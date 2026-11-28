"""Celery tasks: the campaign pipeline and the periodic scheduler.

The task is the boundary between "a message in Redis" and "a LangGraph run". It owns the
database writes for a campaign: the graph nodes compute, the task persists the outcome.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from celery import Task
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from common.db import session_scope
from common.logger import bind_context, clear_context, get_logger
from common.models import Campaign, ProductOffer, Shop
from common.queue import RUN_CAMPAIGN_TASK, SCHEDULE_DUE_CAMPAIGNS_TASK
from common.schemas import (
    CampaignFormat,
    CampaignState,
    CampaignStatus,
    CampaignTrigger,
    ScheduleFrequency,
)
from common.settings import get_settings
from worker.celery_app import celery_app
from worker.graph import get_pipeline

log = get_logger(__name__)

# Statuses in which a delivered task may (re)run the pipeline. PROCESSING is included:
# with late acks, a task whose worker died mid-run is delivered again and must finish.
RUNNABLE = (CampaignStatus.QUEUED, CampaignStatus.PROCESSING)


# Infrastructure errors that are usually temporary (Postgres restarting, a network blip).
# The task is retried with exponential backoff instead of failing the campaign.
TRANSIENT_ERRORS = (OperationalError, RedisConnectionError)


class CampaignTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        """Runs once retries are exhausted (or on a non-retryable crash): never leave a
        campaign in QUEUED/PROCESSING, or the dashboard would show it spinning forever."""
        try:
            _mark_failed(args[0], f"Task failed: {exc!r}")
        except Exception:
            log.exception("campaign_mark_failed_error", campaign_id=args[0])


# --- Campaign pipeline ------------------------------------------------------------------------


@celery_app.task(
    name=RUN_CAMPAIGN_TASK,
    bind=True,
    base=CampaignTask,
    autoretry_for=TRANSIENT_ERRORS,
    max_retries=5,
    retry_backoff=2,  # 2s, 4s, 8s, ... between attempts
    retry_backoff_max=60,
    retry_jitter=True,  # randomise, so many failed tasks do not retry in lockstep
)
def run_campaign_pipeline(self, campaign_id: str) -> str:
    clear_context()
    bind_context(campaign_id=campaign_id)

    # 1. Claim the campaign. Idempotency: a duplicate delivery of a finished campaign
    #    (e.g. after a broker hiccup) must not regenerate it, so anything that is not
    #    QUEUED/PROCESSING is skipped.
    with session_scope() as session:
        campaign = session.get(Campaign, uuid.UUID(campaign_id), with_for_update=True)
        if campaign is None:
            log.warning("campaign_not_found")
            return "NOT_FOUND"
        if campaign.status not in RUNNABLE:
            log.info("campaign_skipped", status=campaign.status)
            return campaign.status.value
        campaign.status = CampaignStatus.PROCESSING
        shop_id, fmt = str(campaign.shop_id), campaign.format

    bind_context(shop_id=shop_id)
    redelivered = bool((self.request.delivery_info or {}).get("redelivered"))
    log.info("campaign_started", format=fmt, redelivered=redelivered)
    start = time.perf_counter()

    # 2. Run the graph.
    initial: CampaignState = {
        "campaign_id": campaign_id,
        "shop_id": shop_id,
        "target_format": fmt,
        "retry_count": 0,
        "llm_usage": [],
    }
    try:
        final = get_pipeline().invoke(initial)
    except TRANSIENT_ERRORS:
        raise  # let Celery retry the whole task (the campaign is still PROCESSING)
    except Exception as exc:
        # A fatal node error (e.g. shop deleted mid-run). Never leave it in PROCESSING.
        _mark_failed(campaign_id, f"Pipeline error: {exc!r}")
        log.exception("campaign_completed", status=CampaignStatus.FAILED,
                      duration_ms=_ms(start))  # fmt: skip
        return CampaignStatus.FAILED.value

    # 3. Persist the outcome.
    status = _persist(campaign_id, final)
    usage = final.get("llm_usage", [])
    log.info(
        "campaign_completed",
        status=status,
        format=fmt,
        retry_count=final.get("retry_count", 0),
        duration_ms=_ms(start),
        total_input_tokens=sum(u["input_tokens"] for u in usage),
        total_output_tokens=sum(u["output_tokens"] for u in usage),
        total_cost_usd=round(sum(u["cost_usd"] for u in usage), 6),
    )
    return status.value


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _persist(campaign_id: str, final: CampaignState) -> CampaignStatus:
    passed = bool(final.get("qa_passed"))
    status = CampaignStatus.PENDING_REVIEW if passed else CampaignStatus.FAILED
    brief = final.get("creative_brief") or {}
    usage = final.get("llm_usage", [])

    with session_scope() as session:
        campaign = session.get(Campaign, uuid.UUID(campaign_id))
        campaign.status = status
        campaign.caption = brief.get("caption")
        campaign.hashtags = brief.get("hashtags", [])
        campaign.minio_output_key = final.get("artifact_minio_key") if passed else None
        campaign.qa_feedback = final.get("qa_feedback") or None
        campaign.retry_count = final.get("retry_count", 0)
        campaign.error_message = None if passed else final.get("error")
        campaign.agent_metadata = {
            "calendar_context": final.get("calendar_context"),
            "news_trend_context": final.get("news_trend_context"),
            "brand_catalog": final.get("brand_catalog"),
            "creative_brief": brief,
            "llm_usage": usage,
            "prompt_versions": {u["agent"]: u["prompt_version"] for u in usage},
        }
        # Rotate products: the featured one goes to the back of the queue.
        featured = (final.get("brand_catalog") or {}).get("featured_product_id")
        if passed and featured:
            product = session.get(ProductOffer, uuid.UUID(featured))
            if product is not None:
                product.last_featured_at = datetime.now(UTC)
    return status


def _mark_failed(campaign_id: str, message: str) -> None:
    with session_scope() as session:
        campaign = session.get(Campaign, uuid.UUID(campaign_id))
        if campaign is not None:
            campaign.status = CampaignStatus.FAILED
            campaign.error_message = message


# --- Scheduler (Celery Beat) --------------------------------------------------------------------

INTERVALS = {
    ScheduleFrequency.DAILY: timedelta(days=1),
    ScheduleFrequency.TWICE_WEEKLY: timedelta(days=7) / 2,
    ScheduleFrequency.THRICE_WEEKLY: timedelta(days=7) / 3,
}
FORMAT_ROTATION = [CampaignFormat.REEL, CampaignFormat.STATIC_POST, CampaignFormat.TWEET]


def due_campaigns(now: datetime) -> list[uuid.UUID]:
    """Create one SCHEDULED campaign per shop that is due; return their ids (committed)."""
    # Beat ticks every N seconds, so "exactly 24h ago" is usually missed by a few
    # minutes. Allowing one tick of slack keeps a DAILY shop at the same time each day
    # instead of drifting later by up to one tick per day.
    slack = timedelta(seconds=get_settings().schedule_check_interval_seconds)
    last = (
        select(
            Campaign.shop_id,
            func.max(Campaign.scheduled_for).label("last_scheduled"),
            func.count().label("n_scheduled"),
        )
        .where(Campaign.trigger == CampaignTrigger.SCHEDULED)
        .group_by(Campaign.shop_id)
        .subquery()
    )
    query = (
        select(Shop, last.c.last_scheduled, last.c.n_scheduled)
        .outerjoin(last, last.c.shop_id == Shop.id)
        .order_by(Shop.created_at)
        # Lock the shop rows while deciding. If two schedulers ever ran at once (a
        # second Beat by mistake, or a manual run), the second skips locked shops
        # instead of creating duplicate campaigns.
        .with_for_update(of=Shop, skip_locked=True)
    )

    created: list[uuid.UUID] = []
    with session_scope() as session:
        for shop, last_scheduled, n_scheduled in session.execute(query):
            interval = INTERVALS[shop.schedule_frequency]
            if last_scheduled is not None and now - last_scheduled < interval - slack:
                continue
            campaign = Campaign(
                id=uuid.uuid4(),
                shop_id=shop.id,
                format=FORMAT_ROTATION[(n_scheduled or 0) % len(FORMAT_ROTATION)],
                trigger=CampaignTrigger.SCHEDULED,
                scheduled_for=now,
            )
            session.add(campaign)
            created.append(campaign.id)
    return created


@celery_app.task(name=SCHEDULE_DUE_CAMPAIGNS_TASK)
def schedule_due_campaigns() -> int:
    clear_context()
    created = due_campaigns(datetime.now(UTC))  # committed before enqueueing (see API)
    enqueued = 0
    for campaign_id in created:
        try:
            run_campaign_pipeline.apply_async(args=[str(campaign_id)], task_id=str(campaign_id))
            enqueued += 1
        except Exception as exc:
            _mark_failed(str(campaign_id), f"Could not enqueue task: {exc!r}")
            log.error("campaign_enqueue_failed", campaign_id=str(campaign_id), error=repr(exc))
    log.info("scheduled_campaigns", created=len(created), enqueued=enqueued)
    return enqueued
