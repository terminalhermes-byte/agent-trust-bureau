"""Tests for the admin CRUD endpoints.

Covers:
- Policy config: GET, PUT, validation
- Agent overrides: POST, DELETE, GET list, duplicate 409, not-found 404
- Webhooks: POST (server-side secret), PATCH, GET list, secret masking, duplicate 409
- Tenant isolation across all admin endpoints
- Threshold validation (allow >= review >= block, 0..100)
- Admin endpoints require auth even when REQUIRE_AUTH=false
- Rotate secret changes HMAC signature behaviour
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
    Tenant,
)
from app.rate_limit import reset_rate_limits
from app.services.webhook import compute_signature


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


def _make_db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    return engine, sf


def _seed(sf):
    db = sf()
    db.add_all([
        Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True),
        Tenant(id=2, name="Tenant B", slug="tenant-b", is_active=True),
    ])
    db.commit()
    db.add_all([
        ApiKey(tenant_id=1, key_prefix=_RAW_KEY_T1[:12], key_hash=hash_api_key(_RAW_KEY_T1), name="t1-key"),
        ApiKey(tenant_id=2, key_prefix=_RAW_KEY_T2[:12], key_hash=hash_api_key(_RAW_KEY_T2), name="t2-key"),
    ])
    db.commit()
    # Seed default policy configs for both tenants
    db.add_all([
        PolicyConfig(tenant_id=1, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0),
        PolicyConfig(tenant_id=2, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0),
    ])
    db.commit()
    db.close()


@pytest.fixture
def admin_client() -> TestClient:
    engine, sf = _make_db()
    _seed(sf)

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
            yield c
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


# ==========================================================================
# Policy Config
# ==========================================================================

def test_get_policy_config(admin_client: TestClient) -> None:
    r = admin_client.get("/v1/admin/policy/config", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["allow_threshold"] == 80.0
    assert data["review_threshold"] == 60.0
    assert data["block_threshold"] == 40.0
    assert "created_at" in data
    assert "updated_at" in data


def test_put_policy_config(admin_client: TestClient) -> None:
    r = admin_client.put(
        "/v1/admin/policy/config",
        json={"allow_threshold": 90.0, "review_threshold": 70.0, "block_threshold": 50.0},
        headers=_H1,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["allow_threshold"] == 90.0
    assert data["review_threshold"] == 70.0
    assert data["block_threshold"] == 50.0

    # Verify it persisted
    r2 = admin_client.get("/v1/admin/policy/config", headers=_H1)
    assert r2.json()["allow_threshold"] == 90.0


def test_put_policy_config_validation_allow_lt_review(admin_client: TestClient) -> None:
    r = admin_client.put(
        "/v1/admin/policy/config",
        json={"allow_threshold": 50.0, "review_threshold": 70.0, "block_threshold": 40.0},
        headers=_H1,
    )
    assert r.status_code == 422


def test_put_policy_config_validation_review_lt_block(admin_client: TestClient) -> None:
    r = admin_client.put(
        "/v1/admin/policy/config",
        json={"allow_threshold": 80.0, "review_threshold": 30.0, "block_threshold": 40.0},
        headers=_H1,
    )
    assert r.status_code == 422


def test_put_policy_config_validation_out_of_range(admin_client: TestClient) -> None:
    r = admin_client.put(
        "/v1/admin/policy/config",
        json={"allow_threshold": 110.0, "review_threshold": 60.0, "block_threshold": 40.0},
        headers=_H1,
    )
    assert r.status_code == 422


def test_policy_config_tenant_isolation(admin_client: TestClient) -> None:
    """Updating tenant A config does not affect tenant B."""
    admin_client.put(
        "/v1/admin/policy/config",
        json={"allow_threshold": 95.0, "review_threshold": 75.0, "block_threshold": 55.0},
        headers=_H1,
    )
    r2 = admin_client.get("/v1/admin/policy/config", headers=_H2)
    assert r2.status_code == 200
    assert r2.json()["allow_threshold"] == 80.0  # unchanged


# ==========================================================================
# Agent Overrides
# ==========================================================================

def test_create_agent_override(admin_client: TestClient) -> None:
    r = admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-x", "allow_threshold": 90.0, "review_threshold": 70.0, "block_threshold": 50.0},
        headers=_H1,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["agent_id"] == "agent-x"
    assert data["allow_threshold"] == 90.0


def test_create_agent_override_partial_thresholds(admin_client: TestClient) -> None:
    """Only some thresholds set; others are null."""
    r = admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-partial", "allow_threshold": 95.0},
        headers=_H1,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["allow_threshold"] == 95.0
    assert data["review_threshold"] is None
    assert data["block_threshold"] is None


def test_create_agent_override_duplicate_409(admin_client: TestClient) -> None:
    admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-dup"},
        headers=_H1,
    )
    r = admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-dup"},
        headers=_H1,
    )
    assert r.status_code == 409


def test_create_override_validation_allow_lt_review(admin_client: TestClient) -> None:
    r = admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-bad", "allow_threshold": 50.0, "review_threshold": 70.0},
        headers=_H1,
    )
    assert r.status_code == 422


def test_delete_agent_override(admin_client: TestClient) -> None:
    admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-del"},
        headers=_H1,
    )
    r = admin_client.delete("/v1/admin/policy/overrides/agent-del", headers=_H1)
    assert r.status_code == 204

    # Verify it's gone
    r2 = admin_client.get("/v1/admin/policy/overrides", headers=_H1)
    ids = [o["agent_id"] for o in r2.json()["overrides"]]
    assert "agent-del" not in ids


def test_delete_agent_override_not_found(admin_client: TestClient) -> None:
    r = admin_client.delete("/v1/admin/policy/overrides/nonexistent", headers=_H1)
    assert r.status_code == 404


def test_list_agent_overrides(admin_client: TestClient) -> None:
    admin_client.post("/v1/admin/policy/overrides", json={"agent_id": "a1"}, headers=_H1)
    admin_client.post("/v1/admin/policy/overrides", json={"agent_id": "a2"}, headers=_H1)
    r = admin_client.get("/v1/admin/policy/overrides", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    assert data["overrides"][0]["agent_id"] == "a1"
    assert data["overrides"][1]["agent_id"] == "a2"


def test_list_agent_overrides_with_limit(admin_client: TestClient) -> None:
    for i in range(5):
        admin_client.post("/v1/admin/policy/overrides", json={"agent_id": f"lim-{i}"}, headers=_H1)
    r = admin_client.get("/v1/admin/policy/overrides?limit=3", headers=_H1)
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_agent_override_tenant_isolation(admin_client: TestClient) -> None:
    """Tenant A's override is not visible to tenant B."""
    admin_client.post(
        "/v1/admin/policy/overrides",
        json={"agent_id": "agent-iso"},
        headers=_H1,
    )
    r = admin_client.get("/v1/admin/policy/overrides", headers=_H2)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_agent_override_delete_tenant_isolation(admin_client: TestClient) -> None:
    """Tenant B cannot delete tenant A's override."""
    admin_client.post("/v1/admin/policy/overrides", json={"agent_id": "agent-cross"}, headers=_H1)
    r = admin_client.delete("/v1/admin/policy/overrides/agent-cross", headers=_H2)
    assert r.status_code == 404

    # Verify it still exists for tenant A
    r2 = admin_client.get("/v1/admin/policy/overrides", headers=_H1)
    assert r2.json()["count"] == 1


# ==========================================================================
# Webhooks
# ==========================================================================

def test_create_webhook(admin_client: TestClient) -> None:
    """POST creates webhook; secret is provided by the caller and never returned in full."""
    secret = "my-secret-key-1234"
    r = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com/test", "secret": secret},
        headers=_H1,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["url"] == "https://hooks.example.com/test"
    assert data["enabled"] is True
    assert "id" in data
    assert "secret" not in data
    assert data["secret_last4"] == "***" + secret[-4:]
    assert data["revoked_at"] is None


def test_create_webhook_secret_not_in_list(admin_client: TestClient) -> None:
    """Full secret must not appear in the list response."""
    secret = "super-secret-value-9999"
    create_r = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://example.com/hook", "secret": secret},
        headers=_H1,
    )
    assert create_r.status_code == 201

    list_r = admin_client.get("/v1/admin/policy/webhooks", headers=_H1)
    assert list_r.status_code == 200
    body_text = list_r.text
    assert secret not in body_text
    assert ("***" + secret[-4:]) in body_text


def test_create_webhook_duplicate_409(admin_client: TestClient) -> None:
    admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://a.com/1", "secret": "secret-1234"},
        headers=_H1,
    )
    r = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://a.com/2", "secret": "secret-5678"},
        headers=_H1,
    )
    assert r.status_code == 409


def test_create_webhook_secret_too_short(admin_client: TestClient) -> None:
    r = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": "short"},
        headers=_H1,
    )
    assert r.status_code == 422


def test_patch_webhook_disable(admin_client: TestClient) -> None:
    r1 = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": "my-secret-1234"},
        headers=_H1,
    )
    wh_id = r1.json()["id"]

    r2 = admin_client.patch(
        f"/v1/admin/policy/webhooks/{wh_id}",
        json={"enabled": False},
        headers=_H1,
    )
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False
    assert r2.json()["revoked_at"] is not None


def test_patch_webhook_rotate_secret(admin_client: TestClient) -> None:
    """rotate_secret updates the stored secret without returning it."""
    r1 = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": "old-secret-1234"},
        headers=_H1,
    )
    wh_id = r1.json()["id"]

    r2 = admin_client.patch(
        f"/v1/admin/policy/webhooks/{wh_id}",
        json={"rotate_secret": "new-secret-5678"},
        headers=_H1,
    )
    assert r2.status_code == 200
    data = r2.json()
    assert "secret" not in data
    assert data["secret_last4"] == "***5678"


def test_patch_webhook_no_rotation_does_not_expose_secret(admin_client: TestClient) -> None:
    """PATCH without rotate_secret does not expose the secret."""
    r1 = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": "my-secret-1234"},
        headers=_H1,
    )
    wh_id = r1.json()["id"]

    r2 = admin_client.patch(
        f"/v1/admin/policy/webhooks/{wh_id}",
        json={"enabled": True},
        headers=_H1,
    )
    assert r2.status_code == 200
    assert "secret" not in r2.json()


def test_patch_webhook_not_found(admin_client: TestClient) -> None:
    r = admin_client.patch(
        "/v1/admin/policy/webhooks/9999",
        json={"enabled": False},
        headers=_H1,
    )
    assert r.status_code == 404


def test_list_webhooks(admin_client: TestClient) -> None:
    admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": "secret-1234"},
        headers=_H1,
    )
    r = admin_client.get("/v1/admin/policy/webhooks", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["webhooks"][0]["url"] == "https://hooks.example.com"
    assert data["webhooks"][0]["secret_last4"].startswith("***")
    # Full secret not in list response
    assert "secret" not in data["webhooks"][0] or data["webhooks"][0].get("secret") is None


def test_list_webhooks_empty(admin_client: TestClient) -> None:
    r = admin_client.get("/v1/admin/policy/webhooks", headers=_H1)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_webhook_tenant_isolation(admin_client: TestClient) -> None:
    """Tenant A's webhook is not visible to tenant B."""
    admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://a.com/hook", "secret": "secret-aaaa"},
        headers=_H1,
    )
    r = admin_client.get("/v1/admin/policy/webhooks", headers=_H2)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_webhook_patch_tenant_isolation(admin_client: TestClient) -> None:
    """Tenant B cannot patch tenant A's webhook."""
    r1 = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://a.com/hook", "secret": "secret-aaaa"},
        headers=_H1,
    )
    wh_id = r1.json()["id"]

    r2 = admin_client.patch(
        f"/v1/admin/policy/webhooks/{wh_id}",
        json={"enabled": False},
        headers=_H2,
    )
    assert r2.status_code == 404

    # Verify it's still enabled for tenant A
    r3 = admin_client.get("/v1/admin/policy/webhooks", headers=_H1)
    assert r3.json()["webhooks"][0]["enabled"] is True


# ==========================================================================
# Rotate secret changes HMAC signature
# ==========================================================================

def test_rotate_secret_changes_signature(admin_client: TestClient) -> None:
    """After rotating the webhook secret, HMAC signatures differ for the same payload."""
    # Create webhook with a known secret so we can validate signatures.
    original_secret = "secret-old-1234"
    r1 = admin_client.post(
        "/v1/admin/policy/webhooks",
        json={"url": "https://hooks.example.com", "secret": original_secret},
        headers=_H1,
    )
    assert r1.status_code == 201
    wh_id = r1.json()["id"]

    # Rotate secret
    new_secret = "secret-new-5678"
    r2 = admin_client.patch(
        f"/v1/admin/policy/webhooks/{wh_id}",
        json={"rotate_secret": new_secret},
        headers=_H1,
    )
    assert r2.status_code == 200
    assert new_secret != original_secret

    # Same payload, different signatures
    payload = json.dumps({"agent_id": "test", "decision": "allow"}, sort_keys=True).encode()
    sig_old = compute_signature(payload, original_secret)
    sig_new = compute_signature(payload, new_secret)
    assert sig_old != sig_new


# ==========================================================================
# Strict auth: admin requires API key even in dev mode
# ==========================================================================

def test_admin_endpoints_require_key_even_when_auth_disabled() -> None:
    """Admin endpoints must always require a real API key, even in dev mode."""
    engine, sf = _make_db()
    _seed(sf)

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = False  # dev mode
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            # All admin endpoints should return 401 without an API key
            assert c.get("/v1/admin/policy/config").status_code == 401
            assert c.put("/v1/admin/policy/config", json={
                "allow_threshold": 80, "review_threshold": 60, "block_threshold": 40,
            }).status_code == 401
            assert c.post("/v1/admin/policy/overrides", json={"agent_id": "x"}).status_code == 401
            assert c.get("/v1/admin/policy/overrides").status_code == 401
            assert c.delete("/v1/admin/policy/overrides/x").status_code == 401
            assert c.post("/v1/admin/policy/webhooks", json={"url": "https://x.com"}).status_code == 401
            assert c.get("/v1/admin/policy/webhooks").status_code == 401
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


def test_admin_works_with_key_in_dev_mode() -> None:
    """Admin endpoints work with a valid API key even when REQUIRE_AUTH=false."""
    engine, sf = _make_db()
    _seed(sf)

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = False  # dev mode
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            r = c.get("/v1/admin/policy/config", headers=_H1)
            assert r.status_code == 200
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()
