# BUILD_PLAN.md — Sellorita Two-Developer Execution Roadmap (CSE 816)

> **Team Structure:** Split cleanly along the **Application/AI Track** and the **Distributed Systems/DevOps Track** so both members work in parallel from Day 1, merging cleanly via feature branches and Pull Requests.
>
> **Guiding principle:** Build a **thin end-to-end slice first** (everything mocked, but real HTTP, queue, DB and storage), then deepen. Dev B builds CI/CD and K8s against that slice from Day 3 instead of waiting for finished agents.

---

## Role Division & Ownership

* **Developer A (AI Agents, FastAPI & Frontend Lead):**
  * Owns `libs/common/`, `services/api_gateway/`, `services/agent_worker/`, `services/frontend/` (app code), `prompts/`, `evals/`, and `tests/`.
  * Builds the LangGraph multi-agent workflow, HyperFrames video rendering pipeline, FastAPI endpoints, Alembic migrations, structured JSON logging, and Review Dashboard UI.
* **Developer B (Distributed Storage, Kubernetes, CI/CD & Observability Lead):**
  * Owns `docker/` (all Dockerfiles incl. `Dockerfile.frontend` + `nginx.conf`), `ansible/`, `k8s/`, `Jenkinsfile`, HashiCorp Vault setup, and the ELK Stack + Kibana dashboards.
  * Configures distributed MinIO, PostgreSQL, Redis StatefulSets, API HPA, **KEDA** worker autoscaling, Celery Beat single-replica Deployment, the Alembic migration Job, and zero-downtime RollingUpdates.
  * Owns the load-test script and wiring the LLM eval gate into Jenkins (Dev A provides the eval runner command).

### Workload Rebalancing (agreed)
Dev A's track is heavier in Phases 1–2, so:
* All **containerization of app services** (worker image with Node/Chromium/FFmpeg/HyperFrames, frontend nginx image) is Dev B's.
* All **K8s runtime concerns** of app components (Beat deployment, migration Job, KEDA scaler, probes) are Dev B's; Dev A only exposes the entrypoints/commands.
* Dev A delivers output formats in priority order **TWEET → STATIC_POST → REEL**, so a demoable pipeline exists at every stage; REEL is the last and riskiest.

---

## Phase 1: Foundation & Local Docker Compose Stack (Days 1–3)

### Developer A Tasks
0. ✅ **DONE — PASS, see [docs/hyperframes_spike.md](../docs/hyperframes_spike.md).** **Day-1 HyperFrames spike (time-boxed, ~half day):** Confirm the HyperFrames CLI/npm package renders a sample HTML/GSAP scene to MP4 headless inside a Linux container with Chromium + FFmpeg. Share the exact install steps with Dev B for `Dockerfile.worker`. Note the result; if it fails, fallback is Playwright frame capture + FFmpeg.
1. **Initialize Monorepo & Shared Library:**
   * Create `libs/common` (installable package): `settings.py` (pydantic-settings), `logger.py` (structlog JSON, routes uvicorn/celery logs too), `db.py`, `models.py`, `schemas.py` (incl. `CampaignState`), `storage.py` (internal + public MinIO clients).
   * Create `services/api_gateway` and `services/agent_worker` depending on `libs/common`.
   * Write `docker/docker-compose.dev.yml` (postgres, redis, minio only) for app development; handed to Dev B afterwards.
2. **Build DB Layer & Core FastAPI Endpoints:**
   * ✅ **DONE — DB layer:** `docker/docker-compose.dev.yml`, `common/db.py`, `common/models.py`, initial Alembic migration `0001`, `sellorita-migrate` command, `tests/test_db.py`.
   * ✅ **DONE — Step 3:** `common/storage.py` (internal + public boto3 clients), `common/queue.py` (shared Celery config, task names), API schemas, `/healthz`, `/readyz`, `POST/GET /shops`, `POST /shops/{id}/assets`, `POST /campaigns/generate` (`send_task` → `campaigns` queue, `202`), `GET /campaigns/{id}`, request-ID + structured access log middleware, `tests/test_api.py`.
   * SQLAlchemy 2.0 models (`shops`, `products_offers`, `campaigns` incl. `FAILED` status, `qa_feedback`, `retry_count`, `error_message`) and the initial **Alembic** migration.
   * Implement `/healthz`, `/readyz`, `/shops`, `/shops/{shop_id}/assets` (MinIO upload), `/campaigns/generate` (Celery `.delay()` dispatch, `202`), `GET /campaigns/{id}`.
3. ✅ **DONE — Step 4:** Celery worker + Beat (`worker/celery_app.py`, `worker/tasks.py`), LangGraph graph (`worker/graph/builder.py`), agents per layer (`worker/agents/`), mock FFmpeg renderer, Redis intelligence cache, `schedule_due_campaigns`, task retries/idempotency, `tests/test_agents.py`. **Implement Mocked LangGraph Pipeline:**
   * Build the LangGraph state machine with parallel intelligence nodes (`CalendarCulturalAgent`, `NewsTrendAgent`, `BrandCatalogAgent`) feeding into `CreativeDirectorAgent` -> `ProductionRouter` -> `QACriticAgent`, with the bounded retry loop and `mark_failed` node.
   * Ensure `MOCK_MODE=true` generates a valid dummy MP4/PNG (real FFmpeg encode) and uploads it to MinIO so end-to-end testing works immediately.
   * Add the `schedule_due_campaigns` Celery Beat task.

### Developer B Tasks
1. **GitHub Repository & Branch Protection:**
   * Create GitHub repo with `main` (protected) and `dev` branches; configure GitHub Webhooks pointing to Jenkins.
2. **Multi-Stage Dockerfiles & Local Compose:**
   * Write `docker/Dockerfile.api` (lightweight Python slim image, installs `libs/common`).
   * Write `docker/Dockerfile.worker` (Python + Node.js + Chromium + FFmpeg + HyperFrames, using Dev A's spike notes).
   * Write `docker/Dockerfile.frontend` (Vite build stage → nginx runtime) and `docker/nginx.conf` (serves SPA, proxies `/api` to api-gateway).
   * Write `docker/docker-compose.yml` spinning up `api-gateway`, `agent-worker`, `celery-beat`, `frontend`, `postgres`, `redis`, and `minio` (with automatic bucket initialization `sellorita-media`) and a one-shot `migrate` service (`alembic upgrade head`).
3. **Provision Target Infrastructure via Ansible (Modular Roles):**
   * Create `ansible/site.yml` with distinct roles:
     * `roles/common`: Installs Docker, utilities, and configures host networking.
     * `roles/k8s_cluster`: Sets up K3s / Kubernetes cluster on target VM(s), incl. KEDA.
     * `roles/vault`: Deploys HashiCorp Vault and seeds `secret/data/sellorita/prod`.
     * `roles/elk`: Deploys Elasticsearch, Logstash/Filebeat, and Kibana.

**Phase 1 Milestone Checkpoint:** Running `docker compose up` locally allows creating a shop, uploading a product photo, triggering a campaign, and seeing a generated MP4/PNG appear in MinIO, the campaign reaching `PENDING_REVIEW`, with JSON logs printed to stdout.

---

## Phase 2: Real Agent Logic, HyperFrames & Review UI (Days 4–7)

### Developer A Tasks
1. **Real LLM Agents (in order TWEET → STATIC_POST → REEL):**
   * Connect agents to the LLM API (key read from environment, injected by Vault in K8s). Use structured output (Pydantic-validated JSON) for intelligence agents and `CreativeDirectorAgent`.
   * Move all prompts into `prompts/{agent}/v{N}.md`; record prompt version + token usage/cost/latency in `llm_usage` and in logs.
   * `CopyTweetAgent` first, then `StaticPostAgent` (LLM-authored HTML → PNG via Playwright/Chromium).
   * `ReelProductionAgent` last: LLM authors HTML/CSS/GSAP scenes using shop brand colors + internally-signed presigned product image URLs, render via HyperFrames to `/tmp/{campaign_id}/`, push to MinIO. QA Critic verifies render success and retries with feedback.
2. **Build Lightweight Review Dashboard (`services/frontend`, React + Vite):**
   * **Shop Setup Page:** Enter shop details, colors, schedule frequency (`Daily`, `2x/week`, `3x/week`), and upload product photos.
   * **Review & Approve Queue:** Preview rendered MP4 Reels (`<video>` player) and static posts, edit generated captions inline, and click **Approve & Publish**. Shows `QUEUED`/`PROCESSING`/`FAILED` states via polling.
   * All API calls use the relative `/api/v1` path (no hard-coded host, no CORS).
3. **Write Automated Pytest Suite (`tests/`) + LLM Eval Runner (`evals/`):**
   * `test_api.py`: Tests `/healthz`, `/readyz`, shop creation, asset upload, and campaign queuing.
   * `test_agents.py`: Tests full LangGraph execution, QA retry loop, and `FAILED` path under `MOCK_MODE=true`.
   * `evals/run_evals.py`: Runs the golden-set inputs through the pipeline and exits non-zero if QA pass rate falls below threshold (used as the Jenkins eval gate).

### Developer B Tasks
1. **Kubernetes Manifests & Distributed Storage (`k8s/`):**
   * Create `StatefulSet` + `PersistentVolumeClaim` manifests for **PostgreSQL**, **Redis**, and **MinIO** (configured in distributed/multi-drive mode).
   * Create `Deployment` manifests for `api-gateway`, `agent-worker`, `celery-beat` (`replicas: 1`, no autoscaling), and `frontend` with `RollingUpdate` strategy (`maxSurge: 1`, `maxUnavailable: 0`), `/healthz` liveness and `/readyz` readiness probes for zero-downtime live patching.
   * Create `jobs/db-migrate.yaml` running `alembic upgrade head` before API rollout.
2. **HashiCorp Vault Integration:**
   * Configure pods to receive DB passwords, MinIO keys, and LLM API keys from Vault via Vault Agent Injector, exposed as environment variables (no app-side Vault code).
3. **Configure Autoscaling (`k8s/autoscaling/`):**
   * **KEDA** `ScaledObject` on `agent-worker` scaling `1 → 5` replicas based on Redis list length of the `campaigns` queue.
   * HPA on `api-gateway` (`minReplicas: 2` to `maxReplicas: 4` at 60% CPU).

**Phase 2 Milestone Checkpoint:** All services run inside Kubernetes, secrets come from HashiCorp Vault, and a user can generate and approve a real Tweet, Static Post and HyperFrames Reel via the web UI.

---

## Phase 3: CI/CD Automation, ELK Observability & Load Testing (Days 8–10)

### Developer A & Developer B Joint Tasks
1. **Build Full `Jenkinsfile` Pipeline:**
   * **Stage 1 (Checkout):** Triggered automatically by GitHub Webhook on push to `main`.
   * **Stage 2 (Lint & Automated Tests):** Spins up ephemeral test container and runs `pytest tests/ --junitxml=results.xml` with `MOCK_MODE=true`.
   * **Stage 3 (LLM Eval Gate):** Runs `python evals/run_evals.py`; pipeline fails if the QA pass rate drops below threshold.
   * **Stage 4 (Build & Push Images):** Builds `api-gateway`, `agent-worker`, and `frontend` Docker images tagged with `${BUILD_NUMBER}` and `latest`, pushing to Docker Hub using Jenkins credentials.
   * **Stage 5 (Migrate & Deploy / Live Patch):** Runs the DB migration Job, then `kubectl set image` / Ansible deployment playbook against the K8s cluster and waits for `kubectl rollout status` to succeed without downtime.
2. **Build Kibana Observability Dashboard:**
   * Verify Filebeat/Logstash is shipping container JSON logs into Elasticsearch index `sellorita-logs-*`.
   * Create and save a Kibana Dashboard showing:
     * Real-time campaign generation logs & status distribution (incl. `FAILED`).
     * Latency breakdown per agent (`CalendarCulturalAgent`, `CreativeDirectorAgent`, `ReelProductionAgent`).
     * FastAPI endpoint throughput and HTTP status codes.
     * QA retry counts and worker queue throughput.
     * **LLMOps:** token usage and cost per agent / prompt version.
3. **Simulate Production Load & Verify Autoscaling:**
   * Trigger a batch of 15 concurrent generation requests (`POST /api/v1/campaigns/generate`) to verify Redis queue buildup, KEDA scaling up `agent-worker` pods, and Kibana visualizing the spike.

---

## Phase 4: Final Demo Preparation & Evaluation Checklist (Day 11+)

Before the final CSE 816 evaluation, verify every mandatory rubric item live:

* [ ] **Git -> Jenkins -> Docker Hub -> K8s Live Patch:** Make a visible UI or caption-template change in Git, push to `main`, show Jenkins automatically running `pytest` + the LLM eval gate, building/pushing images, and updating the live K8s deployment seamlessly on browser refresh.
* [ ] **Multi-Agent + Distributed Storage Proof:** Trigger a new Reel generation for a festival/offer, show `agent-worker` pods scaling via KEDA, show the generated `.mp4` stored inside the MinIO console, and approve it in the dashboard.
* [ ] **LLMOps Proof:** Show versioned prompts in Git, the eval gate stage in Jenkins, and the token/cost panel in Kibana.
* [ ] **ELK Stack & Kibana:** Open Kibana and demonstrate live application logs, agent execution durations, and error tracking.
* [ ] **Security & Modularity:** Show secrets stored in HashiCorp Vault (zero hardcoded credentials in repo) and walk through the modular Ansible roles (`common`, `k8s_cluster`, `vault`, `elk`).
