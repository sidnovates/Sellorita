"""API gateway tests: real FastAPI app, real PostgreSQL and MinIO (test DB and bucket from
conftest.py), and a fake task dispatcher, so no Celery worker or Redis is needed for
everything except the readiness check.
"""

from __future__ import annotations

import io
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import get_dispatcher
from app.main import app
from app.routers import health
from common.logger import configure_logging
from common.schemas import CampaignStatus

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e2213bc8"
    "0000000049454e44ae426082"
)

SHOP = {
    "name": "Chai Corner",
    "category": "cafe",
    "city": "Bengaluru",
    "brand_primary_color": "#7B3F00",
    "schedule_frequency": "TWICE_WEEKLY",
}


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []
        self.fail = False

    async def dispatch(self, campaign_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.dispatched.append(campaign_id)


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def client(clean_tables, test_bucket, dispatcher):
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    with TestClient(app) as c:  # `with` runs the lifespan (startup/shutdown)
        yield c
    app.dependency_overrides.clear()


def create_shop(client: TestClient, **overrides) -> dict:
    response = client.post("/api/v1/shops", json=SHOP | overrides)
    assert response.status_code == 201, response.text
    return response.json()


def upload(client: TestClient, shop_id: str, data: bytes = PNG_1PX, **form):
    return client.post(
        f"/api/v1/shops/{shop_id}/assets",
        files={"file": ("chai.png", io.BytesIO(data), "image/png")},
        data={"title": "Masala chai", **form},
    )


# --- Health ------------------------------------------------------------------------------------


def test_healthz_needs_no_dependencies():
    with TestClient(app) as c:
        assert c.get("/api/v1/healthz").json() == {"status": "ok"}


def test_readyz_all_up(client):
    response = client.get("/api/v1/readyz")
    if response.json().get("redis") == "down":
        pytest.skip("Redis not running (docker/docker-compose.dev.yml)")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "up", "redis": "up", "minio": "up"}


def test_readyz_returns_503_when_a_dependency_is_down(client, monkeypatch):
    async def down() -> bool:
        raise ConnectionError("refused")

    monkeypatch.setitem(health.CHECKS, "db", down)
    monkeypatch.setitem(health.CHECKS, "redis", lambda: _true())
    response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down", "redis": "up", "minio": "up"}


async def _true() -> bool:
    return True


# --- Shops & assets ----------------------------------------------------------------------------


def test_create_and_get_shop_applies_defaults(client):
    shop = create_shop(client)
    assert shop["brand_secondary_color"] == "#FFFFFF"
    assert shop["schedule_frequency"] == "TWICE_WEEKLY"
    assert shop["products"] == []

    fetched = client.get(f"/api/v1/shops/{shop['id']}").json()
    assert fetched["id"] == shop["id"] and fetched["name"] == "Chai Corner"


@pytest.mark.parametrize(
    "bad", [{"brand_primary_color": "brown"}, {"schedule_frequency": "HOURLY"}, {"name": ""}]
)
def test_create_shop_validates_input(client, bad):
    assert client.post("/api/v1/shops", json=SHOP | bad).status_code == 422


def test_unknown_shop_is_404(client):
    assert client.get(f"/api/v1/shops/{uuid.uuid4()}").status_code == 404
    assert upload(client, str(uuid.uuid4())).status_code == 404


def test_upload_asset_stores_object_and_returns_working_url(client, test_bucket):
    shop = create_shop(client)
    response = upload(client, shop["id"], discount="20% off")
    assert response.status_code == 201, response.text
    asset = response.json()

    assert asset["minio_image_key"] == f"shops/{shop['id']}/products/{asset['id']}.png"
    assert asset["discount_text"] == "20% off"
    head = test_bucket.head(asset["minio_image_key"])
    assert head["ContentType"] == "image/png" and head["ContentLength"] == len(PNG_1PX)

    # The presigned URL works without credentials (what the browser does).
    assert httpx.get(asset["image_url"]).content == PNG_1PX
    # And the product shows up on the shop.
    products = client.get(f"/api/v1/shops/{shop['id']}").json()["products"]
    assert [p["id"] for p in products] == [asset["id"]]


def test_upload_rejects_non_images_by_content_not_header(client):
    shop = create_shop(client)
    # Claims image/png, but the bytes are an HTML file.
    response = upload(client, shop["id"], data=b"<html><script>alert(1)</script></html>")
    assert response.status_code == 415


def test_upload_rejects_files_over_limit(client, monkeypatch):
    from app.routers import shops

    monkeypatch.setattr(shops, "MAX_UPLOAD_BYTES", 10)
    shop = create_shop(client)
    assert upload(client, shop["id"]).status_code == 413


# --- Campaigns ---------------------------------------------------------------------------------


def test_generate_campaign_queues_task_and_returns_202(client, dispatcher):
    shop = create_shop(client)
    response = client.post(
        "/api/v1/campaigns/generate", json={"shop_id": shop["id"], "format": "REEL"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "QUEUED"
    assert dispatcher.dispatched == [uuid.UUID(body["campaign_id"])]

    campaign = client.get(f"/api/v1/campaigns/{body['campaign_id']}").json()
    assert campaign["status"] == "QUEUED" and campaign["trigger"] == "MANUAL"
    assert campaign["format"] == "REEL" and campaign["media_url"] is None


def test_generate_marks_campaign_failed_when_queue_is_down(client, dispatcher):
    dispatcher.fail = True
    shop = create_shop(client)
    response = client.post(
        "/api/v1/campaigns/generate", json={"shop_id": shop["id"], "format": "TWEET"}
    )
    assert response.status_code == 503

    from common.db import session_scope
    from common.models import Campaign

    with session_scope() as session:
        (campaign,) = session.query(Campaign).all()
        assert campaign.status is CampaignStatus.FAILED
        assert "redis down" in campaign.error_message


def test_generate_validates_shop_and_format(client):
    url = "/api/v1/campaigns/generate"
    assert (
        client.post(url, json={"shop_id": str(uuid.uuid4()), "format": "REEL"}).status_code == 404
    )
    shop = create_shop(client)
    assert client.post(url, json={"shop_id": shop["id"], "format": "GIF"}).status_code == 422


def test_get_unknown_campaign_is_404(client):
    assert client.get(f"/api/v1/campaigns/{uuid.uuid4()}").status_code == 404


# --- Logging -----------------------------------------------------------------------------------


def test_request_id_header_and_structured_access_log(client, capsys):
    configure_logging(service_name="api-gateway", json_logs=True)  # .env may say console
    shop = create_shop(client)
    capsys.readouterr()  # drop earlier output
    response = client.get(f"/api/v1/shops/{shop['id']}", headers={"X-Request-ID": "req-42"})
    assert response.headers["X-Request-ID"] == "req-42"

    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    access = [r for r in lines if r["event"] == "http_request"]
    assert access, "no access log line"
    record = access[-1]
    assert record["request_id"] == "req-42"
    assert record["route"] == "/api/v1/shops/{shop_id}"  # template, not the raw path
    assert record["status_code"] == 200 and record["method"] == "GET"
    assert record["service"] == "api-gateway"
    assert isinstance(record["duration_ms"], int)


def test_unmatched_paths_share_one_route_value():
    from app.middleware import route_template

    assert route_template({"path": "/wp-admin/login.php"}) == "<unmatched>"
