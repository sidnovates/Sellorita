"""Sellorita API gateway entrypoint.

Run locally from the repo root (backing services from docker/docker-compose.dev.yml):
    uvicorn app.main:app --app-dir services/api_gateway --reload

Interactive API docs: http://localhost:8000/api/v1/docs
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import install_request_logging
from app.routers import campaigns, health, shops
from common.db import get_async_engine
from common.logger import configure_logging, get_logger
from common.settings import get_settings

# Configure logging at import time: uvicorn sets up its own logging before importing
# the app, so doing it here replaces uvicorn's plain-text handlers with our JSON ones.
configure_logging(service_name="api-gateway")
# Our middleware writes the structured access line (route template, duration,
# request_id); uvicorn's own plain-text access line would only duplicate it.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
log = get_logger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    log.info("service_started", app_env=settings.app_env, mock_mode=settings.mock_mode)
    yield
    # Close pooled DB connections cleanly so Postgres does not log aborted connections
    # every time a pod is replaced during a RollingUpdate.
    await get_async_engine().dispose()
    log.info("service_stopped")


app = FastAPI(
    title="Sellorita API Gateway",
    version="0.1.0",
    lifespan=lifespan,
    # Everything under /api/v1, so nginx can proxy one prefix to this service.
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=None,
    openapi_url=f"{API_PREFIX}/openapi.json",
)
install_request_logging(app)

app.include_router(health.router, prefix=API_PREFIX)
app.include_router(shops.router, prefix=API_PREFIX)
app.include_router(campaigns.router, prefix=API_PREFIX)
