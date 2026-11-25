"""Strategy layer: CreativeDirectorAgent turns the three intelligence outputs into one
creative brief (hook, script, caption, hashtags, CTA, theme).

It is also where the QA retry loop re-enters the graph: when it runs after a failed QA
check, it receives `qa_feedback`, counts the retry, and must produce a revised brief.
"""

from __future__ import annotations

import json

from common.schemas import CampaignState
from common.settings import get_settings
from worker.agents.base import USAGE, agent_node, estimate_tokens

MOCK_PROMPT_VERSION = "mock"


def _hashtag(text: str) -> str:
    return "#" + "".join(
        word.capitalize() for word in text.replace("/", " ").split() if word.isalnum()
    )


def mock_brief(state: CampaignState) -> dict:
    """Template-based brief built from the real intelligence outputs (no LLM call)."""
    brand = state["brand_catalog"]
    calendar = state.get("calendar_context", {})
    news = state.get("news_trend_context", {})
    festival = calendar.get("upcoming_festival") or "this week"
    offer = brand["offer"] or f"our {brand.get('category', 'shop')} favourites"

    hook = f"{festival} is {calendar.get('days_away', 0)} days away!"
    if news.get("safe_to_use") and news.get("local_hook"):
        hook += f" Plus {news['local_hook']}."
    cta = f"Visit {brand['name']} today"
    caption = f"{hook} Celebrate with {offer} at {brand['name']}, {brand.get('city', '')}. {cta}!"
    if state.get("qa_feedback"):
        caption = f"[Revised] {caption}"
    hashtags = [_hashtag(festival), _hashtag(brand["name"]), _hashtag(brand.get("city", ""))]
    return {
        "hook": hook,
        "script": f"Scene 1: {hook} / Scene 2: {offer} / Scene 3: {cta}",
        "caption": caption,
        "hashtags": [h for h in hashtags if len(h) > 1],
        "cta": cta,
        "theme": f"{festival} x {brand.get('tone', 'friendly')}",
    }


@agent_node("CreativeDirectorAgent")
def creative_director(state: CampaignState) -> dict:
    if not get_settings().mock_mode:
        raise NotImplementedError("LLM creative director arrives in Phase 2")

    update: dict = {}
    if state.get("qa_passed") is False:
        # Re-entered from the QA critic: this is a retry.
        update["retry_count"] = state.get("retry_count", 0) + 1

    brief = mock_brief(state)
    prompt_text = json.dumps(
        {
            k: state.get(k)
            for k in ("calendar_context", "news_trend_context", "brand_catalog", "qa_feedback")
        }
    )
    update |= {
        "creative_brief": brief,
        "error": None,  # clear a production error from the previous attempt
        USAGE: {
            "prompt_version": MOCK_PROMPT_VERSION,
            "input_tokens": estimate_tokens(prompt_text),
            "output_tokens": estimate_tokens(json.dumps(brief)),
        },
    }
    return update
