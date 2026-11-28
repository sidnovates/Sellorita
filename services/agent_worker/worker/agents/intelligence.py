"""Intelligence layer: three agents that run in parallel and each write one state key.

* CalendarCulturalAgent -> calendar_context   (cached per day)
* NewsTrendAgent        -> news_trend_context (cached per city per day)
* BrandCatalogAgent     -> brand_catalog      (always fresh from PostgreSQL)

Each agent writes only its own key, so the parallel updates never collide and no
reducer is needed (DESIGN.md section 3).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from common.db import session_scope
from common.models import ProductOffer, Shop
from common.schemas import CampaignState
from common.settings import get_settings
from worker.agents.base import agent_node
from worker.cache import cached_json, calendar_key, trends_key

# --- Calendar -------------------------------------------------------------------------------

# Upcoming festivals and occasions. Lunar-calendar dates move every year and some vary by
# region, so treat these as approximate. In Phase 2 the LLM agent fills in local context.
FESTIVALS: list[tuple[date, str]] = [
    (date(2026, 1, 1), "New Year"),
    (date(2026, 1, 14), "Makar Sankranti / Pongal"),
    (date(2026, 1, 26), "Republic Day"),
    (date(2026, 3, 4), "Holi"),
    (date(2026, 3, 19), "Ugadi / Gudi Padwa"),
    (date(2026, 3, 21), "Eid al-Fitr"),
    (date(2026, 8, 15), "Independence Day"),
    (date(2026, 8, 26), "Onam"),
    (date(2026, 8, 28), "Raksha Bandhan"),
    (date(2026, 9, 14), "Ganesh Chaturthi"),
    (date(2026, 10, 20), "Dussehra"),
    (date(2026, 11, 8), "Diwali"),
    (date(2026, 12, 25), "Christmas"),
    (date(2026, 12, 31), "New Year's Eve"),
    (date(2027, 1, 1), "New Year"),
    (date(2027, 1, 14), "Makar Sankranti / Pongal"),
    (date(2027, 1, 26), "Republic Day"),
    (date(2027, 3, 22), "Holi"),
]
FESTIVAL_HORIZON_DAYS = 45


def today() -> date:
    return datetime.now(UTC).date()


def calendar_context_for(day: date) -> dict:
    for festival_day, name in FESTIVALS:
        days_away = (festival_day - day).days
        if 0 <= days_away <= FESTIVAL_HORIZON_DAYS:
            return {"date": day.isoformat(), "upcoming_festival": name, "days_away": days_away}
    saturday = day + timedelta(days=(5 - day.weekday()) % 7)
    return {
        "date": day.isoformat(),
        "upcoming_festival": "Weekend",
        "days_away": (saturday - day).days,
    }


def _calendar_fallback(_: CampaignState) -> dict:
    d = today().isoformat()
    return {"calendar_context": {"date": d, "upcoming_festival": "", "days_away": 0}}


@agent_node("CalendarCulturalAgent", on_error="degrade", fallback=_calendar_fallback)
def calendar_agent(state: CampaignState) -> dict:
    day = today()
    context = cached_json(calendar_key(day.isoformat()), lambda: calendar_context_for(day))
    return {"calendar_context": context}


# --- News / trends -----------------------------------------------------------------------------

_MOCK_WEATHER = ["sunny, 29°C", "pleasant evening breeze", "light drizzle", "clear and cool"]
_MOCK_HOOKS = [
    "the weekend food walk everyone in {city} is talking about",
    "a new street-art festival in {city}",
    "{city}'s favourite cricket team winning big",
    "the first cool evenings of the season in {city}",
]


def _shop_city(shop_id: str) -> str:
    with session_scope() as session:
        city = session.scalar(select(Shop.city).where(Shop.id == uuid.UUID(shop_id)))
    if city is None:
        raise LookupError(f"Shop {shop_id} not found")
    return city


def mock_trends(city: str, day: str) -> dict:
    # Deterministic per city+day (hash, not random), so tests and demos are repeatable.
    n = int(hashlib.sha256(f"{city}:{day}".encode()).hexdigest(), 16)
    return {
        "local_hook": _MOCK_HOOKS[n % len(_MOCK_HOOKS)].format(city=city),
        "weather": _MOCK_WEATHER[(n // 7) % len(_MOCK_WEATHER)],
        "safe_to_use": True,  # Phase 2: the LLM filters negative/controversial news
    }


def _news_fallback(_: CampaignState) -> dict:
    return {"news_trend_context": {"local_hook": "", "weather": "", "safe_to_use": False}}


@agent_node("NewsTrendAgent", on_error="degrade", fallback=_news_fallback)
def news_agent(state: CampaignState) -> dict:
    if not get_settings().mock_mode:
        raise NotImplementedError("Real news/trend lookup arrives in Phase 2")
    city, day = _shop_city(state["shop_id"]), today().isoformat()
    context = cached_json(trends_key(city, day), lambda: mock_trends(city, day))
    return {"news_trend_context": context}


# --- Brand catalog ------------------------------------------------------------------------------


@agent_node("BrandCatalogAgent")  # fatal on error: no brand data, no campaign
def brand_agent(state: CampaignState) -> dict:
    with session_scope() as session:
        shop = session.get(Shop, uuid.UUID(state["shop_id"]))
        if shop is None:
            raise LookupError(f"Shop {state['shop_id']} not found")
        # Rotate products: feature the one shown least recently (never-featured first).
        products = session.scalars(
            select(ProductOffer)
            .where(ProductOffer.shop_id == shop.id, ProductOffer.is_active.is_(True))
            .order_by(ProductOffer.last_featured_at.asc().nulls_first(), ProductOffer.title)
        ).all()
        featured = products[0] if products else None
        offer = ""
        if featured:
            offer = featured.title + (
                f" - {featured.discount_text}" if featured.discount_text else ""
            )
        catalog = {
            "name": shop.name,
            "colors": [shop.brand_primary_color, shop.brand_secondary_color],
            "font": shop.brand_font,
            "offer": offer,
            "asset_keys": [p.minio_image_key for p in products],
            "category": shop.category,
            "city": shop.city,
            "tone": shop.tone,
            "featured_product_id": str(featured.id) if featured else None,
        }
    return {"brand_catalog": catalog}
