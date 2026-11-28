"""Liveness and readiness probes.

Kubernetes uses them differently:
* **liveness** (`/healthz`): "is the process stuck?" If it fails, K8s *restarts* the pod.
  It must not check dependencies: a Postgres outage would otherwise restart every API
  pod in a loop, which fixes nothing.
* **readiness** (`/readyz`): "can this pod serve traffic right now?" If it fails, K8s
  only *removes the pod from the Service* until it passes again. During a RollingUpdate,
  a new pod gets traffic only once it is ready, which is what makes it zero-downtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from common.db import ping_db
from common.logger import get_logger
from common.settings import get_settings
from common.storage import get_storage

router = APIRouter(tags=["health"])
log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 3.0


async def check_db() -> bool:
    return await ping_db()


async def check_redis() -> bool:
    client = Redis.from_url(
        get_settings().celery_broker_url, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def check_minio() -> bool:
    return await asyncio.to_thread(get_storage().ping)


# Module-level so tests can replace a single check.
CHECKS: dict[str, Callable[[], Awaitable[bool]]] = {
    "db": check_db,
    "redis": check_redis,
    "minio": check_minio,
}


async def _run(name: str) -> bool:
    try:
        return await asyncio.wait_for(CHECKS[name](), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # any failure (timeout, refused, auth) means "down"
        log.warning("readiness_check_failed", dependency=name, error=repr(exc))
        return False


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and its event loop is responding."""
    return {"status": "ok"}


@router.get("/readyz", responses={503: {"description": "A dependency is down"}})
async def readyz() -> JSONResponse:
    """Readiness: PostgreSQL, Redis and MinIO are all reachable (checked in parallel)."""
    names = list(CHECKS)
    results = await asyncio.gather(*(_run(n) for n in names))
    body = {name: ("up" if ok else "down") for name, ok in zip(names, results, strict=True)}
    ready = all(results)
    return JSONResponse({"status": "ok" if ready else "degraded", **body}, 200 if ready else 503)
