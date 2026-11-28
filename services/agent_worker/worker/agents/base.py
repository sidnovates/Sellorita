"""The `agent_node` decorator: what every LangGraph node does besides its own job.

A LangGraph node is a function `state -> partial state update`. Every node also needs
the same bookkeeping, so it lives here once instead of in each agent:

1. **Timing + one structured log line** (`agent_node_completed`, with `agent_name`,
   `duration_ms`, `status`) that Kibana's "latency per agent" panel aggregates.
2. **LLMOps usage:** a node that called an LLM returns a `USAGE` entry (tokens, prompt
   version). The decorator turns it into an `llm_usage` record, which LangGraph appends
   to the state list via the `operator.add` reducer, and logs the same fields.
3. **Error policy**, chosen per node:
   * `on_error="raise"`: the error is fatal (e.g. the shop is missing from the DB). The
     exception propagates, the graph stops, and the Celery task marks the campaign FAILED.
   * `on_error="degrade"`: the node is optional (news, calendar). Its `fallback` value is
     used instead and the log says `status=DEGRADED`; the campaign still gets made.
   * `on_error="state"`: the error is written to `state["error"]` so the QA critic can
     send it back for another attempt (rendering can fail transiently).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, Literal

from common.logger import get_logger
from common.schemas import CampaignState, LLMUsage

_log = get_logger("worker.agents")

USAGE = "__usage__"  # key a node uses to report LLM usage; never reaches the graph state

NodeFn = Callable[[CampaignState], dict[str, Any]]


def estimate_tokens(text: str) -> int:
    """~4 characters per token: the usual rough estimate for English text. Used to make
    mock-mode usage numbers plausible; real calls report exact counts."""
    return max(1, len(text) // 4)


def agent_node(
    agent_name: str,
    *,
    on_error: Literal["raise", "degrade", "state"] = "raise",
    fallback: Callable[[CampaignState], dict[str, Any]] | None = None,
) -> Callable[[NodeFn], NodeFn]:
    if on_error == "degrade" and fallback is None:
        raise ValueError("on_error='degrade' needs a fallback")

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def node(state: CampaignState) -> dict[str, Any]:
            # Parallel nodes run in a thread pool; bind ids from the state explicitly
            # rather than relying on context variables crossing threads.
            log = _log.bind(campaign_id=state["campaign_id"], shop_id=state["shop_id"])
            start = time.perf_counter()
            attempt = state.get("retry_count", 0) + 1
            status = "SUCCESS"
            try:
                update = dict(fn(state))
            except Exception as exc:
                duration_ms = round((time.perf_counter() - start) * 1000)
                if on_error == "raise":
                    log.exception(
                        "agent_node_failed",
                        agent_name=agent_name,
                        duration_ms=duration_ms,
                        attempt=attempt,
                        status="FAILED",
                    )
                    raise
                if on_error == "degrade":
                    log.warning("agent_node_degraded", agent_name=agent_name, error=repr(exc))
                    update, status = dict(fallback(state)), "DEGRADED"
                else:
                    log.warning("agent_node_error", agent_name=agent_name, error=repr(exc))
                    update, status = {"error": f"{agent_name}: {exc}"}, "ERROR"

            duration_ms = round((time.perf_counter() - start) * 1000)
            usage = update.pop(USAGE, None)
            fields: dict[str, Any] = {}
            if usage is not None:
                record: LLMUsage = {
                    "agent": agent_name,
                    "prompt_version": usage["prompt_version"],
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cost_usd": usage.get("cost_usd", 0.0),
                    "duration_ms": duration_ms,
                }
                update["llm_usage"] = [record]  # appended by the reducer, not replaced
                fields = {k: v for k, v in record.items() if k not in ("agent", "duration_ms")}

            log.info("agent_node_completed", agent_name=agent_name, duration_ms=duration_ms,
                     status=status, attempt=attempt, **fields)  # fmt: skip
            return update

        return node

    return decorate
