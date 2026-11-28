"""Redis cache for intelligence results (DESIGN.md 4.3, Redis DB 2).

Calendar and trend context is the same for every shop in a city on a given day, so one
lookup per day is shared by every campaign. In Phase 2 these are LLM/news API calls, so
the cache directly saves money and latency.

The cache is an optimisation, never a dependency: if Redis is unreachable the value is
computed directly and the pipeline carries on.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from common.logger import get_logger
from common.settings import get_settings

log = get_logger(__name__)


@lru_cache
def get_cache() -> Redis:
    return Redis.from_url(
        get_settings().redis_cache_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


def calendar_key(day: str) -> str:
    return f"cache:calendar:{day}"


def trends_key(city: str, day: str) -> str:
    return f"cache:trends:{city.strip().lower().replace(' ', '-')}:{day}"


def cached_json(key: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Return the cached value for `key`, or compute it, store it with the TTL and return it."""
    ttl = get_settings().intel_cache_ttl_seconds
    try:
        hit = get_cache().get(key)
        if hit is not None:
            log.debug("cache_hit", cache_key=key)
            return json.loads(hit)
    except RedisError as exc:
        log.warning("cache_unavailable", cache_key=key, error=repr(exc))
        return compute()

    value = compute()
    try:
        get_cache().set(key, json.dumps(value), ex=ttl)
    except RedisError as exc:
        log.warning("cache_unavailable", cache_key=key, error=repr(exc))
    return value
