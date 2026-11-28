"""Request middleware: request IDs and one structured access-log line per request.

Every request gets a `request_id` (reused from an incoming `X-Request-ID` header, e.g.
set by nginx, or generated). It is bound to the logging context, so every log line
written while handling the request carries it, and it is returned in the response
header so a user-reported error can be found in Kibana.

The access line logs `route` as the *template* (`/api/v1/shops/{shop_id}`), not the raw
path, so Kibana can aggregate request rate and latency per endpoint.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from common.logger import bind_context, clear_context, get_logger

log = get_logger("app.access")

# K8s probes hit these every few seconds; log them at DEBUG to keep INFO logs useful.
_QUIET_PATHS = frozenset({"/api/v1/healthz", "/api/v1/readyz"})


def route_template(scope: dict) -> str:
    """Full route template for the matched endpoint, e.g. `/api/v1/shops/{shop_id}`.

    With nested routers, the matched route object only knows its own path
    (`/shops/{shop_id}`), so the router prefix is recovered from the real request path.
    Unmatched requests (404 scans like `/wp-admin`) are grouped under one value, so
    random URLs cannot create unbounded distinct values in Elasticsearch.
    """
    path: str = scope["path"]
    template = getattr(scope.get("route"), "path", None)
    if template is None:
        return "<unmatched>"
    params = {k: str(v) for k, v in scope.get("path_params", {}).items()}
    try:
        rendered = template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template
    if path.endswith(rendered):
        return path[: len(path) - len(rendered)] + template
    return template


def install_request_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_logging(request: Request, call_next) -> Response:
        clear_context()  # never leak context from a previous request on this worker
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        bind_context(request_id=request_id)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - start) * 1000),
            )
            response = JSONResponse(
                {"detail": "Internal server error", "request_id": request_id}, status_code=500
            )

        fields = {
            "method": request.method,
            "route": route_template(request.scope),
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000),
        }
        if request.url.path in _QUIET_PATHS and response.status_code < 400:
            log.debug("http_request", **fields)
        else:
            log.info("http_request", **fields)

        response.headers["X-Request-ID"] = request_id
        clear_context()
        return response
