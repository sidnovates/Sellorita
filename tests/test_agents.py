"""LangGraph pipeline + Celery task tests (MOCK_MODE=true).

Tasks run in-process with `.apply()` (Celery's eager call: no broker, no worker), against
the real test database and test bucket from conftest.py and a real FFmpeg encode.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from redis.exceptions import RedisError

from common.db import session_scope
from common.logger import configure_logging
from common.models import Campaign, ProductOffer, Shop
from common.schemas import (
    CampaignFormat,
    CampaignStatus,
    CampaignTrigger,
    ScheduleFrequency,
)
from common.settings import get_settings
from worker import tasks
from worker.agents import intelligence
from worker.graph import build_graph
from worker.renderers.mock_media import probe
from worker.tasks import run_campaign_pipeline, schedule_due_campaigns

# --- Fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    """Set env vars for one test and rebuild the cached Settings around it."""

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()

    apply(mock_mode="true", mock_reel_seconds=1, render_scratch_dir=tmp_path / "scratch")
    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def shop(clean_tables, test_bucket, settings_env) -> Shop:
    with session_scope() as session:
        shop = Shop(
            name="Chai Corner",
            category="cafe",
            city="Bengaluru",
            brand_primary_color="#7B3F00",
            brand_secondary_color="#F5DEB3",
            brand_font="Poppins",
            tone="friendly",
            schedule_frequency=ScheduleFrequency.DAILY,
            products=[
                ProductOffer(title="Masala chai", discount_text="20% off", minio_image_key="a.jpg"),
                ProductOffer(title="Samosa", minio_image_key="b.jpg"),
            ],
        )
        session.add(shop)
    return shop


def make_campaign(shop: Shop, fmt: CampaignFormat, **fields) -> str:
    with session_scope() as session:
        campaign = Campaign(id=uuid.uuid4(), shop_id=shop.id, format=fmt, **fields)
        session.add(campaign)
    return str(campaign.id)


def run(campaign_id: str) -> Campaign:
    result = run_campaign_pipeline.apply(args=[campaign_id])
    assert result.successful(), result.traceback
    with session_scope() as session:
        return session.get(Campaign, uuid.UUID(campaign_id))


# --- Pure units -------------------------------------------------------------------------------


def test_graph_has_design_nodes():
    nodes = set(build_graph().compile().get_graph().nodes)
    assert {"calendar", "news", "brand", "creative_director", "reel", "static_post",
            "tweet", "qa_critic", "mark_failed"} <= nodes  # fmt: skip


def test_calendar_picks_next_festival_or_weekend():
    assert intelligence.calendar_context_for(date(2026, 11, 1)) == {
        "date": "2026-11-01",
        "upcoming_festival": "Diwali",
        "days_away": 7,
    }
    # Nothing in the table within 45 days of this date: fall back to the next Saturday.
    ctx = intelligence.calendar_context_for(date(2026, 6, 10))  # a Wednesday
    assert ctx["upcoming_festival"] == "Weekend" and ctx["days_away"] == 3


# --- Pipeline outcomes ------------------------------------------------------------------------


def test_tweet_reaches_pending_review_with_llmops_metadata(shop):
    campaign = run(make_campaign(shop, CampaignFormat.TWEET))

    assert campaign.status is CampaignStatus.PENDING_REVIEW
    assert campaign.minio_output_key is None
    assert "Chai Corner" in campaign.caption
    assert len(campaign.caption) + 1 + len(" ".join(campaign.hashtags)) <= 280
    assert campaign.retry_count == 0 and campaign.error_message is None

    meta = campaign.agent_metadata
    assert {u["agent"] for u in meta["llm_usage"]} == {"CreativeDirectorAgent", "CopyTweetAgent"}
    assert all(u["input_tokens"] > 0 for u in meta["llm_usage"])
    assert meta["prompt_versions"]["CreativeDirectorAgent"] == "mock"
    assert meta["brand_catalog"]["offer"] == "Masala chai - 20% off"
    assert meta["calendar_context"]["date"] == datetime.now(UTC).date().isoformat()


def test_featured_product_rotates_between_campaigns(shop):
    first = run(make_campaign(shop, CampaignFormat.TWEET)).agent_metadata["brand_catalog"]
    second = run(make_campaign(shop, CampaignFormat.TWEET)).agent_metadata["brand_catalog"]
    assert first["offer"].startswith("Masala chai")
    assert second["offer"] == "Samosa"


def test_reel_uploads_playable_1080x1920_mp4_and_cleans_scratch(shop, test_bucket, tmp_path):
    campaign_id = make_campaign(shop, CampaignFormat.REEL)
    campaign = run(campaign_id)

    assert campaign.status is CampaignStatus.PENDING_REVIEW
    assert campaign.minio_output_key == f"shops/{shop.id}/outputs/{campaign_id}.mp4"
    local = tmp_path / "check.mp4"
    test_bucket.download_file(campaign.minio_output_key, local)
    assert probe(local) == {"codec_name": "h264", "width": 1080, "height": 1920}
    assert not (tmp_path / "scratch" / campaign_id).exists()


def test_static_post_uploads_1080x1080_png(shop, test_bucket, tmp_path):
    campaign = run(make_campaign(shop, CampaignFormat.STATIC_POST))
    assert campaign.minio_output_key.endswith(".png")
    local = tmp_path / "check.png"
    test_bucket.download_file(campaign.minio_output_key, local)
    assert probe(local) == {"codec_name": "png", "width": 1080, "height": 1080}


# --- QA retry loop and failure paths -------------------------------------------------------------


def test_qa_rejection_retries_with_feedback_then_passes(shop, settings_env):
    settings_env(mock_qa_failures=1)
    campaign = run(make_campaign(shop, CampaignFormat.TWEET))

    assert campaign.status is CampaignStatus.PENDING_REVIEW
    assert campaign.retry_count == 1
    assert campaign.caption.startswith("[Revised]")  # the director saw the feedback
    agents = [u["agent"] for u in campaign.agent_metadata["llm_usage"]]
    assert agents.count("CreativeDirectorAgent") == 2


def test_qa_retries_are_bounded_then_campaign_fails(shop, settings_env):
    settings_env(mock_qa_failures=99)
    campaign = run(make_campaign(shop, CampaignFormat.REEL))

    assert campaign.status is CampaignStatus.FAILED
    assert campaign.retry_count == 2  # 3 attempts in total, then mark_failed
    assert "after 2 retries" in campaign.error_message
    assert campaign.minio_output_key is None  # rejected media is not offered for review


def test_render_failure_is_retried_then_fails_with_reason(shop, settings_env):
    settings_env(ffmpeg_binary="definitely-not-ffmpeg")
    campaign = run(make_campaign(shop, CampaignFormat.REEL))
    assert campaign.status is CampaignStatus.FAILED
    assert "not found" in campaign.error_message
    assert campaign.retry_count == 2


def test_fatal_pipeline_error_marks_failed_instead_of_stuck(shop, monkeypatch):
    class Broken:
        def invoke(self, _):
            raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "get_pipeline", lambda: Broken())
    campaign = run(make_campaign(shop, CampaignFormat.TWEET))
    assert campaign.status is CampaignStatus.FAILED
    assert "boom" in campaign.error_message


def _flaky_pipeline(monkeypatch, failures: int) -> list[int]:
    """get_pipeline() whose first `failures` runs hit a (simulated) DB outage."""
    from sqlalchemy.exc import OperationalError

    real, calls = tasks.get_pipeline, []

    class Flaky:
        def invoke(self, state):
            calls.append(1)
            if len(calls) <= failures:
                raise OperationalError("SELECT 1", {}, ConnectionError("server closed"))
            return real().invoke(state)

    monkeypatch.setattr(tasks, "get_pipeline", lambda: Flaky())
    return calls


def test_transient_db_error_is_retried_not_failed(shop, monkeypatch):
    calls = _flaky_pipeline(monkeypatch, failures=1)
    campaign_id = make_campaign(shop, CampaignFormat.TWEET)
    run_campaign_pipeline.apply(args=[campaign_id])  # eager mode runs retries inline
    with session_scope() as session:
        assert session.get(Campaign, uuid.UUID(campaign_id)).status is CampaignStatus.PENDING_REVIEW
    assert len(calls) == 2


def test_exhausted_retries_mark_campaign_failed(shop, monkeypatch):
    calls = _flaky_pipeline(monkeypatch, failures=99)
    campaign_id = make_campaign(shop, CampaignFormat.TWEET)
    run_campaign_pipeline.apply(args=[campaign_id])
    with session_scope() as session:
        campaign = session.get(Campaign, uuid.UUID(campaign_id))
        assert campaign.status is CampaignStatus.FAILED
        assert "OperationalError" in campaign.error_message
    assert len(calls) == 6  # first attempt + max_retries=5


def test_optional_agents_degrade_instead_of_failing(shop, monkeypatch):
    def no_news(*_):
        raise TimeoutError("news API down")

    monkeypatch.setattr(intelligence, "mock_trends", no_news)
    monkeypatch.setattr(intelligence, "today", lambda: date(2031, 1, 1))  # cache miss
    campaign = run(make_campaign(shop, CampaignFormat.TWEET))
    assert campaign.status is CampaignStatus.PENDING_REVIEW
    assert campaign.agent_metadata["news_trend_context"]["safe_to_use"] is False


def test_pipeline_works_without_redis_cache(shop, monkeypatch):
    class DeadRedis:
        def get(self, *_):
            raise RedisError("connection refused")

        set = get

    monkeypatch.setattr("worker.cache.get_cache", lambda: DeadRedis())
    assert run(make_campaign(shop, CampaignFormat.TWEET)).status is CampaignStatus.PENDING_REVIEW


# --- Idempotency ---------------------------------------------------------------------------------


def test_finished_campaign_is_not_regenerated_on_redelivery(shop):
    campaign_id = make_campaign(shop, CampaignFormat.TWEET, status=CampaignStatus.APPROVED)
    assert run_campaign_pipeline.apply(args=[campaign_id]).get() == "APPROVED"
    with session_scope() as session:
        assert session.get(Campaign, uuid.UUID(campaign_id)).caption is None


def test_unknown_campaign_is_ignored(shop):
    assert run_campaign_pipeline.apply(args=[str(uuid.uuid4())]).get() == "NOT_FOUND"


# --- Scheduler -----------------------------------------------------------------------------------


@pytest.fixture
def enqueued(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        run_campaign_pipeline, "apply_async", lambda args, task_id: calls.append(task_id)
    )
    return calls


def test_scheduler_creates_one_campaign_per_due_shop(shop, enqueued):
    assert schedule_due_campaigns.apply().get() == 1
    assert schedule_due_campaigns.apply().get() == 0  # just scheduled: not due again yet

    with session_scope() as session:
        (campaign,) = session.query(Campaign).all()
        assert str(campaign.id) == enqueued[0]
        assert campaign.trigger is CampaignTrigger.SCHEDULED
        assert campaign.format is CampaignFormat.REEL  # first in the rotation
        assert campaign.status is CampaignStatus.QUEUED


def test_scheduler_respects_frequency_and_rotates_format(shop, enqueued):
    now = datetime.now(UTC)
    make_campaign(shop, CampaignFormat.REEL, trigger=CampaignTrigger.SCHEDULED,
                  scheduled_for=now - timedelta(hours=25))  # fmt: skip
    with session_scope() as session:
        weekly = Shop(name="Slow Salon", category="salon", city="Pune",
                      brand_primary_color="#000000", brand_secondary_color="#FFFFFF",
                      brand_font="Inter", tone="premium",
                      schedule_frequency=ScheduleFrequency.TWICE_WEEKLY)  # fmt: skip
        session.add(weekly)
        session.flush()
        session.add(Campaign(shop_id=weekly.id, format=CampaignFormat.REEL,
                             trigger=CampaignTrigger.SCHEDULED,
                             scheduled_for=now - timedelta(days=1)))  # fmt: skip

    assert schedule_due_campaigns.apply().get() == 1  # only the DAILY shop is due
    with session_scope() as session:
        newest = session.get(Campaign, uuid.UUID(enqueued[0]))
        assert newest.shop_id == shop.id
        assert newest.format is CampaignFormat.STATIC_POST  # second in the rotation


# --- Observability -------------------------------------------------------------------------------


def test_every_agent_logs_a_structured_line(shop, capsys):
    configure_logging(service_name="agent-worker", json_logs=True)
    campaign_id = make_campaign(shop, CampaignFormat.STATIC_POST)
    capsys.readouterr()
    run(campaign_id)

    records = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    nodes = [r for r in records if r["event"] == "agent_node_completed"]
    assert {r["agent_name"] for r in nodes} == {
        "CalendarCulturalAgent", "NewsTrendAgent", "BrandCatalogAgent",
        "CreativeDirectorAgent", "StaticPostAgent", "QACriticAgent",
    }  # fmt: skip
    assert all(r["campaign_id"] == campaign_id and "duration_ms" in r for r in nodes)
    assert all(r["service"] == "agent-worker" for r in nodes)

    (done,) = [r for r in records if r["event"] == "campaign_completed"]
    assert done["status"] == "PENDING_REVIEW" and done["total_input_tokens"] > 0
