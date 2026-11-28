"""SQLAlchemy 2.0 ORM models: the PostgreSQL schema from DESIGN.md section 4.1.

These classes are the single definition of the schema. Alembic compares them against the
live database to generate migrations, so the app never calls `create_all()`.

Conventions used here:
* UUID primary keys generated in Python, so an ID is known before the INSERT. The asset
  upload uses this: the MinIO key `shops/{shop_id}/products/{product_id}.jpg` is built
  before the row is written.
* Enums are stored as VARCHAR + CHECK constraint (`native_enum=False`), not as PostgreSQL
  ENUM types. Adding a new status later is then a simple constraint change, whereas
  `ALTER TYPE ... ADD VALUE` has transaction restrictions and cannot remove values.
* Timestamps are `timestamptz` (stored in UTC) with server-side defaults.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from common.schemas import CampaignFormat, CampaignStatus, CampaignTrigger, ScheduleFrequency

# Deterministic constraint/index names. Without this, PostgreSQL invents names like
# "campaigns_shop_id_fkey", and a later migration that drops a constraint has to guess
# what the name was. With it, every name is predictable from the model.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A StrEnum stored as VARCHAR with a CHECK constraint listing the allowed values."""
    return Enum(
        enum_cls,
        name=name,  # constraint becomes ck_<table>_<name>
        native_enum=False,
        create_constraint=True,
        validate_strings=True,  # reject unknown strings in Python, before the DB does
        length=max(len(m.value) for m in enum_cls),
        values_callable=lambda e: [m.value for m in e],  # store "REEL", not the member name
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Shop(TimestampMixin, Base):
    __tablename__ = "shops"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))  # cafe, salon, retail, ...
    city: Mapped[str] = mapped_column(String(80))  # keys the trends cache per city
    brand_primary_color: Mapped[str] = mapped_column(String(7))  # "#RRGGBB"
    brand_secondary_color: Mapped[str] = mapped_column(String(7))
    brand_font: Mapped[str] = mapped_column(String(60))
    tone: Mapped[str] = mapped_column(String(60))  # "friendly", "premium", ...
    logo_minio_key: Mapped[str | None] = mapped_column(String(255))
    schedule_frequency: Mapped[ScheduleFrequency] = mapped_column(
        _str_enum(ScheduleFrequency, "schedule_frequency")
    )

    products: Mapped[list[ProductOffer]] = relationship(
        back_populates="shop", cascade="all, delete-orphan", passive_deletes=True
    )
    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="shop", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Shop {self.id} {self.name!r}>"


class ProductOffer(Base):
    __tablename__ = "products_offers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    discount_text: Mapped[str | None] = mapped_column(String(160))  # "20% off this weekend"
    minio_image_key: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # Lets BrandCatalogAgent rotate products instead of always featuring the same one.
    last_featured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shop: Mapped[Shop] = relationship(back_populates="products")

    def __repr__(self) -> str:
        return f"<ProductOffer {self.id} {self.title!r}>"


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        # Review dashboard: "drafts for this shop with status PENDING_REVIEW".
        Index("ix_campaigns_shop_id_status", "shop_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    format: Mapped[CampaignFormat] = mapped_column(_str_enum(CampaignFormat, "format"))
    status: Mapped[CampaignStatus] = mapped_column(
        _str_enum(CampaignStatus, "status"),
        default=CampaignStatus.QUEUED,
        server_default=CampaignStatus.QUEUED.value,
    )
    trigger: Mapped[CampaignTrigger] = mapped_column(
        _str_enum(CampaignTrigger, "trigger"),
        default=CampaignTrigger.MANUAL,
        server_default=CampaignTrigger.MANUAL.value,
    )
    # Filled in by the pipeline; empty while QUEUED/PROCESSING.
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    minio_output_key: Mapped[str | None] = mapped_column(String(255))  # None for TWEET
    qa_feedback: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Intelligence outputs, creative brief, llm_usage and prompt versions. JSONB because
    # its shape changes as agents evolve, and Kibana gets the same data from logs anyway.
    agent_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # SCHEDULED campaigns: the slot Celery Beat created them for. None for MANUAL.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    shop: Mapped[Shop] = relationship(back_populates="campaigns")

    def __repr__(self) -> str:
        return f"<Campaign {self.id} {self.format} {self.status}>"
