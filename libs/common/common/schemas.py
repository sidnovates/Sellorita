"""Shared enums, the LangGraph `CampaignState` contract, and the REST API schemas
(DESIGN.md sections 3, 4 and 5).
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field

# --- Enums (stored as strings in PostgreSQL, sent as strings over the API) -------------


class CampaignFormat(StrEnum):
    REEL = "REEL"
    STATIC_POST = "STATIC_POST"
    TWEET = "TWEET"


class CampaignStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class CampaignTrigger(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


class ScheduleFrequency(StrEnum):
    DAILY = "DAILY"
    TWICE_WEEKLY = "TWICE_WEEKLY"
    THRICE_WEEKLY = "THRICE_WEEKLY"


# --- Per-layer outputs ------------------------------------------------------------------


class CalendarContext(TypedDict):
    date: str
    upcoming_festival: str
    days_away: int


class NewsTrendContext(TypedDict):
    local_hook: str
    weather: str
    safe_to_use: bool


class BrandCatalog(TypedDict):
    name: str
    colors: list[str]
    font: str
    offer: str
    asset_keys: list[str]


class CreativeBrief(TypedDict):
    hook: str
    script: str
    caption: str
    hashtags: list[str]
    cta: str
    theme: str


class LLMUsage(TypedDict):
    agent: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


# --- The state passed between LangGraph nodes --------------------------------------------


class CampaignState(TypedDict, total=False):
    """Each node returns a partial dict containing only the keys it owns.

    `total=False` because the graph starts with just the three required keys and the
    rest are filled in as nodes run.
    """

    campaign_id: Required[str]
    shop_id: Required[str]
    target_format: Required[CampaignFormat]

    # Intelligence layer (parallel): each agent writes only its own key, so no reducer.
    calendar_context: CalendarContext
    news_trend_context: NewsTrendContext
    brand_catalog: BrandCatalog

    # Strategy layer
    creative_brief: CreativeBrief

    # Production layer: None for TWEET (text only)
    artifact_minio_key: str | None

    # Verification layer
    qa_passed: bool
    qa_feedback: str
    retry_count: int

    # Set by any node on an unrecoverable failure
    error: str | None

    # LLMOps metadata. Several nodes append to this key, so it needs a reducer:
    # LangGraph combines updates with `operator.add` (list concatenation).
    llm_usage: Annotated[list[LLMUsage], operator.add]


# --- REST API schemas (DESIGN.md section 5) ------------------------------------------------

HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$", examples=["#7B3F00"])]


class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60, examples=["cafe"])
    city: str = Field(min_length=1, max_length=80, examples=["Bengaluru"])
    brand_primary_color: HexColor
    brand_secondary_color: HexColor = "#FFFFFF"
    brand_font: str = Field(default="Poppins", max_length=60)
    tone: str = Field(default="friendly", max_length=60)
    schedule_frequency: ScheduleFrequency = ScheduleFrequency.DAILY


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    shop_id: uuid.UUID
    title: str
    discount_text: str | None
    minio_image_key: str
    is_active: bool
    image_url: str | None = None  # presigned, public endpoint; filled by the API


class ShopResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build directly from ORM objects

    id: uuid.UUID
    name: str
    category: str
    city: str
    brand_primary_color: str
    brand_secondary_color: str
    brand_font: str
    tone: str
    logo_minio_key: str | None
    schedule_frequency: ScheduleFrequency
    created_at: datetime
    products: list[AssetResponse] = []


class CampaignGenerateRequest(BaseModel):
    shop_id: uuid.UUID
    format: CampaignFormat


class CampaignQueuedResponse(BaseModel):
    campaign_id: uuid.UUID
    status: CampaignStatus


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    shop_id: uuid.UUID
    format: CampaignFormat
    status: CampaignStatus
    trigger: CampaignTrigger
    caption: str | None
    hashtags: list[str]
    minio_output_key: str | None
    media_url: str | None = None  # presigned, public endpoint; None for TWEET / not ready
    qa_feedback: str | None
    retry_count: int
    error_message: str | None
    scheduled_for: datetime | None
    created_at: datetime
    updated_at: datetime
