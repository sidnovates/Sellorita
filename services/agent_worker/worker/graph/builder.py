"""The LangGraph state machine (DESIGN.md section 3, "Graph Shape").

    START ─┬─► calendar ─┐
           ├─► news ─────┼─► creative_director ─► [format] ─► reel|post|tweet ─► qa_critic
           └─► brand ────┘           ▲                                              │
                                     └──── retry (retry_count < QA_MAX_RETRIES) ◄───┤
                                               passed ─► END / else ─► mark_failed ─► END

How LangGraph runs this:
* Execution goes in "supersteps". Every node whose inputs are ready runs in the same
  superstep, so calendar/news/brand run in parallel (threads, for sync nodes).
* `add_edge([a, b, c], d)` is a *join*: d runs only after all of a, b and c finished.
* After each superstep, the partial updates returned by the nodes are merged into the
  state, using the reducer of each key (default: overwrite; `llm_usage`: list append).
* Conditional edges call a plain function on the state to pick the next node. Routing
  stays out of the agents, so the control flow is visible in one place.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from common.schemas import CampaignFormat, CampaignState
from common.settings import get_settings
from worker.agents.base import agent_node
from worker.agents.creative import creative_director
from worker.agents.intelligence import brand_agent, calendar_agent, news_agent
from worker.agents.production import reel_agent, static_post_agent, tweet_agent
from worker.agents.qa import qa_critic

PRODUCTION_NODES = {
    CampaignFormat.REEL: "reel",
    CampaignFormat.STATIC_POST: "static_post",
    CampaignFormat.TWEET: "tweet",
}


def route_by_format(state: CampaignState) -> str:
    return PRODUCTION_NODES[CampaignFormat(state["target_format"])]


def route_after_qa(state: CampaignState) -> Literal["done", "retry", "fail"]:
    if state["qa_passed"]:
        return "done"
    # retry_count = retries already made. With QA_MAX_RETRIES=2 there are at most
    # 3 attempts, after which the campaign is marked FAILED (bounded loop: an LLM that
    # keeps failing must not burn tokens forever).
    if state.get("retry_count", 0) < get_settings().qa_max_retries:
        return "retry"
    return "fail"


@agent_node("MarkFailed")
def mark_failed(state: CampaignState) -> dict:
    retries = state.get("retry_count", 0)
    return {"error": f"QA rejected the output after {retries} retries: {state['qa_feedback']}"}


def build_graph() -> StateGraph:
    g = StateGraph(CampaignState)

    g.add_node("calendar", calendar_agent)
    g.add_node("news", news_agent)
    g.add_node("brand", brand_agent)
    g.add_node("creative_director", creative_director)
    g.add_node("reel", reel_agent)
    g.add_node("static_post", static_post_agent)
    g.add_node("tweet", tweet_agent)
    g.add_node("qa_critic", qa_critic)
    g.add_node("mark_failed", mark_failed)

    # Intelligence layer: fan out from START, join before the creative director.
    for node in ("calendar", "news", "brand"):
        g.add_edge(START, node)
    g.add_edge(["calendar", "news", "brand"], "creative_director")

    # Production layer: exactly one branch runs, chosen by target_format.
    g.add_conditional_edges("creative_director", route_by_format, list(PRODUCTION_NODES.values()))
    for node in PRODUCTION_NODES.values():
        g.add_edge(node, "qa_critic")

    # Verification layer: done, bounded retry, or fail.
    g.add_conditional_edges(
        "qa_critic",
        route_after_qa,
        {"done": END, "retry": "creative_director", "fail": "mark_failed"},
    )
    g.add_edge("mark_failed", END)
    return g


@lru_cache
def get_pipeline() -> CompiledStateGraph:
    """Compile once per process; compiled graphs are reusable and thread-safe."""
    return build_graph().compile()
