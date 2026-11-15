# BUILD_PLAN.md — Two-Developer Execution Roadmap (CSE 816)

> **Team Structure:** Split cleanly along the **Application/AI Track** and the **Distributed Systems/DevOps Track** so both members work in parallel from Day 1, merging cleanly via feature branches and Pull Requests.

---

## Role Division & Ownership

* **Developer A (AI Agents, FastAPI & Frontend Lead):**
  * Owns `services/api_gateway/`, `services/agent_worker/`, `services/frontend/`, and `tests/`.
  * Builds the LangGraph multi-agent workflow, HyperFrames video rendering pipeline, FastAPI endpoints, structured JSON logging, and Review Dashboard UI.
* **Developer B (Distributed Storage, Kubernetes, CI/CD & Observability Lead):**
  * Owns `docker/`, `ansible/`, `k8s/`, `Jenkinsfile`, HashiCorp Vault setup, and the ELK Stack + Kibana dashboards.
  * Configures distributed MinIO, PostgreSQL, Redis StatefulSets, Kubernetes HPA, and zero-downtime RollingUpdates.

---

## Phase 1: Foundation & Local Docker Compose Stack (Days 1–3)

### Developer A Tasks
1. **Initialize Monorepo & Python Environments:**
   * Create `services/api_gateway` and `services/agent_worker` with shared Pydantic schemas matching `DESIGN.md` Section 3 & 5.
   * Implement `structlog` JSON logger utility (`common/logger.py`) so every service outputs uniform JSON logs.
2. **Build Core FastAPI Endpoints & DB Models:**
   * Implement SQLAlchemy async models (`shops`, `products_offers`, `campaigns`).
   * Implement `/healthz`, `/shops`, `/shops/{shop_id}/assets` (MinIO upload via `boto3`), and `/campaigns/generate` (Celery `.delay()` dispatch).
3. **Implement Mocked LangGraph Pipeline:**
   * Build the LangGraph state machine with parallel intelligence nodes (`CalendarCulturalAgent`, `NewsTrendAgent`, `BrandCatalogAgent`) feeding into `CreativeDirectorAgent` -> `ProductionRouter` -> `QACriticAgent`.
   * Ensure `MOCK_MODE=true` generates a valid dummy MP4/PNG and uploads it to MinIO so end-to-end testing works immediately.

### Developer B Tasks
1. **GitHub Repository & Branch Protection:**
   * Create GitHub repo with `main` (protected) and `dev` branches; configure GitHub Webhooks pointing to Jenkins.
2. **Multi-Stage Dockerfiles & Local Compose:**
   * Write `docker/Dockerfile.api` (lightweight Python slim image).
   * Write `docker/Dockerfile.worker` (Python + Node.js + Chromium + FFmpeg for HyperFrames rendering).
   * Write `docker/docker-compose.yml` spinning up `api-gateway`, `agent-worker`, `postgres`, `redis`, and `minio` (with automatic bucket initialization `adpulse-media`).
3. **Provision Target Infrastructure via Ansible (Modular Roles):**
   * Create `ansible/site.yml` with distinct roles:
     * `roles/common`: Installs Docker, utilities, and configures host networking.
     * `roles/k8s_cluster`: Sets up K3s / Kubernetes cluster on target VM(s).
     * `roles/vault`: Deploys HashiCorp Vault and seeds `secret/data/adpulse/prod`.
     * `roles/elk`: Deploys Elasticsearch, Logstash/Filebeat, and Kibana.

**Phase 1 Milestone Checkpoint:** Running `docker-compose up` locally allows uploading a shop logo, triggering a campaign, and seeing a generated MP4/PNG appear in MinIO with JSON logs printed to stdout.

---

## Phase 2: Real Agent Logic, HyperFrames & Review UI (Days 4–7)

### Developer A Tasks
1. **Integrate HyperFrames Claude Skill for Reels:**
   * Connect `CreativeDirectorAgent` to LLM API (reading key from environment/Vault).
   * Build `ReelProductionAgent` to invoke the HyperFrames skill pipeline: generate HTML/CSS/GSAP scene files using shop brand colors + presigned product image URLs, render via HyperFrames CLI to `/tmp/{campaign_id}.mp4`, and push to MinIO.
   * Build `StaticPostAgent` (HTML-to-PNG via Playwright/Chromium) and `CopyTweetAgent`.
2. **Build Lightweight Review Dashboard (`services/frontend`):**
   * Create a clean 2-page React/Vite (or Tailwind + HTMX) dashboard:
     * **Shop Setup Page:** Enter shop details, colors, schedule frequency (`Daily`, `2x/week`, `3x/week`), and upload product photos.
     * **Review & Approve Queue:** Preview rendered MP4 Reels (`<video>` player) and static posts, edit generated captions inline, and click **Approve & Publish**.
3. **Write Automated Pytest Suite (`tests/`):**
   * `test_api.py`: Tests `/healthz`, shop creation, and campaign queuing.
   * `test_agents.py`: Tests full LangGraph execution and QA retry loop under `MOCK_MODE=true`.

### Developer B Tasks
1. **Kubernetes Manifests & Distributed Storage (`k8s/`):**
   * Create `StatefulSet` + `PersistentVolumeClaim` manifests for **PostgreSQL**, **Redis**, and **MinIO** (configured in distributed/multi-drive mode).
   * Create `Deployment` manifests for `api-gateway`, `agent-worker`, and `frontend` with `RollingUpdate` strategy (`maxSurge: 1`, `maxUnavailable: 0`) and `/healthz` readiness probes for zero-downtime live patching.
2. **HashiCorp Vault Integration:**
   * Configure Kubernetes pods to pull DB passwords, MinIO keys, and LLM API keys from Vault (via Vault Agent Injector annotations or init-container).
3. **Configure Horizontal Pod Autoscaling (`k8s/hpa/`):**
   * Apply HPA on `agent-worker` (scale `minReplicas: 1` to `maxReplicas: 5` at 60% CPU utilization) and `api-gateway` (`minReplicas: 2` to `maxReplicas: 4`).

**Phase 2 Milestone Checkpoint:** All services run inside Kubernetes, secrets come from HashiCorp Vault, and a user can generate and approve a real HyperFrames Reel via the web UI.

---

## Phase 3: CI/CD Automation, ELK Observability & Load Testing (Days 8–10)

### Developer A & Developer B Joint Tasks
1. **Build Full `Jenkinsfile` Pipeline:**
   * **Stage 1 (Checkout):** Triggered automatically by GitHub Webhook on push to `main`.
   * **Stage 2 (Automated Tests):** Spins up ephemeral test container and runs `pytest tests/ --junitxml=results.xml` with `MOCK_MODE=true`.
   * **Stage 3 (Build & Push Images):** Builds `api-gateway`, `agent-worker`, and `frontend` Docker images tagged with `${BUILD_NUMBER}` and `latest`, pushing to Docker Hub using Jenkins credentials.
   * **Stage 4 (Deploy / Live Patch):** Executes `kubectl set image` / Ansible deployment playbook against the K8s cluster and waits for `kubectl rollout status` to succeed without downtime.
2. **Build Kibana Observability Dashboard:**
   * Verify Filebeat/Logstash is shipping container JSON logs into Elasticsearch index `adpulse-logs-*`.
   * Create and save a Kibana Dashboard showing:
     * Real-time campaign generation logs & status distribution.
     * Latency breakdown per agent (`CalendarAgent`, `CreativeDirectorAgent`, `ReelProductionAgent`).
     * FastAPI endpoint throughput and HTTP status codes.
3. **Simulate Production Load & Verify HPA:**
   * Trigger a batch of 15 concurrent Reel generation requests (`POST /api/v1/campaigns/generate`) to verify Redis queue buildup, Kubernetes HPA scaling up `agent-worker` pods, and Kibana visualizing the spike.

---

## Phase 4: Final Demo Preparation & Evaluation Checklist (Day 11+)

Before the final CSE 816 evaluation, verify every mandatory rubric item live:

* [ ] **Git -> Jenkins -> Docker Hub -> K8s Live Patch:** Make a visible UI or caption-template change in Git, push to `main`, show Jenkins automatically running `pytest`, building/pushing images, and updating the live K8s deployment seamlessly on browser refresh.
* [ ] **Multi-Agent + Distributed Storage Proof:** Trigger a new Reel generation for a festival/offer, show `agent-worker` pods scaling via HPA, show the generated `.mp4` stored inside the MinIO console, and approve it in the dashboard.
* [ ] **ELK Stack & Kibana:** Open Kibana and demonstrate live application logs, agent execution durations, and error tracking.
* [ ] **Security & Modularity:** Show secrets stored in HashiCorp Vault (zero hardcoded credentials in repo) and walk through the modular Ansible roles (`common`, `k8s_cluster`, `vault`, `elk`).