"""Tests for the shared settings and logging utilities."""

import json

import pytest
from pydantic import ValidationError

from common.logger import bind_context, clear_context, configure_logging, get_logger
from common.schemas import CampaignFormat, CampaignState, CampaignStatus
from common.settings import Settings


def make_settings(**overrides) -> Settings:
    # _env_file=None: ignore any developer .env so tests are deterministic.
    return Settings(_env_file=None, **overrides)


def test_derived_urls_use_correct_drivers_and_redis_dbs():
    s = make_settings(postgres_password="p@ss/word", redis_host="redis")
    assert s.database_url.startswith("postgresql+asyncpg://sellorita:p%40ss%2Fword@")
    assert s.database_url_sync.startswith("postgresql+psycopg://")
    assert s.celery_broker_url == "redis://redis:6379/0"
    assert s.celery_result_backend == "redis://redis:6379/1"
    assert s.redis_cache_url == "redis://redis:6379/2"


def test_secrets_are_masked_when_printed():
    s = make_settings(postgres_password="super-secret")
    assert "super-secret" not in repr(s)
    assert "super-secret" not in str(s.model_dump())


def test_real_mode_without_api_key_fails_fast():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(mock_mode=False)


def test_prod_requires_storage_and_db_secrets():
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        make_settings(app_env="prod")


def test_logs_are_single_line_json_with_bound_context(capsys):
    configure_logging(service_name="agent-worker", level="INFO", json_logs=True)
    clear_context()
    bind_context(campaign_id="c-123", shop_id="s-456")
    get_logger("test").info(
        "agent_node_completed", agent_name="CalendarCulturalAgent", duration_ms=12
    )
    clear_context()

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "agent_node_completed"
    assert record["level"] == "INFO"
    assert record["service"] == "agent-worker"
    assert record["campaign_id"] == "c-123"
    assert record["shop_id"] == "s-456"
    assert record["duration_ms"] == 12
    assert record["timestamp"].endswith("Z")


def test_stdlib_library_logs_are_also_json(capsys):
    import logging

    configure_logging(service_name="api-gateway", level="INFO", json_logs=True)
    logging.getLogger("uvicorn.error").info(
        "Application startup complete.", extra={"color_message": "\x1b[1mstartup\x1b[0m"}
    )

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "Application startup complete."
    assert "color_message" not in record
    assert record["service"] == "api-gateway"
    assert record["logger"] == "uvicorn.error"


def test_campaign_state_minimal_construction():
    state: CampaignState = {
        "campaign_id": "c-1",
        "shop_id": "s-1",
        "target_format": CampaignFormat.TWEET,
    }
    assert state["target_format"] == "TWEET"  # StrEnum compares equal to its string
    assert CampaignStatus.FAILED == "FAILED"
