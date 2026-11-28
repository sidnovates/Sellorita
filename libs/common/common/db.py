"""Database engines and sessions.

Two flavours, because the two services run differently:

* **API gateway (async):** FastAPI serves many requests on one event loop. While one
  request waits on PostgreSQL, the loop serves others, so the driver must be async
  (`asyncpg`). Use `get_db_session` as a FastAPI dependency.
* **Celery worker (sync):** each task runs in its own process and blocks while it runs.
  A plain sync driver (`psycopg`) is simpler there. Use `session_scope()`.

Engines are created lazily, once per process. Celery forks worker processes, and a
connection pool must not be shared across a fork, so nothing connects at import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from common.settings import get_settings

# pool_pre_ping: test each pooled connection before use, so a Postgres restart (e.g. a
# StatefulSet pod being rescheduled) gives one reconnect instead of a burst of errors.
_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}


# --- Async (API gateway) -------------------------------------------------------------------


@lru_cache
def get_async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        connect_args={"timeout": settings.db_connect_timeout_seconds},  # asyncpg's name
        **_ENGINE_OPTIONS,
    )


@lru_cache
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: after commit, objects keep their loaded values. Without it,
    # reading `shop.name` after commit would trigger a lazy reload, which async SQLAlchemy
    # cannot do implicitly (it raises MissingGreenlet).
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed when the request ends.

    Endpoints call `await session.commit()` themselves; anything not committed is
    rolled back when the session closes.
    """
    async with get_async_sessionmaker()() as session:
        yield session


# --- Sync (Celery worker, Alembic) ---------------------------------------------------------


@lru_cache
def get_sync_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url_sync,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},  # libpq's name
        **_ENGINE_OPTIONS,
    )


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Unit of work for worker code: commit on success, roll back on any exception.

    with session_scope() as session:
        campaign = session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.PROCESSING
    """
    session = get_sync_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Health ----------------------------------------------------------------------------------


async def ping_db() -> bool:
    """True if PostgreSQL answers `SELECT 1`. Used by the /readyz readiness probe."""
    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def reset_engines() -> None:
    """Forget cached engines (tests that point at a different database, or after fork)."""
    for factory in (
        get_async_sessionmaker,
        get_async_engine,
        get_sync_sessionmaker,
        get_sync_engine,
    ):
        factory.cache_clear()
