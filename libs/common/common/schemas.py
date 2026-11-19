"""Shared enums and the LangGraph `CampaignState` contract (DESIGN.md sections 3 and 4).

API request/response schemas are added here in Step 3.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Required, TypedDict

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
