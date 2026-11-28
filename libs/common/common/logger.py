"""Structured JSON logging shared by every Sellorita service.

Every line written to stdout is one JSON object, so Filebeat/Logstash can ship it to
Elasticsearch and Kibana can filter and aggregate on its fields (`agent_name`,
`duration_ms`, `campaign_id`, ...).

Logs from third-party libraries that use the standard `logging` module (uvicorn, Celery,
SQLAlchemy) are routed through the same processors, so ELK never receives mixed formats.

Usage:
    configure_logging(service_name="api-gateway")   # once, at process start
    log = get_logger(__name__)
    bind_context(campaign_id=cid, shop_id=sid)      # attached to every later line
    log.info("agent_node_completed", agent_name="CalendarCulturalAgent", duration_ms=12)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
import structlog.tracebacks
from structlog.types import EventDict, Processor, WrappedLogger

from common.settings import get_settings

# Context helpers: values bound here are stored in a contextvar, so they follow the
# current request/task (including across `await`) without being passed around.
bind_context = structlog.contextvars.bind_contextvars
unbind_context = structlog.contextvars.unbind_contextvars
clear_context = structlog.contextvars.clear_contextvars

# Library loggers that install their own handlers; we strip those and let records
# propagate to our root handler instead.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "celery", "sqlalchemy")


def _uppercase_level(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    if "level" in event_dict:
        event_dict["level"] = str(event_dict["level"]).upper()
    return event_dict


def _drop_color_message(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    # uvicorn attaches an ANSI-coloured copy of each message; useless noise in ELK.
    event_dict.pop("color_message", None)
    return event_dict


# Read at log time, not captured at configure time: structlog caches loggers with the
# processor chain they were created with, so a value captured in a closure would stick.
_service_name = "sellorita"


def _add_service(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("service", _service_name)
    return event_dict


def configure_logging(
    service_name: str | None = None,
    level: str | None = None,
    json_logs: bool | None = None,
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    global _service_name
    settings = get_settings()
    _service_name = service_name or settings.service_name
    level = (level or settings.log_level).upper()
    json_logs = settings.log_json if json_logs is None else json_logs

    # Processors applied to every record, whether it came from structlog or stdlib.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
        _uppercase_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_service,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    final_processors: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if json_logs:
        # Exceptions become structured objects instead of one giant string.
        final_processors += [
            # show_locals=False: frame locals can hold secrets (DB connection strings,
            # API keys, request bodies) and must never be shipped to Elasticsearch.
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            structlog.processors.JSONRenderer(),
        ]
    else:
        final_processors += [structlog.dev.ConsoleRenderer()]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=final_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    # SQLAlchemy logs every SQL statement at INFO; keep it quiet unless debugging.
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name, **initial_values)
