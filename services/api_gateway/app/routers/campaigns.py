"""Campaign generation (async, via Celery) and status polling."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DbSession, DispatcherDep, StorageDep
from common.logger import bind_context, get_logger
from common.models import Campaign, Shop
from common.schemas import (
    CampaignGenerateRequest,
    CampaignQueuedResponse,
    CampaignResponse,
    CampaignStatus,
    CampaignTrigger,
)
from common.storage import Storage

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
log = get_logger(__name__)


def campaign_response(campaign: Campaign, storage: Storage) -> CampaignResponse:
    response = CampaignResponse.model_validate(campaign)
    if campaign.minio_output_key:
        response.media_url = storage.presigned_url(campaign.minio_output_key)
    return response


@router.post(
    "/generate",
    status_code=status.HTTP_202_ACCEPTED,
    responses={503: {"description": "Task queue unavailable"}},
)
async def generate_campaign(
    body: CampaignGenerateRequest, session: DbSession, dispatcher: DispatcherDep
) -> CampaignQueuedResponse:
    """Create a QUEUED campaign and hand it to the worker. Returns immediately (202);
    the client polls `GET /campaigns/{id}` until the status leaves QUEUED/PROCESSING."""
    if await session.get(Shop, body.shop_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Shop {body.shop_id} not found")

    campaign = Campaign(
        id=uuid.uuid4(),
        shop_id=body.shop_id,
        format=body.format,
        status=CampaignStatus.QUEUED,
        trigger=CampaignTrigger.MANUAL,
    )
    session.add(campaign)
    # Commit *before* enqueueing: a fast worker could otherwise pick up the task and
    # look for a row that is not visible yet.
    await session.commit()
    bind_context(campaign_id=str(campaign.id), shop_id=str(body.shop_id))

    try:
        await dispatcher.dispatch(campaign.id)
    except Exception as exc:
        # The row exists but no task does; mark it so it never sits in QUEUED forever.
        campaign.status = CampaignStatus.FAILED
        campaign.error_message = f"Could not enqueue task: {exc!r}"
        await session.commit()
        log.error("campaign_enqueue_failed", error=repr(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Task queue unavailable, try again later"
        ) from exc

    log.info("campaign_queued", format=body.format, trigger=CampaignTrigger.MANUAL)
    return CampaignQueuedResponse(campaign_id=campaign.id, status=campaign.status)


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID, session: DbSession, storage: StorageDep
) -> CampaignResponse:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Campaign {campaign_id} not found")
    return campaign_response(campaign, storage)
