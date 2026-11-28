"""Shop profile and product photo upload endpoints."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import DbSession, StorageDep
from common.logger import bind_context, get_logger
from common.models import ProductOffer, Shop
from common.schemas import AssetResponse, ShopCreate, ShopResponse
from common.storage import Storage, product_image_key

router = APIRouter(prefix="/shops", tags=["shops"])
log = get_logger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """(extension, content type) from the file's magic bytes, or None if not an image.

    The browser-supplied Content-Type is just a claim; the first bytes of the file are
    what it actually is. Only JPEG, PNG and WebP are accepted.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def asset_response(product: ProductOffer, storage: Storage) -> AssetResponse:
    asset = AssetResponse.model_validate(product)
    asset.image_url = storage.presigned_url(product.minio_image_key)
    return asset


async def get_shop_or_404(session: DbSession, shop_id: uuid.UUID) -> Shop:
    shop = await session.scalar(
        select(Shop).options(selectinload(Shop.products)).where(Shop.id == shop_id)
    )
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Shop {shop_id} not found")
    return shop


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_shop(body: ShopCreate, session: DbSession) -> ShopResponse:
    shop = Shop(**body.model_dump(), products=[])
    session.add(shop)
    await session.commit()
    bind_context(shop_id=str(shop.id))
    log.info("shop_created", city=shop.city, category=shop.category)
    return ShopResponse.model_validate(shop)


@router.get("/{shop_id}")
async def get_shop(shop_id: uuid.UUID, session: DbSession, storage: StorageDep) -> ShopResponse:
    shop = await get_shop_or_404(session, shop_id)
    response = ShopResponse.model_validate(shop)
    response.products = [asset_response(p, storage) for p in shop.products]
    return response


@router.post(
    "/{shop_id}/assets",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"description": "File too large"}, 415: {"description": "Not an image"}},
)
async def upload_asset(
    shop_id: uuid.UUID,
    session: DbSession,
    storage: StorageDep,
    file: Annotated[UploadFile, File(description="Product photo (JPEG, PNG or WebP)")],
    title: Annotated[str, Form(min_length=1, max_length=160)],
    discount: Annotated[str | None, Form(max_length=160)] = None,
) -> AssetResponse:
    await get_shop_or_404(session, shop_id)
    bind_context(shop_id=str(shop_id))

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File larger than 10 MB")
    sniffed = sniff_image(data)
    if sniffed is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a JPEG, PNG or WebP image"
        )
    ext, content_type = sniffed

    # The ID is generated before the INSERT, so the object key is known up front.
    product = ProductOffer(
        id=uuid.uuid4(), shop_id=shop_id, title=title, discount_text=discount or None
    )
    product.minio_image_key = product_image_key(shop_id, product.id, ext)

    # Upload first, then write the row: a row never points at a missing object. If
    # the DB write fails, remove the object so storage does not collect orphans.
    await asyncio.to_thread(storage.upload_bytes, product.minio_image_key, data, content_type)
    try:
        session.add(product)
        await session.commit()
    except Exception:
        await asyncio.to_thread(storage.delete, product.minio_image_key)
        raise

    log.info("asset_uploaded", product_id=str(product.id), size_bytes=len(data), type=ext)
    return asset_response(product, storage)
