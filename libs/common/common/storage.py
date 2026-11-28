"""MinIO (S3-compatible) object storage: uploads, downloads and presigned URLs.

Why two clients? A presigned URL is a normal URL plus a signature computed over the
method, path, expiry *and host*. The worker and headless Chromium reach MinIO inside the
cluster as `http://minio:9000`; the browser reaches it through the ingress. A URL signed
for one host is rejected by MinIO when requested via the other, so:

* `internal` client: real I/O (upload, download, delete) and URLs for in-cluster readers
  (e.g. product images embedded in LLM-authored HTML rendered by Chromium);
* `public` client: never sends a request; it only signs URLs handed to the browser.

Signing is pure local computation (HMAC with the secret key), so creating a URL costs
no network round trip.

boto3 is synchronous. In async FastAPI code, call these methods via
`asyncio.to_thread(...)` so the event loop is not blocked during an upload.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from common.settings import Settings, get_settings

# --- Object key layout (DESIGN.md 4.2). Build keys only through these helpers. -------------


def product_image_key(shop_id: uuid.UUID | str, product_id: uuid.UUID | str, ext: str) -> str:
    return f"shops/{shop_id}/products/{product_id}.{ext}"


def logo_key(shop_id: uuid.UUID | str, ext: str = "png") -> str:
    return f"shops/{shop_id}/brand/logo.{ext}"


def output_key(shop_id: uuid.UUID | str, campaign_id: uuid.UUID | str, ext: str) -> str:
    """Rendered artifact: ext is "mp4" (REEL) or "png" (STATIC_POST)."""
    return f"shops/{shop_id}/outputs/{campaign_id}.{ext}"


# --- Client ------------------------------------------------------------------------------------


def _make_client(settings: Settings, endpoint: str) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.minio_root_user.get_secret_value(),
        aws_secret_access_key=settings.minio_root_password.get_secret_value(),
        region_name=settings.minio_region,
        config=Config(
            signature_version="s3v4",
            # MinIO serves buckets as a path (host/bucket/key), not as a subdomain
            # (bucket.host/key), which would need wildcard DNS.
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=30,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


class Storage:
    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.bucket = settings.minio_bucket
        self.expiry = settings.presigned_url_expiry_seconds
        self.internal = _make_client(settings, settings.minio_internal_endpoint)
        self.public = _make_client(settings, settings.minio_public_endpoint)

    # --- writes / reads (internal endpoint) ---

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.internal.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def upload_file(self, key: str, path: str | Path, content_type: str) -> None:
        # upload_file switches to multipart upload for large files (e.g. long MP4s).
        self.internal.upload_file(
            str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
        )

    def download_file(self, key: str, path: str | Path) -> None:
        self.internal.download_file(self.bucket, key, str(path))

    def read_bytes(self, key: str) -> bytes:
        return self.internal.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key: str) -> dict[str, Any] | None:
        """Object metadata (size, content type), or None if the key does not exist."""
        try:
            return self.internal.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def delete(self, key: str) -> None:
        self.internal.delete_object(Bucket=self.bucket, Key=key)

    # --- presigned URLs (no network) ---

    def presigned_url(self, key: str, *, public: bool = True) -> str:
        """Time-limited GET URL. public=True for the browser, False for in-cluster use."""
        client = self.public if public else self.internal
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expiry
        )

    # --- health / setup ---

    def ping(self) -> bool:
        """True if MinIO is reachable and the bucket exists (readiness probe)."""
        try:
            self.internal.head_bucket(Bucket=self.bucket)
            return True
        except (BotoCoreError, ClientError):
            return False

    def ensure_bucket(self) -> None:
        """Create the bucket if missing. For tests and scripts; in compose/K8s the
        bucket is created by infrastructure (minio-init), not by the app."""
        if not self.ping():
            self.internal.create_bucket(Bucket=self.bucket)


@lru_cache
def get_storage() -> Storage:
    return Storage()
