"""Tests for webhook delivery logging and the admin deliveries endpoint.

Covers:
- Delivery logging on successful webhook send (status_code + success=True)
- Delivery logging on network error (error string + success=False)
- Delivery logging records every retry attempt
- GET /v1/admin/policy/webhooks/{id}/deliveries returns recent attempts
- Deliveries endpoint is tenant-isolated
- Deliveries endpoint returns 404 for non-existent webhook
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_api_key
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import (
    ApiKey,
    Base,
    PolicyConfig,
    PolicyWebhook,
    Tenant,
    WebhookDelivery,
)
from app.rate_limit import reset_rate_limits


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


def _make_db_and_seed(*, tenant2: bool = False):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    db = sf()
    db.add(Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True))
    if tenant2:
        db.add(Tenant(id=2, name="Tenant B", slug="tenant-b", is_active=True))
    db.commit()
    db.add(ApiKey(tenant_id=1, key_prefix=_RAW_KEY_T1[:12], key_hash=hash_api_key(_RAW_KEY_T1), name="t1-key"))
    if tenant2:
        db.add(ApiKey(tenant_id=2, key_prefix=_RAW_KEY_T2[:12], key_hash=hash_api_key(_RAW_KEY_T2), name="t2-key"))
    db.add(PolicyConfig(tenant_id=1, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
    db.add(PolicyWebhook(tenant_id=1, url="https://hooks.example.com/policy", secret="test-webhook-secret", enabled=True))
    if tenant2:
        db.add(PolicyConfig(tenant_id=2, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
        db.add(PolicyWebhook(tenant_id=2, url="https://hooks.example.com/other", secret="other-secret", enabled=True))
    db.commit()
    db.close()
    return engine, sf


@pytest.fixture
def delivery_client():
    engine, sf = _make_db_and_seed()

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            yield c, sf
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


@pytest.fixture
def multi_tenant_client():
    engine, sf = _make_db_and_seed(tenant2=True)

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            yield c, sf
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


# ==========================================================================
# Delivery logging on success
# ==========================================================================

def test_delivery_logged_on_success(delivery_client) -> None:
    """A successful webhook fires records a delivery with status_code and success=True."""
    client, sf = delivery_client

    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        r = client.get("/v1/policy/decision/agent-success", headers=_H1)
        assert r.status_code == 200

    # Check delivery records in DB
    db = sf()
    rows = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
    db.close()

    assert len(rows) == 1
    assert rows[0].attempt == 1
    assert rows[0].status_code == 200
    assert rows[0].error is None
    assert rows[0].success is True
    assert rows[0].tenant_id == 1


# ==========================================================================
# Delivery logging on network error
# ==========================================================================

def test_delivery_logged_on_network_error(delivery_client) -> None:
    """Network errors record deliveries with error string and success=False."""
    client, sf = delivery_client

    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.services.webhook.time.sleep"):
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        MockClientClass.return_value = mock_client

        r = client.get("/v1/policy/decision/agent-err", headers=_H1)
        assert r.status_code == 200

    db = sf()
    rows = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
    db.close()

    # All 3 retry attempts should be logged
    assert len(rows) == 3
    for i, row in enumerate(rows, start=1):
        assert row.attempt == i
        assert row.status_code is None
        assert "Connection refused" in row.error
        assert row.success is False


# ==========================================================================
# Delivery logging records every retry attempt
# ==========================================================================

def test_delivery_logged_per_retry_attempt(delivery_client) -> None:
    """Each retry attempt (fail then succeed) is recorded."""
    client, sf = delivery_client

    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.services.webhook.time.sleep"):
        mock_client = MagicMock()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_client.post.side_effect = [fail_resp, ok_resp]
        MockClientClass.return_value = mock_client

        r = client.get("/v1/policy/decision/agent-retry", headers=_H1)
        assert r.status_code == 200

    db = sf()
    rows = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
    db.close()

    assert len(rows) == 2
    # First attempt: failure
    assert rows[0].attempt == 1
    assert rows[0].status_code == 503
    assert rows[0].success is False
    # Second attempt: success
    assert rows[1].attempt == 2
    assert rows[1].status_code == 200
    assert rows[1].success is True


# ==========================================================================
# GET /v1/admin/policy/webhooks/{id}/deliveries
# ==========================================================================

def test_deliveries_endpoint_returns_attempts(delivery_client) -> None:
    """GET deliveries endpoint returns recent delivery attempts."""
    client, sf = delivery_client

    # Trigger a policy decision to create delivery records
    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        client.get("/v1/policy/decision/agent-log", headers=_H1)

    # Get the webhook ID
    wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
    wh_id = wh_resp.json()["webhooks"][0]["id"]

    # Fetch deliveries
    r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/deliveries", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    d = data["deliveries"][0]
    assert "id" in d
    assert "webhook_id" in d
    assert d["webhook_id"] == wh_id
    assert "attempt" in d
    assert "status_code" in d
    assert "success" in d
    assert "created_at" in d


def test_deliveries_endpoint_404_for_nonexistent_webhook(delivery_client) -> None:
    """Deliveries endpoint returns 404 for a webhook that doesn't exist."""
    client, _ = delivery_client
    r = client.get("/v1/admin/policy/webhooks/9999/deliveries", headers=_H1)
    assert r.status_code == 404


# ==========================================================================
# Tenant isolation for deliveries
# ==========================================================================

def test_deliveries_tenant_isolation(multi_tenant_client) -> None:
    """Tenant B cannot read tenant A's delivery logs."""
    client, sf = multi_tenant_client

    # Trigger a delivery for tenant A
    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        client.get("/v1/policy/decision/agent-iso", headers=_H1)

    # Get tenant A's webhook ID
    wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
    wh_id_a = wh_resp.json()["webhooks"][0]["id"]

    # Tenant A can see its deliveries
    r_a = client.get(f"/v1/admin/policy/webhooks/{wh_id_a}/deliveries", headers=_H1)
    assert r_a.status_code == 200
    assert r_a.json()["count"] >= 1

    # Tenant B cannot see tenant A's webhook (returns 404)
    r_b = client.get(f"/v1/admin/policy/webhooks/{wh_id_a}/deliveries", headers=_H2)
    assert r_b.status_code == 404
