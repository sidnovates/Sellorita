"""Alembic environment: how Alembic connects to the database and finds the models.

Alembic imports this file for every command. It
1. gets the database URL from Settings (env vars), never from alembic.ini;
2. points `target_metadata` at our models, which is what `--autogenerate` diffs against
   the live database;
3. runs the migrations inside one transaction (PostgreSQL supports transactional DDL, so
   a failed migration leaves the schema unchanged).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from common import models  # noqa: F401  (registers every table on Base.metadata)
from common.logger import configure_logging, get_logger
from common.models import Base
from common.settings import get_settings

config = context.config
target_metadata = Base.metadata

# Tests can hand over a ready connection or a URL via `config.attributes`; otherwise the
# URL comes from the environment like every other service.
if "connection" not in config.attributes:
    configure_logging(service_name="db-migrate")
log = get_logger("alembic.env")


def _database_url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url_sync


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,  # also detect column type changes (e.g. String(60) -> String(80))
        **kwargs,
    )


def run_migrations_offline() -> None:
    """`--sql` mode: print the SQL instead of running it (useful to review a migration)."""
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_with(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with(connection)
        return

    # NullPool: a migration is a one-off process, so there is no point keeping a pool.
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        log.info("migration_started", database=engine.url.database, host=engine.url.host)
        _run_with(conn)
    log.info("migration_finished")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
