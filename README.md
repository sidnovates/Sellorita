# Sellorita

Autonomous multi-agent social media content platform for local businesses (CSE 816 project).
Design: [Plans/system_architecture.md](Plans/system_architecture.md) · Build plan: [Plans/team_execution_devops_build_plan.md](Plans/team_execution_devops_build_plan.md)

## Repository layout

| Path | Owner | Contents |
| --- | --- | --- |
| `libs/common/` | Dev A | Shared package: settings, JSON logger, schemas, DB models + Alembic migrations (storage added next) |
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

Then open http://localhost:8000/api/v1/healthz. The MinIO console is at http://localhost:9001
(user `sellorita`, password `sellorita_dev`, local-only defaults from `.env.example`).

`tests/test_db.py` needs Postgres running. It creates and drops its own `sellorita_test`
database, and skips with a message if Postgres is unreachable.

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
