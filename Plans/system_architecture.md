# DESIGN.md — AdPulse AI: Autonomous Social Media Multi-Agent Platform

> **Purpose:** Single source of truth for architectural decisions, schemas, API contracts, and infrastructure conventions so both developers can work independently without merge conflicts or integration mismatches.

---

## 1. Project Overview & Domain Alignment
* **Problem:** Small local businesses (cafes, salons, retail shops) lack the time and budget to consistently produce context-aware Reels, social posts, and tweets tied to festivals, local news, and active store offers.
* **Solution:** An asynchronous, multi-agent FastAPI platform that automatically researches local/cultural context, plans creative direction, renders MP4 Reels (via **HyperFrames**) and static posts/captions, stores media in distributed object storage (**MinIO**), and presents drafts for one-click owner approval and publishing.
* **CSE 816 Course Alignment:**
  * **Domain-Specific (5 Marks):** AIOps / MLOps multi-agent orchestration with deterministic video rendering.
  * **Advanced Features (3 Marks):** HashiCorp Vault secret management, modular Ansible Roles, Kubernetes Horizontal Pod Autoscaling (HPA), and zero-downtime RollingUpdates.
  * **Innovation (2 Marks):** Parallel intelligence-gathering agents + automated HTML/CSS/GSAP-to-MP4 rendering pipeline + Critic QA agent.

---

## 2. High-Level System Architecture

```text
[Frontend Review Dashboard (React / Next.js)]
       │  (REST API + Presigned Media URLs)
       ▼
[FastAPI Gateway Pods (K8s Deployment + HPA)] ◄───► [HashiCorp Vault]
       │                                              (LLM Keys, DB/MinIO Creds)
       ├──► [PostgreSQL StatefulSet]  (Users, Brand Kits, Schedules, Post States)
       ├──► [Redis StatefulSet]       (Celery Task Broker + Daily Trend Cache)
       └──► [MinIO Distributed Store] (Product Photos, Rendered MP4 Reels, PNG Posts)
                ▲
                │ (Reads raw photos / Uploads rendered MP4 & PNG)
[Celery Multi-Agent + HyperFrames Worker Pods (K8s Deployment + HPA)]
       │
       └──► (Structured JSON Logs via Filebeat/Logstash) ──► [Elasticsearch] ──► [Kibana]
```

### Core Architectural Rules (Agreed Conventions)
1. **Strict Statelessness in Compute Pods:** Neither `api-gateway` nor `agent-worker` pods may store files locally beyond temporary `/tmp/{job_id}` scratch space during rendering. All persistent assets go to **MinIO**; all state goes to **PostgreSQL** or **Redis**.
2. **Asynchronous Job Execution:** HTTP requests never block on LLM calls or video rendering. FastAPI enqueues a task in Redis and returns `202 Accepted` with a `campaign_id`.
3. **Mock-First Testing:** All external LLM/News/HyperFrames calls must support a `MOCK_MODE=true` environment flag so Jenkins CI `pytest` pipelines execute deterministically in `< 15 seconds` without spending API credits.

---

## 3. Multi-Agent Pipeline Specification (LangGraph)

### Shared `CampaignState` Schema (Python TypedDict / Pydantic)
Both developers must adhere strictly to this state object passed between LangGraph nodes:

```python
class CampaignState(TypedDict):
    campaign_id: str
    shop_id: str
    target_format: Literal["REEL", "STATIC_POST", "TWEET"]
    # Populated by Intelligence Layer (Parallel)
    calendar_context: dict       # {"date": str, "upcoming_festival": str, "days_away": int}
    news_trend_context: dict     # {"local_hook": str, "weather": str, "safe_to_use": bool}
    brand_catalog: dict          # {"name": str, "colors": list[str], "font": str, "offer": str, "asset_keys": list[str]}
    # Populated by Strategy Layer
    creative_brief: dict         # {"hook": str, "script": str, "caption": str, "hashtags": list[str], "cta": str, "theme": str}
    # Populated by Production Layer
    artifact_minio_key: str      # e.g., "shops/{shop_id}/outputs/{campaign_id}.mp4"
    # Populated by QA Layer
    qa_passed: bool
    qa_feedback: str
    retry_count: int
```

### Agent Responsibilities
1. **Intelligence Layer (Parallel Execution):**
   * `CalendarCulturalAgent`: Checks current date, upcoming Indian/local festivals (Diwali, Onam, Pongal, weekends), and seasonal context. Caches daily output in Redis (`cache:calendar:{YYYY-MM-DD}`).
   * `NewsTrendAgent`: Pulls local city news/weather/viral hooks and filters out negative/controversial topics (`safe_to_use: bool`). Caches per city in Redis (`cache:trends:{city}:{YYYY-MM-DD}`).
   * `BrandCatalogAgent`: Queries PostgreSQL for the shop's brand colors, typography, tone, active promotion, and MinIO object keys of uploaded product images.
2. **Strategy Layer:**
   * `CreativeDirectorAgent`: Synthesizes the 3 intelligence outputs into `creative_brief` (hook, 15s Reel script or post copy, CTA, layout preset, and font pairing).
3. **Production Layer (Conditional Router):**
   * `ReelProductionAgent`: Uses the **HyperFrames Claude Skill** conventions to generate self-contained HTML5/CSS3/GSAP animation frames, embeds presigned product images, and invokes the HyperFrames CLI/renderer to export a `1080x1920` MP4 file to MinIO.
   * `StaticPostAgent`: Generates a `1080x1080` HTML/CSS promotional card (or integrates Pomelli API if configured) and renders to PNG via headless Chromium, uploading to MinIO.
   * `CopyTweetAgent`: Formats concise X/Twitter threads and Instagram/Facebook captions with localized hashtags.
4. **Verification Layer:**
   * `QACriticAgent`: Validates non-empty media output, checks caption length limits, verifies brand name/CTA presence, and triggers up to `retry_count <= 2` back to `CreativeDirectorAgent` on failure.

---

## 4. Distributed Storage & Data Contracts

### 4.1 PostgreSQL Schema (`adpulse_db`)
* **`shops`**: `id (UUID PK)`, `name`, `category`, `city`, `brand_primary_color`, `brand_secondary_color`, `brand_font`, `tone`, `schedule_frequency` (`DAILY` | `TWICE_WEEKLY` | `THRICE_WEEKLY`), `created_at`
* **`products_offers`**: `id (UUID PK)`, `shop_id (FK)`, `title`, `discount_text`, `minio_image_key`, `is_active`, `last_featured_at`
* **`campaigns`**: `id (UUID PK)`, `shop_id (FK)`, `format` (`REEL` | `STATIC_POST` | `TWEET`), `status` (`QUEUED` | `PROCESSING` | `PENDING_REVIEW` | `APPROVED` | `REJECTED` | `PUBLISHED`), `caption`, `hashtags (JSONB)`, `minio_output_key`, `agent_metadata (JSONB)`, `scheduled_for`, `created_at`

### 4.2 MinIO Bucket Layout
* **Bucket Name:** `adpulse-media`
  * `shops/{shop_id}/brand/logo.png`
  * `shops/{shop_id}/products/{product_id}.jpg`
  * `shops/{shop_id}/outputs/{campaign_id}.mp4` (or `.png`)

### 4.3 Redis Key Conventions
* Celery Broker / Result Backend: DB `0` and DB `1`
* Shared Intelligence Cache: `intel:{city}:{YYYY-MM-DD}` (TTL: `43200` seconds / 12 hours)

---

## 5. FastAPI REST Contract (API Gateway)

Base Prefix: `/api/v1`

| Method | Endpoint | Request Body / Params | Response (`200` / `202`) | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/healthz` | None | `{"status": "ok", "db": "up", "redis": "up", "minio": "up"}` | K8s Liveness/Readiness probe |
| `POST` | `/shops` | `ShopCreateSchema` | `201`: `ShopResponse` | Register business profile & schedule |
| `POST` | `/shops/{shop_id}/assets` | `multipart/form-data` (`file`, `title`, `discount`) | `201`: `AssetResponse` | Upload product photo to MinIO + DB |
| `POST` | `/campaigns/generate` | `{"shop_id": "uuid", "format": "REEL"}` | `202`: `{"campaign_id": "uuid", "status": "QUEUED"}` | Enqueue multi-agent Celery pipeline |
| `GET` | `/shops/{shop_id}/campaigns` | `?status=PENDING_REVIEW` | `200`: `list[CampaignResponse]` (includes presigned MinIO URLs) | Fetch drafts for review dashboard |
| `PATCH` | `/campaigns/{campaign_id}` | `{"status": "APPROVED", "caption": "edited text"}` | `200`: `CampaignResponse` | Approve/edit draft & trigger mock publish |

---

## 6. DevOps, Security & Observability Standards

### 6.1 Repository Monorepo Structure
```text
adpulse-ai/
├── services/
│   ├── api_gateway/          # FastAPI app, SQLAlchemy models, MinIO/Vault clients
│   ├── agent_worker/         # Celery app, LangGraph agents, HyperFrames renderer
│   └── frontend/             # Lightweight React/Vite Review Dashboard
├── tests/
│   ├── test_api.py           # FastAPI endpoint & DB integration tests
│   └── test_agents.py        # LangGraph workflow tests (MOCK_MODE=true)
├── docker/
│   ├── Dockerfile.api
│   ├── Dockerfile.worker     # Includes Node.js, Chromium, FFmpeg, HyperFrames
│   └── docker-compose.yml
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
│   ├── deployments/          # api-gateway.yaml, agent-worker.yaml, frontend.yaml
│   ├── statefulsets/         # postgres.yaml, redis.yaml, minio.yaml
│   ├── hpa/                  # worker-hpa.yaml, api-hpa.yaml
│   └── services/
└── Jenkinsfile               # Lint -> Pytest -> Docker Build/Push -> K8s RollingUpdate
```

### 6.2 Structured Logging Schema (ELK Ingestion)
Every log line emitted to `stdout` by FastAPI and Celery **must** be valid JSON using `structlog`:
```json
{
  "timestamp": "2026-11-15T18:00:00Z",
  "level": "INFO",
  "service": "agent-worker",
  "event": "agent_node_completed",
  "campaign_id": "c91a2b3c-...",
  "shop_id": "s12d3e4f-...",
  "agent_name": "ReelProductionAgent",
  "duration_ms": 4210,
  "status": "SUCCESS"
}
```
* **Kibana Dashboard Panels (Minimum 4 required for demo):**
  1. Total Campaigns Generated by Format (`REEL` vs `STATIC_POST` vs `TWEET`).
  2. Average Latency per Agent Node (`CalendarAgent`, `CreativeDirector`, `ReelProductionAgent`).
  3. FastAPI HTTP Request Rate & Error Count (`4xx`/`5xx`).
  4. Worker Queue Throughput & QA Retry Count.

### 6.3 Secrets Management (HashiCorp Vault)
* **Vault Path:** `secret/data/adpulse/prod`
* **Keys Stored:** `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `ANTHROPIC_API_KEY`, `NEWS_API_KEY`.
* No plaintext passwords or API keys may be committed to Git. Local development uses `.env.example` copied to gitignored `.env`.