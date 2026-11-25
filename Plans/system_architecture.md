# DESIGN.md — Sellorita: Autonomous Social Media Multi-Agent Platform

> **Purpose:** Single source of truth for architectural decisions, schemas, API contracts, and infrastructure conventions so both developers can work independently without merge conflicts or integration mismatches.

---

## 1. Project Overview & Domain Alignment
* **Problem:** Small local businesses (cafes, salons, retail shops) lack the time and budget to consistently produce context-aware Reels, social posts, and tweets tied to festivals, local news, and active store offers.
* **Solution:** An asynchronous, multi-agent FastAPI platform that automatically researches local/cultural context, plans creative direction, renders MP4 Reels (via **HyperFrames**) and static posts/captions, stores media in distributed object storage (**MinIO**), and presents drafts for one-click owner approval and publishing.
* **CSE 816 Course Alignment:**
  * **Domain-Specific (5 Marks): LLMOps** — operating a production multi-agent LLM system:
    * **Versioned prompts** stored as files under `prompts/` in Git; every campaign records the prompt versions it used.
    * **LLM evaluation gate in CI:** Jenkins runs a golden-set evaluation of the agent pipeline + QA Critic before images are pushed (quality gate for AI output, analogous to unit tests for code).
    * **LLM observability:** per-agent token usage, estimated cost, latency and QA retry counts are logged as structured JSON and visualized in Kibana.
  * **Advanced Features (3 Marks):** HashiCorp Vault secret management, modular Ansible Roles, Kubernetes autoscaling (HPA + **KEDA** queue-based scaling), and zero-downtime RollingUpdates.
  * **Innovation (2 Marks):** Parallel intelligence-gathering agents + LLM-authored HTML/CSS/GSAP-to-MP4 rendering pipeline + Critic QA agent with a bounded self-correction loop.

---

## 2. High-Level System Architecture

```text
[Frontend Review Dashboard (React + Vite, served by nginx)]
       │  (REST via relative /api path + Presigned Media URLs)
       ▼
[FastAPI Gateway Pods (K8s Deployment + HPA)]
       │        ▲
       │        └── env vars injected by Vault Agent ◄── [HashiCorp Vault]
       ├──► [PostgreSQL StatefulSet]  (Shops, Products/Offers, Campaigns)
       ├──► [Redis StatefulSet]       (Celery Broker/Results + Intelligence Cache)
       └──► [MinIO Distributed Store] (Product Photos, Rendered MP4 Reels, PNG Posts)
                ▲
                │ (Reads raw photos / Uploads rendered MP4 & PNG)
[Celery Multi-Agent + HyperFrames Worker Pods (K8s Deployment, scaled by KEDA on Redis queue length)]
[Celery Beat Scheduler (single-replica Deployment, never autoscaled)]
       │
       └──► (Structured JSON Logs via Filebeat/Logstash) ──► [Elasticsearch] ──► [Kibana]

[DB Migration Job (Alembic `upgrade head`, runs once per deploy before API rollout)]
```

### Core Architectural Rules (Agreed Conventions)
1. **Strict Statelessness in Compute Pods:** Neither `api-gateway` nor `agent-worker` pods may store files locally beyond temporary `/tmp/{campaign_id}` scratch space during rendering. All persistent assets go to **MinIO**; all state goes to **PostgreSQL** or **Redis**.
2. **Asynchronous Job Execution:** HTTP requests never block on LLM calls or video rendering. FastAPI enqueues a task in Redis and returns `202 Accepted` with a `campaign_id`. Clients poll for status.
3. **Mock-First Testing:** All external LLM/News/HyperFrames calls must support a `MOCK_MODE=true` environment flag so Jenkins CI `pytest` pipelines execute deterministically in `< 15 seconds` without spending API credits. Mock mode still performs a **real FFmpeg encode** of the dummy media so CPU load and the pipeline shape are realistic.
4. **12-Factor Configuration:** Application code reads configuration **only from environment variables** (via `pydantic-settings`). The app never talks to Vault directly — Vault Agent injects secrets into the pod environment in K8s; locally, a gitignored `.env` is used.
5. **No Authentication (Demo Scope):** The platform is single-tenant for the demo; no users table and no login. Shops are identified by `shop_id`.
6. **Schema Changes via Migrations Only:** No `create_all()` at app startup. All schema changes are Alembic migrations, applied once per deploy by a K8s Job (or `alembic upgrade head` in local compose).

---

## 3. Multi-Agent Pipeline Specification (LangGraph)

### Shared `CampaignState` Schema (Python TypedDict / Pydantic)
Both developers must adhere strictly to this state object passed between LangGraph nodes. It lives in `libs/common/common/schemas.py`.

```python
class CampaignState(TypedDict):
    campaign_id: str
    shop_id: str
    target_format: Literal["REEL", "STATIC_POST", "TWEET"]
    # Populated by Intelligence Layer (Parallel) — each agent writes ONLY its own key (no reducers needed)
    calendar_context: dict       # {"date": str, "upcoming_festival": str, "days_away": int}
    news_trend_context: dict     # {"local_hook": str, "weather": str, "safe_to_use": bool}
    brand_catalog: dict          # {"name": str, "colors": list[str], "font": str, "offer": str, "asset_keys": list[str]}
    # Populated by Strategy Layer
    creative_brief: dict         # {"hook": str, "script": str, "caption": str, "hashtags": list[str], "cta": str, "theme": str}
    # Populated by Production Layer
    artifact_minio_key: Optional[str]   # e.g., "shops/{shop_id}/outputs/{campaign_id}.mp4"; None for TWEET
    # Populated by QA Layer
    qa_passed: bool
    qa_feedback: str
    retry_count: int
    # Set by any node on unrecoverable failure
    error: Optional[str]
    # LLMOps metadata (accumulated per node: tokens, cost, latency, prompt version)
    llm_usage: list[dict]        # [{"agent": str, "prompt_version": str, "input_tokens": int, "output_tokens": int, "cost_usd": float, "duration_ms": int}]
```

> `llm_usage` is the one key appended to by multiple nodes, so it uses a list-append reducer (`Annotated[list[dict], operator.add]`).

### Agent Responsibilities
1. **Intelligence Layer (Parallel Execution):**
   * `CalendarCulturalAgent`: Checks current date, upcoming Indian/local festivals (Diwali, Onam, Pongal, weekends), and seasonal context. Caches daily output in Redis (`cache:calendar:{YYYY-MM-DD}`).
   * `NewsTrendAgent`: Pulls local city news/weather/viral hooks and filters out negative/controversial topics (`safe_to_use: bool`). Caches per city in Redis (`cache:trends:{city}:{YYYY-MM-DD}`).
   * `BrandCatalogAgent`: Queries PostgreSQL for the shop's brand colors, typography, tone, active promotion, and MinIO object keys of uploaded product images.
2. **Strategy Layer:**
   * `CreativeDirectorAgent`: Synthesizes the 3 intelligence outputs into `creative_brief` (hook, 15s Reel script or post copy, CTA, theme). Uses **structured output** validated against a Pydantic model. On a retry, it receives `qa_feedback`.
3. **Production Layer (Conditional Router on `target_format`):**
   * `ReelProductionAgent`: The LLM authors self-contained HTML5/CSS3/GSAP animation scenes (following HyperFrames conventions) using brand colors and **internally-signed** presigned product image URLs, then invokes the HyperFrames CLI/renderer to export a `1080x1920` MP4 to `/tmp/{campaign_id}/` and uploads it to MinIO.
   * `StaticPostAgent`: The LLM authors a `1080x1080` HTML/CSS promotional card, rendered to PNG via headless Chromium (Playwright), uploaded to MinIO.
   * `CopyTweetAgent`: Formats concise X/Twitter threads and Instagram/Facebook captions with localized hashtags (no media; `artifact_minio_key = None`).
4. **Verification Layer:**
   * `QACriticAgent`: Validates that rendering succeeded and media is non-empty (for REEL/STATIC_POST), checks caption length limits, and verifies brand name/CTA presence. On failure it routes back to `CreativeDirectorAgent` with feedback while `retry_count < 2`; after that the campaign is marked `FAILED`.

### Graph Shape
```text
START ─┬─► calendar ─┐
       ├─► news ─────┼─► creative_director ─► [route by format] ─► reel | post | tweet ─► qa_critic
       └─► brand ────┘           ▲                                                         │
                                 └──────────── retry (retry_count < 2) ◄───────────────────┤
                                                              done ─► END  /  fail ─► mark_failed ─► END
```

### Prompt Versioning
* Each agent's prompt lives in `prompts/{agent_name}/v{N}.md`. The active version is selected via settings (default: latest).
* The prompt version used by each node is recorded in `llm_usage` and persisted to `campaigns.agent_metadata`.

---

## 4. Distributed Storage & Data Contracts

### 4.1 PostgreSQL Schema (`sellorita_db`)
* **`shops`**: `id (UUID PK)`, `name`, `category`, `city`, `brand_primary_color`, `brand_secondary_color`, `brand_font`, `tone`, `logo_minio_key (nullable)`, `schedule_frequency` (`DAILY` | `TWICE_WEEKLY` | `THRICE_WEEKLY`), `created_at`
* **`products_offers`**: `id (UUID PK)`, `shop_id (FK)`, `title`, `discount_text`, `minio_image_key`, `is_active`, `last_featured_at`
* **`campaigns`**: `id (UUID PK)`, `shop_id (FK)`, `format` (`REEL` | `STATIC_POST` | `TWEET`), `status` (`QUEUED` | `PROCESSING` | `PENDING_REVIEW` | `APPROVED` | `REJECTED` | `PUBLISHED` | `FAILED`), `caption`, `hashtags (JSONB)`, `minio_output_key (nullable)`, `qa_feedback (nullable)`, `retry_count (int, default 0)`, `error_message (nullable)`, `agent_metadata (JSONB — intelligence outputs, creative brief, llm_usage, prompt versions)`, `trigger` (`MANUAL` | `SCHEDULED`), `scheduled_for`, `created_at`, `updated_at`
* **Migrations:** managed by Alembic in `libs/common/common/migrations/` (inside the package, so every image that installs `sellorita-common` ships them). Run with the `sellorita-migrate` console script (= `alembic upgrade head` with the packaged config).
* **Storage conventions:** UUID PKs generated in Python; enums stored as `VARCHAR` + named `CHECK` constraints (not native PG enums); all timestamps `timestamptz`; FKs `ON DELETE CASCADE`; index `ix_campaigns_shop_id_status` for the review queue.

### 4.2 MinIO Bucket Layout
* **Image:** MinIO stopped publishing community images (Docker Hub and quay.io refuse pulls since Sept 2026). We use the **Pigsty community build** of the same AGPL source: `pgsty/minio:RELEASE.2026-08-04T00-00-00Z` and `pgsty/mc:RELEASE.2026-11-12T00-00-00Z`, which still include the web console. Pin these tags in compose and K8s.
* **Bucket Name:** `sellorita-media`
  * `shops/{shop_id}/brand/logo.png`
  * `shops/{shop_id}/products/{product_id}.jpg`
  * `shops/{shop_id}/outputs/{campaign_id}.mp4` (or `.png`)
* **Presigned URL rule:** a presigned URL's signature includes the host, so two clients are configured:
  * `MINIO_INTERNAL_ENDPOINT` (e.g. `http://minio:9000`) — used by workers/Chromium inside the cluster.
  * `MINIO_PUBLIC_ENDPOINT` (e.g. `http://<ingress-host>/media`) — used to sign URLs returned to the browser.

### 4.3 Redis Key Conventions
* Celery Broker: DB `0`; Celery Result Backend: DB `1`
* Intelligence Cache: DB `2`
  * `cache:calendar:{YYYY-MM-DD}` (TTL: `43200` seconds / 12 hours)
  * `cache:trends:{city}:{YYYY-MM-DD}` (TTL: `43200` seconds / 12 hours)
* Celery queue name used by KEDA scaler: `campaigns`

### 4.4 Scheduling
* **Celery Beat** runs a periodic task (`schedule_due_campaigns`, every 15 min) that finds shops due for content based on `schedule_frequency` and enqueues campaigns with `trigger = SCHEDULED`.
* Beat must run as exactly **one replica**; it is never autoscaled (multiple replicas would duplicate scheduled jobs).

---

## 5. FastAPI REST Contract (API Gateway)

Base Prefix: `/api/v1`

| Method | Endpoint | Request Body / Params | Response (`200` / `202`) | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/healthz` | None | `{"status": "ok"}` | K8s **liveness** probe (process alive) |
| `GET` | `/readyz` | None | `{"status": "ok", "db": "up", "redis": "up", "minio": "up"}` (`503` if any down) | K8s **readiness** probe (dependencies reachable) |
| `POST` | `/shops` | `ShopCreateSchema` | `201`: `ShopResponse` | Register business profile & schedule |
| `GET` | `/shops/{shop_id}` | None | `200`: `ShopResponse` | Fetch shop profile |
| `POST` | `/shops/{shop_id}/assets` | `multipart/form-data` (`file`, `title`, `discount`) | `201`: `AssetResponse` | Upload product photo to MinIO + DB |
| `POST` | `/campaigns/generate` | `{"shop_id": "uuid", "format": "REEL"}` | `202`: `{"campaign_id": "uuid", "status": "QUEUED"}` | Enqueue multi-agent Celery pipeline |
| `GET` | `/campaigns/{campaign_id}` | None | `200`: `CampaignResponse` | Poll a single campaign's status |
| `GET` | `/shops/{shop_id}/campaigns` | `?status=PENDING_REVIEW` | `200`: `list[CampaignResponse]` (includes public presigned MinIO URLs) | Fetch drafts for review dashboard |
| `PATCH` | `/campaigns/{campaign_id}` | `{"status": "APPROVED", "caption": "edited text"}` | `200`: `CampaignResponse` | Approve/edit draft & trigger mock publish |

---

## 6. DevOps, Security & Observability Standards

### 6.1 Repository Monorepo Structure
```text
sellorita/
├── libs/
│   └── common/               # Installable package shared by API + worker (pip install -e libs/common)
│       ├── common/
│       │   ├── settings.py   # pydantic-settings: all config from env vars
│       │   ├── logger.py     # structlog JSON logging (also wraps uvicorn/celery stdlib logs)
│       │   ├── db.py         # async engine (API) + sync engine (worker)
│       │   ├── models.py     # SQLAlchemy 2.0 models
│       │   ├── schemas.py    # Pydantic API schemas + CampaignState
│       │   └── storage.py    # MinIO clients (internal + public endpoints)
│       │   ├── migrate.py    # `sellorita-migrate` entrypoint (Alembic with packaged config)
│       │   ├── alembic.ini
│       │   └── migrations/   # Alembic env + versions/
│       └── pyproject.toml
├── services/
│   ├── api_gateway/          # FastAPI app (routers, dependencies)
│   ├── agent_worker/         # Celery app + Beat schedule, LangGraph graph, agents, renderers
│   └── frontend/             # React + Vite Review Dashboard (built static, served by nginx)
├── prompts/                  # Versioned agent prompts: prompts/{agent}/v{N}.md
├── evals/                    # Golden-set inputs + expected checks for the CI LLM eval gate
├── tests/
│   ├── test_api.py           # FastAPI endpoint & DB integration tests
│   └── test_agents.py        # LangGraph workflow tests (MOCK_MODE=true)
├── docker/
│   ├── Dockerfile.api
│   ├── Dockerfile.worker     # Includes Node.js, Chromium, FFmpeg, HyperFrames
│   ├── Dockerfile.frontend   # Vite build stage + nginx runtime stage
│   ├── nginx.conf            # Serves SPA, proxies /api to api-gateway
│   ├── docker-compose.yml    # Full local stack
│   └── docker-compose.dev.yml# Only postgres, redis, minio (for app development)
├── ansible/
│   ├── inventory.ini
│   ├── site.yml
│   └── roles/
│       ├── common/           # Docker, dependencies, firewall
│       ├── k8s_cluster/      # K3s/Minikube/Kubeadm setup
│       ├── vault/            # HashiCorp Vault installation & policy seeding
│       └── elk/              # Elasticsearch, Logstash, Kibana deployment
├── k8s/
│   ├── namespace.yaml
│   ├── deployments/          # api-gateway.yaml, agent-worker.yaml, celery-beat.yaml, frontend.yaml
│   ├── jobs/                 # db-migrate.yaml (Alembic upgrade head)
│   ├── statefulsets/         # postgres.yaml, redis.yaml, minio.yaml
│   ├── autoscaling/          # api-hpa.yaml, worker-keda-scaledobject.yaml
│   └── services/
├── .env.example
└── Jenkinsfile               # Lint -> Pytest -> LLM Eval Gate -> Docker Build/Push -> Migrate -> K8s RollingUpdate
```

### 6.2 Structured Logging Schema (ELK Ingestion)
Every log line emitted to `stdout` by FastAPI and Celery **must** be valid JSON using `structlog` (stdlib loggers from uvicorn/celery are routed through structlog too). `campaign_id`/`shop_id` are bound via `contextvars` so they appear on every line of a task. HTTP requests carry a `request_id`.
```json
{
  "timestamp": "2026-11-15T18:00:00Z",
  "level": "INFO",
  "service": "agent-worker",
  "event": "agent_node_completed",
  "campaign_id": "c91a2b3c-...",
  "shop_id": "s12d3e4f-...",
  "agent_name": "ReelProductionAgent",
  "prompt_version": "v2",
  "input_tokens": 1830,
  "output_tokens": 2410,
  "cost_usd": 0.0123,
  "duration_ms": 4210,
  "status": "SUCCESS"
}
```
* **Kibana Dashboard Panels (Minimum 5 required for demo):**
  1. Total Campaigns Generated by Format (`REEL` vs `STATIC_POST` vs `TWEET`) and final status (incl. `FAILED`).
  2. Average Latency per Agent Node (`CalendarCulturalAgent`, `CreativeDirectorAgent`, `ReelProductionAgent`, ...).
  3. FastAPI HTTP Request Rate & Error Count (`4xx`/`5xx`).
  4. Worker Queue Throughput & QA Retry Count.
  5. **LLMOps:** Token usage and estimated cost per agent / per prompt version.

### 6.3 Secrets Management (HashiCorp Vault)
* **Vault Path:** `secret/data/sellorita/prod`
* **Keys Stored:** `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `ANTHROPIC_API_KEY`, `NEWS_API_KEY`.
* Delivery: Vault Agent Injector renders secrets into the pod, exported as environment variables. Application code has **no Vault client**.
* No plaintext passwords or API keys may be committed to Git. Local development uses `.env.example` copied to gitignored `.env`.
