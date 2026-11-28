"""Celery configuration shared by the API (producer) and the agent worker (consumer).

The API never imports worker code. It publishes a message naming the task by string
(`send_task(RUN_CAMPAIGN_TASK, ...)`), and whichever worker consumes the `campaigns`
queue runs it. So the API image does not need LangGraph, Chromium or HyperFrames, and
the two services deploy independently.

Reliability settings (why each one matters for long LLM + render tasks):
* `task_acks_late`: the message is acknowledged only after the task *finishes*. If a
  worker pod is killed mid-render (KEDA scale-down, OOM), Redis redelivers the task.
* `task_reject_on_worker_lost`: same idea when the worker process itself dies.
* `worker_prefetch_multiplier=1`: a worker reserves one task at a time. With long
  tasks, prefetching would park queued work on a busy pod while new pods sit idle.
* `visibility_timeout`: how long Redis waits for an ack before redelivering. Must be
  longer than the slowest task, or a still-running render gets duplicated.
"""

from __future__ import annotations

from functools import lru_cache

from celery import Celery

from common.settings import get_settings

# Task names are a contract between the API and the worker; keep them here only.
RUN_CAMPAIGN_TASK = "worker.run_campaign_pipeline"
SCHEDULE_DUE_CAMPAIGNS_TASK = "worker.schedule_due_campaigns"


def create_celery_app(main: str) -> Celery:
    settings = get_settings()
    app = Celery(main, broker=settings.celery_broker_url, backend=settings.celery_result_backend)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],  # never unpickle messages from the broker
        timezone="UTC",
        enable_utc=True,
        task_default_queue=settings.celery_queue,  # KEDA watches this Redis list
        task_routes={RUN_CAMPAIGN_TASK: {"queue": settings.celery_queue}},
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"visibility_timeout": 3600},
        # The DB row is the source of truth for campaign status; stored results are only
        # for debugging, so they expire after a day.
        result_expires=86400,
        # Fail fast in the API when Redis is down instead of hanging the request.
        broker_connection_timeout=3,
        broker_connection_retry_on_startup=True,
        task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.5},
    )
    return app


@lru_cache
def get_celery_client() -> Celery:
    """Producer-side app used by the API gateway to publish tasks."""
    return create_celery_app("api-gateway")
