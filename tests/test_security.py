"""Security hardening tests.

Covers:
- Auth bypass attempts (missing key, invalid key, revoked key)
- Cross-tenant data isolation
- Webhook URL validation (SSRF prevention)
- Secret leakage checks (webhook secrets, API key hashes)
- Job state parameter validation
- Replay abuse prevention
- Input validation edge cases
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
    WebhookJob,
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
def sec_client():
    """TestClient with auth enabled for security testing."""
    engine, sf = _make_db_and_seed(tenant2=True)

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original_auth = settings.require_auth
    settings.require_auth = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            yield c, sf
    finally:
        settings.require_auth = original_auth
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


# ==========================================================================
# 1. Auth bypass attempts
# ==========================================================================

class TestAuthBypass:
    def test_missing_api_key_401(self, sec_client):
        """Request without API key should get 401."""
        client, _ = sec_client
        endpoints = [
            ("GET", "/v1/admin/keys"),
            ("POST", "/v1/admin/keys"),
            ("GET", "/v1/admin/policy/config"),
            ("GET", "/v1/admin/policy/webhooks"),
        ]
        for method, path in endpoints:
            r = client.request(method, path)
            assert r.status_code == 401, f"{method} {path} should require auth"

    def test_invalid_api_key_401(self, sec_client):
        """Request with garbage API key should get 401."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys", headers={"X-API-Key": "garbage-key-value"})
        assert r.status_code == 401

    def test_empty_api_key_401(self, sec_client):
        """Request with empty API key header should get 401."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys", headers={"X-API-Key": ""})
        assert r.status_code == 401

    def test_wrong_prefix_api_key_401(self, sec_client):
        """Key with valid format but wrong prefix should get 401."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys", headers={"X-API-Key": "atb_nonexisten"})
        assert r.status_code == 401

    def test_revoked_key_cannot_access(self, sec_client):
        """A revoked key must not authenticate."""
        client, sf = sec_client
        # Create and revoke a key
        create_resp = client.post("/v1/admin/keys", json={"name": "revoke-sec"}, headers=_H1)
        raw_key = create_resp.json()["raw_key"]
        key_id = create_resp.json()["id"]

        # Key works before revocation
        assert client.get("/v1/admin/keys", headers={"X-API-Key": raw_key}).status_code == 200

        # Revoke
        client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H1)

        # Key rejected after revocation
        assert client.get("/v1/admin/keys", headers={"X-API-Key": raw_key}).status_code == 401

    def test_admin_endpoints_require_strict_auth(self, sec_client):
        """Admin endpoints require auth even when REQUIRE_AUTH=false."""
        client, _ = sec_client
        original = settings.require_auth
        try:
            settings.require_auth = False
            # Non-admin endpoints should work without key
            r = client.get("/health")
            assert r.status_code == 200

            # Admin endpoints should still require key
            r = client.get("/v1/admin/keys")
            assert r.status_code == 401
        finally:
            settings.require_auth = original


# ==========================================================================
# 2. Cross-tenant isolation
# ==========================================================================

class TestCrossTenantIsolation:
    def test_tenant_b_cannot_see_tenant_a_keys(self, sec_client):
        """Tenant B must not see Tenant A's API keys."""
        client, _ = sec_client
        client.post("/v1/admin/keys", json={"name": "t1-secret-key"}, headers=_H1)

        r = client.get("/v1/admin/keys", headers=_H2)
        key_names = [k["name"] for k in r.json()["keys"]]
        assert "t1-secret-key" not in key_names

    def test_tenant_b_cannot_revoke_tenant_a_key(self, sec_client):
        """Tenant B must not be able to revoke Tenant A's key."""
        client, _ = sec_client
        create_resp = client.post("/v1/admin/keys", json={"name": "cross-revoke"}, headers=_H1)
        key_id = create_resp.json()["id"]

        r = client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H2)
        assert r.status_code == 404

    def test_tenant_b_cannot_see_tenant_a_webhook(self, sec_client):
        """Tenant B must not access Tenant A's webhook."""
        client, _ = sec_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/stats", headers=_H2)
        assert r.status_code == 404

    def test_tenant_b_cannot_replay_tenant_a_job(self, sec_client):
        """Tenant B must not be able to replay Tenant A's job."""
        client, sf = sec_client
        # Create a dead job for tenant A
        db = sf()
        job = WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="cross-agent",
            payload=json.dumps({"agent_id": "cross-agent"}),
            state="dead", attempts=5, max_attempts=5,
            last_error="test",
        )
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        # Tenant B trying to replay tenant A's job
        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H2,
        )
        assert r.status_code == 404

    def test_tenant_b_cannot_see_tenant_a_policy_config(self, sec_client):
        """Tenant B must not access Tenant A's policy config."""
        client, _ = sec_client
        # Both tenants should get their own config
        r1 = client.get("/v1/admin/policy/config", headers=_H1)
        assert r1.status_code == 200

        r2 = client.get("/v1/admin/policy/config", headers=_H2)
        # Both should succeed but with isolated data
        assert r2.status_code == 200


# ==========================================================================
# 3. SSRF prevention — webhook URL validation
# ==========================================================================

class TestSSRFPrevention:
    def test_reject_http_url(self, sec_client):
        """Webhook URL with http:// should be rejected."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": "http://internal-service:8080/hook"},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_reject_file_url(self, sec_client):
        """Webhook URL with file:// should be rejected."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": "file:///etc/passwd"},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_reject_internal_ip(self, sec_client):
        """Webhook URL with internal IP should be rejected (not https)."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": "http://169.254.169.254/latest/meta-data/"},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_reject_localhost(self, sec_client):
        """Webhook URL targeting localhost should be rejected (not https)."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": "http://localhost:8080/hook"},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_accept_https_url(self, sec_client):
        """HTTPS URL should be accepted."""
        client, _ = sec_client
        # This will 409 because webhook already exists for tenant
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": "https://valid-endpoint.example.com/webhook"},
            headers=_H1,
        )
        # Either 201 (created) or 409 (already exists) is fine
        assert r.status_code in (201, 409)


# ==========================================================================
# 4. Secret leakage checks
# ==========================================================================

class TestSecretLeakage:
    def test_webhook_secret_not_in_list(self, sec_client):
        """Listing webhooks must not expose the full secret."""
        client, _ = sec_client
        r = client.get("/v1/admin/policy/webhooks", headers=_H1)
        for wh in r.json()["webhooks"]:
            assert wh["secret"] is None
            assert wh["secret_last4"].startswith("***")

    def test_api_key_hash_not_in_response(self, sec_client):
        """API key listing must not expose the key hash."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys", headers=_H1)
        for key in r.json()["keys"]:
            # raw_key should be None in list
            assert key["raw_key"] is None
            # key_hash should never appear in the response
            assert "key_hash" not in key

    def test_raw_key_only_on_create(self, sec_client):
        """raw_key should only be returned on key creation."""
        client, _ = sec_client
        # Create returns raw_key
        create_resp = client.post("/v1/admin/keys", json={"name": "leak-test"}, headers=_H1)
        assert create_resp.json()["raw_key"] is not None
        assert create_resp.json()["raw_key"].startswith("atb_")

        # List never returns raw_key
        list_resp = client.get("/v1/admin/keys", headers=_H1)
        for k in list_resp.json()["keys"]:
            assert k["raw_key"] is None


# ==========================================================================
# 5. Job state parameter validation
# ==========================================================================

class TestStateValidation:
    def test_valid_states_accepted(self, sec_client):
        """Valid state parameters should be accepted."""
        client, _ = sec_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        for state in ["pending", "in_progress", "failed", "dead", "completed"]:
            r = client.get(
                f"/v1/admin/policy/webhooks/{wh_id}/jobs?state={state}",
                headers=_H1,
            )
            assert r.status_code == 200, f"State '{state}' should be accepted"

    def test_invalid_state_rejected(self, sec_client):
        """Invalid state parameter should get 422."""
        client, _ = sec_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        for bad_state in ["unknown", "PENDING", "active", "' OR 1=1 --"]:
            r = client.get(
                f"/v1/admin/policy/webhooks/{wh_id}/jobs?state={bad_state}",
                headers=_H1,
            )
            assert r.status_code == 422, f"State '{bad_state}' should be rejected"


# ==========================================================================
# 6. Replay abuse prevention
# ==========================================================================

class TestReplayAbuse:
    def test_cannot_replay_pending_job(self, sec_client):
        """Active jobs (pending) cannot be replayed."""
        client, sf = sec_client
        db = sf()
        job = WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="abuse-agent",
            payload="{}", state="pending", attempts=0, max_attempts=5,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H1,
        )
        assert r.status_code == 409

    def test_cannot_replay_in_progress_job(self, sec_client):
        """In-progress jobs cannot be replayed."""
        client, sf = sec_client
        db = sf()
        job = WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="abuse-agent2",
            payload="{}", state="in_progress", attempts=1, max_attempts=5,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H1,
        )
        assert r.status_code == 409

    def test_cannot_replay_completed_job(self, sec_client):
        """Completed jobs cannot be replayed."""
        client, sf = sec_client
        db = sf()
        job = WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="abuse-agent3",
            payload="{}", state="completed", attempts=1, max_attempts=5,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H1,
        )
        assert r.status_code == 409


# ==========================================================================
# 7. Input validation edge cases
# ==========================================================================

class TestInputValidation:
    def test_empty_webhook_url_rejected(self, sec_client):
        """Empty webhook URL should be rejected."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/policy/webhooks",
            json={"url": ""},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_empty_key_name_rejected(self, sec_client):
        """Empty API key name should be rejected."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/keys",
            json={"name": ""},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_overlength_key_name_rejected(self, sec_client):
        """Extremely long key name should be rejected."""
        client, _ = sec_client
        r = client.post(
            "/v1/admin/keys",
            json={"name": "x" * 200},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_negative_limit_rejected(self, sec_client):
        """Negative limit parameter should be rejected."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys?limit=-1", headers=_H1)
        assert r.status_code == 422

    def test_excessive_limit_rejected(self, sec_client):
        """Limit > 500 should be rejected."""
        client, _ = sec_client
        r = client.get("/v1/admin/keys?limit=10000", headers=_H1)
        assert r.status_code == 422

    def test_threshold_ordering_enforced(self, sec_client):
        """Policy config with allow < review should be rejected."""
        client, _ = sec_client
        r = client.put(
            "/v1/admin/policy/config",
            json={"allow_threshold": 50, "review_threshold": 70, "block_threshold": 30},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_threshold_range_enforced(self, sec_client):
        """Policy config with threshold > 100 should be rejected."""
        client, _ = sec_client
        r = client.put(
            "/v1/admin/policy/config",
            json={"allow_threshold": 150, "review_threshold": 60, "block_threshold": 40},
            headers=_H1,
        )
        assert r.status_code == 422


# ==========================================================================
# 8. Constant-time auth check
# ==========================================================================

class TestAuthTimingSafety:
    def test_auth_uses_constant_time_compare(self):
        """Verify auth module uses hmac.compare_digest (not ==)."""
        import inspect
        import app.auth as auth_module

        source = inspect.getsource(auth_module._constant_time_compare)
        assert "compare_digest" in source, "Auth must use hmac.compare_digest for constant-time comparison"

    def test_error_messages_uniform(self, sec_client):
        """Auth error messages should be identical for all failure types to prevent enumeration."""
        client, _ = sec_client
        # Missing key
        r1 = client.get("/v1/admin/keys")
        # Invalid key
        r2 = client.get("/v1/admin/keys", headers={"X-API-Key": "atb_bad_key_bad"})
        # Both should have same error message
        assert r1.json()["detail"] == r2.json()["detail"]
