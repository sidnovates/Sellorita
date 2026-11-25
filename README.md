# Sellorita

Autonomous multi-agent social media content platform for local businesses (CSE 816 project).
Design: [Plans/system_architecture.md](Plans/system_architecture.md) · Build plan: [Plans/team_execution_devops_build_plan.md](Plans/team_execution_devops_build_plan.md)

## Repository layout

| Path | Owner | Contents |
| --- | --- | --- |
| `libs/common/` | Dev A | Shared package: settings, JSON logger, schemas, DB models + Alembic migrations, MinIO storage, Celery config |
| `services/api_gateway/` | Dev A | FastAPI app (`app` package) |
| `services/agent_worker/` | Dev A | Celery + LangGraph worker (`worker` package) |
| `tests/` | Dev A | pytest suite |
| `docker/docker-compose.dev.yml` | Dev A | Postgres + Redis + MinIO for local development |
| `docker/` (rest), `k8s/`, `ansible/`, `Jenkinsfile` | Dev B | Infrastructure and CI/CD |

## Local development (Python 3.11+)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` on Linux/macOS
pip install -r requirements-dev.txt
copy .env.example .env          # `cp` on Linux/macOS
docker compose -f docker/docker-compose.dev.yml up -d --wait   # Postgres, Redis, MinIO
sellorita-migrate               # = alembic upgrade head
pytest
uvicorn app.main:app --app-dir services/api_gateway --reload
```

Then open http://localhost:8000/api/v1/docs (interactive API docs) or http://localhost:8000/api/v1/readyz.

Run the agent worker and the scheduler in two more terminals (on Linux/macOS, drop `--pool=solo`):

```bash
celery --workdir services/agent_worker -A worker.celery_app worker -Q campaigns --pool=solo
celery --workdir services/agent_worker -A worker.celery_app beat     # exactly one instance
```

With `MOCK_MODE=true`, a campaign goes `QUEUED -> PROCESSING -> PENDING_REVIEW` in a couple of
seconds. No LLM is called; FFmpeg really encodes a brand-coloured MP4/PNG and uploads it to MinIO.
To demo the QA retry loop, set `MOCK_QA_FAILURES=1` (one rejection, then pass) or `3` (ends `FAILED`). The MinIO console is at http://localhost:9001
(user `sellorita`, password `sellorita_dev`, local-only defaults from `.env.example`).

The integration tests need the dev services running. They use their own `sellorita_test`
database and `sellorita-test` bucket (see `tests/conftest.py`), and skip with a message if a
service is unreachable. Pipeline tests run Celery tasks in-process (`.apply()`), so no worker is
needed. They write the intelligence cache to Redis DB 15, not the dev DB 2. FFmpeg must be on `PATH`.

### Changing the database schema

Edit `libs/common/common/models.py`, then generate and review a migration:

```bash
sellorita-migrate revision --autogenerate -m "add xyz"   # writes common/migrations/versions/...
sellorita-migrate upgrade head
```

Never call `Base.metadata.create_all()`. `test_migration_matches_models` fails if the models
and migrations drift apart.

## Docker notes for Dev B

* **Migrations:** the migration scripts ship inside `sellorita-common`, so any image that installed it
  can run them. Use `sellorita-migrate` (no arguments, which means `upgrade head`) as the command of the
  compose `migrate` service and of `k8s/jobs/db-migrate.yaml`. It reads the same `POSTGRES_*` env vars
  as the services and exits non-zero on failure.
* **API image vs worker image:** the API publishes tasks by name (`send_task`) and never imports
  `worker`, so `Dockerfile.api` needs only `libs/common` + `services/api_gateway`. Start command:
  `uvicorn app.main:app --host 0.0.0.0 --port 8000` (the app writes its own JSON access log).
  Probes: liveness `GET /api/v1/healthz`, readiness `GET /api/v1/readyz` (503 while Postgres, Redis or
  MinIO is unreachable).
* **Worker image:** command `celery -A worker.celery_app worker -Q campaigns --concurrency=1`
  (workdir = the directory containing the `worker` package). It needs `ffmpeg`/`ffprobe` on `PATH` (or
  `FFMPEG_BINARY`) and a writable `RENDER_SCRATCH_DIR` (default: the system temp dir, e.g. `/tmp`).
  One render per pod (`--concurrency=1`) lines up with KEDA scaling on queue length.
* **Beat:** same image, command `celery -A worker.celery_app beat`, `replicas: 1`, never autoscaled.
  It writes its small `celerybeat-schedule` state file into `RENDER_SCRATCH_DIR`.
* **Worker shutdown:** tasks are acknowledged late, so a pod killed mid-render gets its task redelivered.
  Give pods a `terminationGracePeriodSeconds` longer than one render (60s+) so a scale-down usually
  finishes the current task first.
* **KEDA:** tasks land in the Redis list `campaigns` on DB 0 (`LLEN campaigns` = queue length).
* **Presigned URLs through the ingress:** route `/sellorita-media/` to MinIO with the path and `Host`
  unchanged, and set `MINIO_PUBLIC_ENDPOINT` to the ingress origin (see DESIGN.md 4.2).
* **MinIO image:** `minio/minio` is gone from Docker Hub and quay.io. Use the pinned Pigsty build from
  `docker/docker-compose.dev.yml` (`pgsty/minio`, `pgsty/mc`). It has `bash` but no `curl`/`grep`; the
  compose healthcheck shows a probe that works. In K8s, use an `httpGet` probe on
  `/minio/health/live` and `/minio/health/ready`, port 9000.


Each service image installs the shared library first, then the service requirements:

```dockerfile
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common
COPY services/api_gateway/requirements.txt .
RUN pip install -r requirements.txt
COPY services/api_gateway/app ./app
```
