"""Verification layer: QACriticAgent checks the output before a human ever sees it.

Deterministic checks (always run, also in Phase 2):
* production did not report an error;
* REEL/STATIC_POST: the object exists in MinIO, is non-empty and has the right type;
* the caption is within the platform limit, names the brand, and contains the CTA.

In Phase 2 an LLM review (tone, brand fit, safety) is added on top. In mock mode,
MOCK_QA_FAILURES=N makes the first N attempts fail, to exercise the retry loop.

The critic only *judges*; the graph's router decides retry vs. fail (see graph/builder.py).
"""

from __future__ import annotations

from common.schemas import CampaignFormat, CampaignState
from common.settings import get_settings
from common.storage import get_storage
from worker.agents.base import agent_node
from worker.agents.production import TWEET_LIMIT

CAPTION_LIMIT = 2200  # Instagram
EXPECTED_TYPES = {CampaignFormat.REEL: "video/mp4", CampaignFormat.STATIC_POST: "image/png"}


def find_problems(state: CampaignState) -> list[str]:
    if state.get("error"):
        return [f"Production failed: {state['error']}"]

    problems: list[str] = []
    fmt = state["target_format"]
    if fmt in EXPECTED_TYPES:
        key = state.get("artifact_minio_key")
        meta = get_storage().head(key) if key else None
        if meta is None:
            problems.append("Rendered media is missing from storage")
        elif meta["ContentLength"] == 0:
            problems.append("Rendered media is empty")
        elif meta["ContentType"] != EXPECTED_TYPES[fmt]:
            problems.append(f"Media has type {meta['ContentType']}, expected {EXPECTED_TYPES[fmt]}")

    brief = state["creative_brief"]
    caption = brief["caption"]
    limit = TWEET_LIMIT if fmt == CampaignFormat.TWEET else CAPTION_LIMIT
    if fmt == CampaignFormat.TWEET:
        caption_len = len(caption) + 1 + len(" ".join(brief["hashtags"]))
    else:
        caption_len = len(caption)
    if caption_len > limit:
        problems.append(f"Caption is {caption_len} characters, limit is {limit}")
    if state["brand_catalog"]["name"].lower() not in caption.lower():
        problems.append("Caption does not mention the brand name")
    if fmt != CampaignFormat.TWEET and brief["cta"].lower() not in caption.lower():
        problems.append("Caption is missing the call to action")
    return problems


@agent_node("QACriticAgent")
def qa_critic(state: CampaignState) -> dict:
    problems = find_problems(state)

    settings = get_settings()
    attempt = state.get("retry_count", 0) + 1
    if not problems and settings.mock_mode and attempt <= settings.mock_qa_failures:
        problems.append(
            f"Mock QA rejection {attempt}/{settings.mock_qa_failures}: hook too generic"
        )

    return {"qa_passed": not problems, "qa_feedback": "; ".join(problems)}
