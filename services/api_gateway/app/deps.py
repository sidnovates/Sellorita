"""FastAPI dependencies: the things endpoints need, provided per request.

Endpoints declare what they need (`session: DbSession`) instead of importing globals.
Tests swap any of them with `app.dependency_overrides[...]`, e.g. a fake dispatcher, so
they can check "a task was enqueued" without a running worker.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from celery import Celery
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from common.db import get_db_session
from common.queue import RUN_CAMPAIGN_TASK, get_celery_client
from common.storage import Storage, get_storage


class CampaignDispatcher:
    """Publishes campaign jobs to the Celery `campaigns` queue."""

    def __init__(self, celery: Celery) -> None:
        self.celery = celery

    async def dispatch(self, campaign_id: uuid.UUID) -> None:
        # send_task is a blocking network call to Redis; run it in a thread so the
        # event loop keeps serving other requests. The campaign id doubles as the
        # Celery task id, so logs, the DB row and the task share one identifier.
        await asyncio.to_thread(
            self.celery.send_task,
            RUN_CAMPAIGN_TASK,
            args=[str(campaign_id)],
            task_id=str(campaign_id),
        )


def get_dispatcher() -> CampaignDispatcher:
    return CampaignDispatcher(get_celery_client())


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
StorageDep = Annotated[Storage, Depends(get_storage)]
DispatcherDep = Annotated[CampaignDispatcher, Depends(get_dispatcher)]
