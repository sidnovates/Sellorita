# Sellorita

Autonomous multi-agent social media content platform for local businesses (CSE 816 project).
Design: [Plans/system_architecture.md](Plans/system_architecture.md) · Build plan: [Plans/team_execution_devops_build_plan.md](Plans/team_execution_devops_build_plan.md)

## Repository layout

| Path | Owner | Contents |
| --- | --- | --- |
| `libs/common/` | Dev A | Shared package: settings, JSON logger, schemas (models/storage added later) |
| `services/api_gateway/` | Dev A | FastAPI app (`app` package) |
| `services/agent_worker/` | Dev A | Celery + LangGraph worker (`worker` package) |
| `tests/` | Dev A | pytest suite |
| `docker/`, `k8s/`, `ansible/`, `Jenkinsfile` | Dev B | Infrastructure and CI/CD |

## Local development (Python 3.11+)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` on Linux/macOS
pip install -r requirements-dev.txt
copy .env.example .env          # `cp` on Linux/macOS
pytest
uvicorn app.main:app --app-dir services/api_gateway --reload
```

Then open http://localhost:8000/api/v1/healthz.

## Docker notes for Dev B

Each service image installs the shared library first, then the service requirements:

```dockerfile
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common
COPY services/api_gateway/requirements.txt .
RUN pip install -r requirements.txt
COPY services/api_gateway/app ./app
```
