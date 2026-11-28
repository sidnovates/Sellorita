"""Production layer: one agent per output format, chosen by the router on target_format.

* ReelProductionAgent -> 1080x1920 MP4 in MinIO
* StaticPostAgent     -> 1080x1080 PNG in MinIO
* CopyTweetAgent      -> text only (artifact_minio_key = None)

Rendering happens in `{RENDER_SCRATCH_DIR}/{campaign_id}/`, the only local disk a worker
pod may use (stateless pods, DESIGN.md rule 1). The directory is removed afterwards, even
on failure, so a long-running pod does not slowly fill its disk.

Errors are written to state (`on_error="state"`) so the QA critic can trigger a retry.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from common.schemas import CampaignState
from common.settings import get_settings
from common.storage import get_storage, output_key
from worker.agents.base import USAGE, agent_node, estimate_tokens
from worker.renderers.mock_media import render_mock_post, render_mock_reel

TWEET_LIMIT = 280


@contextmanager
def scratch_dir(campaign_id: str) -> Iterator[Path]:
    path = Path(get_settings().render_scratch_dir) / campaign_id
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _render_and_upload(
    state: CampaignState, ext: str, content_type: str, render: Callable[[Path, str, str], Path]
) -> str:
    if not get_settings().mock_mode:
        raise NotImplementedError("LLM-authored HTML rendering arrives in Phase 2")
    primary, secondary = state["brand_catalog"]["colors"]
    key = output_key(state["shop_id"], state["campaign_id"], ext)
    with scratch_dir(state["campaign_id"]) as tmp:
        out = render(tmp / f"output.{ext}", primary, secondary)
        # Retries reuse the same key, so a retry overwrites the rejected attempt.
        get_storage().upload_file(key, out, content_type)
    return key


@agent_node("ReelProductionAgent", on_error="state")
def reel_agent(state: CampaignState) -> dict:
    return {"artifact_minio_key": _render_and_upload(state, "mp4", "video/mp4", render_mock_reel)}


@agent_node("StaticPostAgent", on_error="state")
def static_post_agent(state: CampaignState) -> dict:
    return {"artifact_minio_key": _render_and_upload(state, "png", "image/png", render_mock_post)}


@agent_node("CopyTweetAgent", on_error="state")
def tweet_agent(state: CampaignState) -> dict:
    brief = dict(state["creative_brief"])
    tags = " ".join(brief["hashtags"])
    room = TWEET_LIMIT - len(tags) - 1
    text = brief["caption"]
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    brief["caption"] = text
    return {
        "creative_brief": brief,
        "artifact_minio_key": None,
        USAGE: {
            "prompt_version": "mock",
            "input_tokens": estimate_tokens(state["creative_brief"]["caption"]),
            "output_tokens": estimate_tokens(text),
        },
    }
