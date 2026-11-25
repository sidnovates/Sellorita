"""Shared pytest fixtures for integration tests.

They use the real backing services from docker/docker-compose.dev.yml (in CI: service
containers), but never your dev data:
* PostgreSQL: a throwaway `sellorita_test` database, migrated with the real Alembic
  scripts, emptied after each test;
* MinIO: a throwaway `sellorita-test` bucket.
If a service is unreachable, tests that need it are skipped with a reason.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from common import db
from common.migrate import ALEMBIC_INI
from common.settings import Settings, get_settings
from common.storage import get_storage

TEST_DB = "sellorita_test"
TEST_BUCKET = "sellorita-test"


def _refresh_cached_config() -> None:
    """Settings, engines and storage clients are cached per process; rebuild them
    after the test fixtures change environment variables."""
    get_settings.cache_clear()
    get_storage.cache_clear()
    db.reset_engines()


# --- PostgreSQL ------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_db_url() -> URL:
    """Create an empty `sellorita_test` database, drop it at the end of the run."""
    base_url = make_url(Settings().database_url_sync)
    admin = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},  # skip fast instead of hanging
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(
            "PostgreSQL not reachable; start it with "
            f"`docker compose -f docker/docker-compose.dev.yml up -d` ({exc.orig})"
        )

    yield base_url.set(database=TEST_DB)

    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture(scope="session")
def alembic_cfg(test_db_url: URL) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = test_db_url.render_as_string(hide_password=False)
    return cfg


@pytest.fixture(scope="session")
def migrated_db(test_db_url: URL, alembic_cfg: Config):
    """Run the real migrations, then point common.db at the test database."""
    command.upgrade(alembic_cfg, "head")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("POSTGRES_DB", TEST_DB)
        _refresh_cached_config()
        yield test_db_url
        db.get_sync_engine().dispose()
    _refresh_cached_config()


@pytest.fixture
def clean_tables(migrated_db):
    yield
    with db.session_scope() as session:
        session.execute(text("TRUNCATE shops CASCADE"))  # cascades to products, campaigns


# --- MinIO -----------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_bucket():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MINIO_BUCKET", TEST_BUCKET)
        _refresh_cached_config()
        storage = get_storage()
        try:
            storage.ensure_bucket()
        except (BotoCoreError, ClientError) as exc:
            pytest.skip(f"MinIO not reachable at {get_settings().minio_internal_endpoint} ({exc})")

        yield storage

        objects = storage.internal.list_objects_v2(Bucket=TEST_BUCKET).get("Contents", [])
        for obj in objects:
            storage.delete(obj["Key"])
        storage.internal.delete_bucket(Bucket=TEST_BUCKET)
    _refresh_cached_config()
