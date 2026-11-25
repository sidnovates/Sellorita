"""Celery application for the agent worker and the Beat scheduler.

Run from the repo root (backing services from docker/docker-compose.dev.yml):

    # Worker. On Windows use --pool=solo (Celery's default prefork pool needs fork()).
    celery --workdir services/agent_worker -A worker.celery_app worker -Q campaigns --pool=solo
    # Beat: exactly ONE instance, it only *publishes* the periodic task.
    celery --workdir services/agent_worker -A worker.celery_app beat

In the container (Linux) the default prefork pool is fine:
    celery -A worker.celery_app worker -Q campaigns --concurrency=1
"""

from __future__ import annotations

from pathlib import Path

from celery.signals import setup_logging, worker_process_init

from common import db
from common.logger import configure_logging
from common.queue import SCHEDULE_DUE_CAMPAIGNS_TASK, create_celery_app
from common.settings import get_settings

configure_logging(service_name="agent-worker")

settings = get_settings()
celery_app = create_celery_app("agent-worker")
celery_app.conf.update(
    include=["worker.tasks"],  # modules whose @task functions the worker registers
    # Keep our JSON logging; by default Celery replaces the root logger's handlers.
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    beat_schedule={
        "schedule-due-campaigns": {
            "task": SCHEDULE_DUE_CAMPAIGNS_TASK,
            "schedule": float(settings.schedule_check_interval_seconds),
        },
    },
    # Beat remembers when each entry last ran in a small file. Put it in scratch space,
    # which is writable in a read-only-rootfs container.
    beat_schedule_filename=str(Path(settings.render_scratch_dir) / "celerybeat-schedule"),
)


@setup_logging.connect
def _use_structlog(**_: object) -> None:
    """Connecting to this signal stops Celery from configuring logging itself."""
    configure_logging(service_name="agent-worker")


@worker_process_init.connect
def _fresh_db_pools(**_: object) -> None:
    """Each forked worker process must open its own DB connections: a connection pool
    inherited from the parent would share sockets between processes."""
    db.reset_engines()
