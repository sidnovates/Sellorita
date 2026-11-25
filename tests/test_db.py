"""Database layer tests: models, the Alembic migration, and the session helpers.

The integration tests need PostgreSQL from docker/docker-compose.dev.yml (or the CI
service container). They create a throwaway `sellorita_test` database, so they never
touch your dev data. If PostgreSQL is unreachable they are skipped with a clear reason.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import URL, create_engine, inspect, make_url, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError

from common import db
from common.migrate import ALEMBIC_INI
from common.models import Base, Campaign, ProductOffer, Shop
from common.schemas import CampaignFormat, CampaignStatus, CampaignTrigger, ScheduleFrequency
from common.settings import Settings, get_settings

TEST_DB = "sellorita_test"

# --- Unit tests (no database) ------------------------------------------------------------


def test_metadata_has_design_tables_with_named_constraints():
    assert set(Base.metadata.tables) == {"shops", "products_offers", "campaigns"}
    campaigns = Base.metadata.tables["campaigns"]
    fk = next(iter(campaigns.foreign_keys))
    assert fk.ondelete == "CASCADE"
    assert campaigns.c.status.nullable is False
    assert campaigns.c.minio_output_key.nullable is True


def test_enum_columns_store_values_not_member_names():
    status_type = Base.metadata.tables["campaigns"].c.status.type
    assert set(status_type.enums) == {s.value for s in CampaignStatus}
    assert status_type.native_enum is False


def make_shop(**overrides) -> Shop:
    fields = dict(
        name="Chai Corner",
        category="cafe",
        city="Bengaluru",
        brand_primary_color="#7B3F00",
        brand_secondary_color="#F5DEB3",
        brand_font="Poppins",
        tone="friendly",
        schedule_frequency=ScheduleFrequency.DAILY,
    )
    return Shop(**(fields | overrides))


# --- Integration fixtures ------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_db_url() -> URL:
    """Create an empty `sellorita_test` database, drop it after the module."""
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


def alembic_config(url: URL) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = url.render_as_string(hide_password=False)
    return cfg


@pytest.fixture(scope="module")
def migrated_db(test_db_url: URL):
    """Run the real migrations, then point common.db at the test database."""
    command.upgrade(alembic_config(test_db_url), "head")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("POSTGRES_DB", TEST_DB)
        get_settings.cache_clear()
        db.reset_engines()
        yield test_db_url
        db.get_sync_engine().dispose()
    get_settings.cache_clear()
    db.reset_engines()


@pytest.fixture
def clean_tables(migrated_db):
    yield
    with db.session_scope() as session:
        session.execute(text("TRUNCATE shops CASCADE"))


# --- Integration tests ---------------------------------------------------------------------


def test_migration_matches_models(migrated_db):
    """If someone edits models.py without writing a migration, this fails in CI."""
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    engine.dispose()
    assert diff == [], (
        f"models.py and migrations differ; run `sellorita-migrate revision --autogenerate`: {diff}"
    )


def test_insert_shop_product_campaign_with_defaults(clean_tables):
    with db.session_scope() as session:
        shop = make_shop()
        shop.products.append(ProductOffer(title="Masala chai", minio_image_key="k.jpg"))
        session.add(shop)
        session.flush()  # sends INSERTs; server defaults come back via RETURNING
        session.add(Campaign(shop_id=shop.id, format=CampaignFormat.REEL))
        shop_id = shop.id

    with db.session_scope() as session:
        campaign = session.scalars(select(Campaign).where(Campaign.shop_id == shop_id)).one()
        assert isinstance(campaign.id, uuid.UUID)
        assert campaign.status is CampaignStatus.QUEUED
        assert campaign.trigger is CampaignTrigger.MANUAL
        assert campaign.retry_count == 0
        assert campaign.hashtags == [] and campaign.agent_metadata == {}
        assert campaign.created_at.tzinfo is not None
        assert campaign.shop.products[0].is_active is True


def test_jsonb_round_trip_and_updated_at(clean_tables):
    with db.session_scope() as session:
        shop = make_shop()
        campaign = Campaign(shop=shop, format=CampaignFormat.TWEET)
        session.add(campaign)
        session.flush()
        created = campaign.updated_at
        campaign.agent_metadata = {"llm_usage": [{"agent": "CopyTweetAgent", "input_tokens": 12}]}
        campaign.hashtags = ["#Diwali", "#ChaiTime"]
        campaign.status = CampaignStatus.PENDING_REVIEW
        session.flush()
        session.refresh(campaign)
        assert campaign.updated_at >= created
        cid = campaign.id

    with db.session_scope() as session:
        row = session.execute(
            text(
                "SELECT agent_metadata -> 'llm_usage' -> 0 ->> 'agent', status "
                "FROM campaigns WHERE id = :id"
            ),
            {"id": cid},
        ).one()
        assert tuple(row) == ("CopyTweetAgent", "PENDING_REVIEW")


def test_check_constraint_rejects_unknown_status(clean_tables):
    with db.session_scope() as session:
        shop = make_shop()
        session.add(shop)
    with pytest.raises(IntegrityError, match="ck_campaigns_status"):
        with db.session_scope() as session:
            session.execute(
                text(
                    "INSERT INTO campaigns (id, shop_id, format, status) "
                    "VALUES (:id, :shop, 'REEL', 'DONE')"
                ),
                {"id": uuid.uuid4(), "shop": shop.id},
            )


def test_python_side_rejects_unknown_status(clean_tables):
    with pytest.raises(StatementError, match="DONE"):
        with db.session_scope() as session:
            shop = make_shop()
            session.add(Campaign(shop=shop, format=CampaignFormat.REEL, status="DONE"))


def test_deleting_shop_cascades_in_database(clean_tables):
    with db.session_scope() as session:
        shop = make_shop()
        shop.products.append(ProductOffer(title="Samosa", minio_image_key="s.jpg"))
        session.add_all([shop, Campaign(shop=shop, format=CampaignFormat.STATIC_POST)])
    with db.session_scope() as session:
        session.execute(text("DELETE FROM shops"))  # raw SQL: proves the FK, not the ORM
    with db.session_scope() as session:
        assert session.scalar(text("SELECT count(*) FROM campaigns")) == 0
        assert session.scalar(text("SELECT count(*) FROM products_offers")) == 0


def test_session_scope_rolls_back_on_error(clean_tables):
    with pytest.raises(RuntimeError):
        with db.session_scope() as session:
            session.add(make_shop(name="Ghost Shop"))
            session.flush()
            raise RuntimeError("task crashed")
    with db.session_scope() as session:
        assert session.scalar(select(Shop).where(Shop.name == "Ghost Shop")) is None


def test_async_session_and_ping(clean_tables):
    with db.session_scope() as session:
        session.add(make_shop(name="Async Bakery"))

    async def run() -> tuple[bool, str]:
        try:
            ok = await db.ping_db()
            async for session in db.get_db_session():  # the FastAPI dependency
                name = await session.scalar(select(Shop.name))
            return ok, name
        finally:
            await db.get_async_engine().dispose()

    assert asyncio.run(run()) == (True, "Async Bakery")


def test_downgrade_to_base_and_back(migrated_db):
    cfg = alembic_config(migrated_db)
    engine = create_engine(migrated_db)
    db.get_sync_engine().dispose()  # release pooled connections holding table locks
    try:
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(cfg, "head")
        assert {"shops", "products_offers", "campaigns"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
