"""Sellorita API gateway entrypoint.

Run locally from the repo root:
    uvicorn app.main:app --app-dir services/api_gateway --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.logger import configure_logging, get_logger
from common.settings import get_settings

# Configure logging at import time: uvicorn sets up its own logging before importing
# the app, so doing it here replaces uvicorn's plain-text handlers with our JSON ones.
configure_logging(service_name="api-gateway")
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    log.info("service_started", app_env=settings.app_env, mock_mode=settings.mock_mode)
    yield
    log.info("service_stopped")


app = FastAPI(title="Sellorita API Gateway", version="0.1.0", lifespan=lifespan)


@app.get("/api/v1/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness probe: the process is up and serving requests."""
    return {"status": "ok"}
