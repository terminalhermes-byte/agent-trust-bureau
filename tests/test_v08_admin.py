"""Tests for v0.8 Ops + Admin Completeness features.

Covers:
- API key management: POST /admin/keys, GET /admin/keys, POST /admin/keys/{id}/revoke
- Webhook replay: POST /admin/policy/webhooks/{id}/replay (job_id + last_failed)
- Webhook stats: GET /admin/policy/webhooks/{id}/stats
- Retention cleanup: CLI cleanup command
- Tenant isolation for all new endpoints
- Edge cases and error paths
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin_store import (
    cleanup_old_records,
    create_api_key_for_tenant,
    get_webhook_stats,
    list_api_keys,
    replay_webhook_job,
    revoke_api_key,
)
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
    WebhookJob,
)
from app.rate_limit import reset_rate_limits

_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


def _make_db_and_seed(*, tenant2: bool = False, with_webhook: bool = True):
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
    if with_webhook:
        db.add(PolicyWebhook(tenant_id=1, url="https://hooks.example.com/policy", secret="test-webhook-secret", enabled=True))
    if tenant2:
        db.add(PolicyConfig(tenant_id=2, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
        db.add(PolicyWebhook(tenant_id=2, url="https://hooks.example.com/other", secret="other-secret", enabled=True))
    db.commit()
    db.close()
    return engine, sf


@pytest.fixture
def admin_client():
    """TestClient with auth enabled for admin endpoint testing."""
    engine, sf = _make_db_and_seed()

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


@pytest.fixture
def multi_tenant_client():
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


@pytest.fixture
def store_db():
    """Raw session factory for store-level tests."""
    engine, sf = _make_db_and_seed()
    yield sf
    Base.metadata.drop_all(bind=engine)


# ==========================================================================
# API Key Management — Endpoints
# ==========================================================================

class TestApiKeyEndpoints:
    def test_create_key(self, admin_client) -> None:
        client, sf = admin_client
        r = client.post("/v1/admin/keys", json={"name": "my-key"}, headers=_H1)
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "my-key"
        assert data["is_active"] is True
        assert data["raw_key"] is not None
        assert data["raw_key"].startswith("atb_")
        assert data["key_prefix"] == data["raw_key"][:12]
        assert data["revoked_at"] is None

    def test_create_key_default_name(self, admin_client) -> None:
        client, sf = admin_client
        r = client.post("/v1/admin/keys", json={}, headers=_H1)
        assert r.status_code == 201
        assert r.json()["name"] == "default"

    def test_list_keys(self, admin_client) -> None:
        client, sf = admin_client
        # Create two keys
        client.post("/v1/admin/keys", json={"name": "key-a"}, headers=_H1)
        client.post("/v1/admin/keys", json={"name": "key-b"}, headers=_H1)

        r = client.get("/v1/admin/keys", headers=_H1)
        assert r.status_code == 200
        data = r.json()
        # Should include the original bootstrap key + 2 new ones
        assert data["count"] >= 3
        # raw_key must NOT be returned in list
        for k in data["keys"]:
            assert k["raw_key"] is None

    def test_revoke_key(self, admin_client) -> None:
        client, sf = admin_client
        # Create a key to revoke
        create_resp = client.post("/v1/admin/keys", json={"name": "doomed"}, headers=_H1)
        key_id = create_resp.json()["id"]

        r = client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H1)
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        assert data["revoked_at"] is not None

    def test_revoke_key_idempotent(self, admin_client) -> None:
        client, sf = admin_client
        create_resp = client.post("/v1/admin/keys", json={"name": "double-revoke"}, headers=_H1)
        key_id = create_resp.json()["id"]

        # Revoke twice — should be idempotent
        r1 = client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H1)
        r2 = client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H1)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["is_active"] is False

    def test_revoke_nonexistent_key(self, admin_client) -> None:
        client, sf = admin_client
        r = client.post("/v1/admin/keys/99999/revoke", headers=_H1)
        assert r.status_code == 404

    def test_revoked_key_cannot_authenticate(self, admin_client) -> None:
        client, sf = admin_client
        # Create and capture raw key
        create_resp = client.post("/v1/admin/keys", json={"name": "auth-test"}, headers=_H1)
        raw_key = create_resp.json()["raw_key"]
        key_id = create_resp.json()["id"]

        # Key works before revocation
        r1 = client.get("/v1/admin/keys", headers={"X-API-Key": raw_key})
        assert r1.status_code == 200

        # Revoke
        client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H1)

        # Key no longer works
        r2 = client.get("/v1/admin/keys", headers={"X-API-Key": raw_key})
        assert r2.status_code == 401

    def test_key_requires_auth(self, admin_client) -> None:
        client, sf = admin_client
        r = client.get("/v1/admin/keys")
        assert r.status_code == 401

    def test_key_tenant_isolation(self, multi_tenant_client) -> None:
        client, sf = multi_tenant_client
        # Create key for tenant A
        r1 = client.post("/v1/admin/keys", json={"name": "t1-only"}, headers=_H1)
        key_id = r1.json()["id"]

        # Tenant B cannot revoke tenant A's key
        r2 = client.post(f"/v1/admin/keys/{key_id}/revoke", headers=_H2)
        assert r2.status_code == 404

        # Tenant B list doesn't include tenant A's keys
        r3 = client.get("/v1/admin/keys", headers=_H2)
        t2_key_names = [k["name"] for k in r3.json()["keys"]]
        assert "t1-only" not in t2_key_names


# ==========================================================================
# Webhook Replay — Endpoints
# ==========================================================================

def _seed_failed_job(sf, *, state: str = "dead", webhook_id: int = 1, tenant_id: int = 1):
    """Insert a failed/dead webhook job for replay testing."""
    db = sf()
    job = WebhookJob(
        webhook_id=webhook_id,
        tenant_id=tenant_id,
        agent_id="agent-replay",
        payload=json.dumps({"agent_id": "agent-replay", "decision": "block"}),
        state=state,
        attempts=3,
        max_attempts=5,
        last_error="HTTP 500",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    db.close()
    return job_id


class TestWebhookReplay:
    def test_replay_by_job_id(self, admin_client) -> None:
        client, sf = admin_client
        job_id = _seed_failed_job(sf)

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H1,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["replayed"] == 1
        assert data["jobs"][0]["state"] == "pending"
        assert data["jobs"][0]["id"] == job_id

    def test_replay_last_failed(self, admin_client) -> None:
        client, sf = admin_client
        _seed_failed_job(sf, state="failed")

        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"last_failed": True},
            headers=_H1,
        )
        assert r.status_code == 200
        assert r.json()["replayed"] == 1
        assert r.json()["jobs"][0]["state"] == "pending"

    def test_replay_rejects_active_job(self, admin_client) -> None:
        client, sf = admin_client
        # Seed a pending job (not failed/dead)
        db = sf()
        job = WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-active",
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

    def test_replay_rejects_both_params(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": 1, "last_failed": True},
            headers=_H1,
        )
        assert r.status_code == 422  # validation error

    def test_replay_rejects_neither_param(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={},
            headers=_H1,
        )
        assert r.status_code == 422

    def test_replay_not_found_job(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": 99999},
            headers=_H1,
        )
        assert r.status_code == 404

    def test_replay_no_failed_jobs(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"last_failed": True},
            headers=_H1,
        )
        assert r.status_code == 404

    def test_replay_tenant_isolation(self, multi_tenant_client) -> None:
        client, sf = multi_tenant_client
        # Seed failed job for tenant A
        job_id = _seed_failed_job(sf, tenant_id=1, webhook_id=1)

        # Get tenant A webhook
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        # Tenant B cannot replay tenant A's job (webhook 404)
        r = client.post(
            f"/v1/admin/policy/webhooks/{wh_id}/replay",
            json={"job_id": job_id},
            headers=_H2,
        )
        assert r.status_code == 404


# ==========================================================================
# Webhook Stats — Endpoints
# ==========================================================================

def _seed_jobs_and_deliveries(sf, webhook_id: int = 1, tenant_id: int = 1):
    """Seed a mix of jobs and deliveries for stats testing."""
    db = sf()
    # Jobs: 2 pending, 1 in_progress, 1 failed, 1 dead, 3 completed
    for state, count in [("pending", 2), ("in_progress", 1), ("failed", 1), ("dead", 1), ("completed", 3)]:
        for _ in range(count):
            db.add(WebhookJob(
                webhook_id=webhook_id, tenant_id=tenant_id,
                agent_id="agent-stats", payload="{}", state=state,
                attempts=1, max_attempts=5,
            ))
    # Deliveries: 7 success, 3 failure
    for i in range(10):
        db.add(WebhookDelivery(
            webhook_id=webhook_id, tenant_id=tenant_id,
            attempt=1, status_code=200 if i < 7 else 500,
            success=(i < 7),
        ))
    db.commit()
    db.close()


class TestWebhookStats:
    def test_stats_endpoint(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]
        _seed_jobs_and_deliveries(sf, webhook_id=wh_id)

        r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/stats", headers=_H1)
        assert r.status_code == 200
        data = r.json()
        assert data["webhook_id"] == wh_id
        assert data["pending"] == 2
        assert data["in_progress"] == 1
        assert data["failed"] == 1
        assert data["dead"] == 1
        assert data["completed"] == 3
        assert data["recent_success_rate"] == 70.0

    def test_stats_empty(self, admin_client) -> None:
        client, sf = admin_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/stats", headers=_H1)
        assert r.status_code == 200
        data = r.json()
        assert data["pending"] == 0
        assert data["completed"] == 0
        assert data["recent_success_rate"] is None

    def test_stats_tenant_isolation(self, multi_tenant_client) -> None:
        client, sf = multi_tenant_client
        wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
        wh_id = wh_resp.json()["webhooks"][0]["id"]

        # Tenant B gets 404 for tenant A's webhook
        r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/stats", headers=_H2)
        assert r.status_code == 404


# ==========================================================================
# Retention Cleanup — Store-level
# ==========================================================================

class TestRetentionCleanup:
    def test_cleanup_deletes_old_records(self, store_db) -> None:
        db = store_db()
        old_time = datetime.now(timezone.utc) - timedelta(days=60)

        # Seed old records
        db.add(WebhookDelivery(
            webhook_id=1, tenant_id=1, attempt=1, success=True,
            status_code=200, created_at=old_time,
        ))
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-old",
            payload="{}", state="completed", attempts=1, max_attempts=5,
            created_at=old_time,
        ))
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-dead-old",
            payload="{}", state="dead", attempts=5, max_attempts=5,
            created_at=old_time,
        ))
        # Seed recent records
        db.add(WebhookDelivery(
            webhook_id=1, tenant_id=1, attempt=1, success=True, status_code=200,
        ))
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-recent",
            payload="{}", state="completed", attempts=1, max_attempts=5,
        ))
        db.commit()

        before = datetime.now(timezone.utc) - timedelta(days=30)
        d_del, j_del = cleanup_old_records(db, before=before)
        db.close()

        assert d_del == 1  # one old delivery
        assert j_del == 2  # two old terminal jobs

    def test_cleanup_skips_active_jobs(self, store_db) -> None:
        db = store_db()
        old_time = datetime.now(timezone.utc) - timedelta(days=60)

        # Old job that is still pending (should NOT be deleted)
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-pending-old",
            payload="{}", state="pending", attempts=0, max_attempts=5,
            created_at=old_time,
        ))
        # Old job that is failed (should NOT be deleted)
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-failed-old",
            payload="{}", state="failed", attempts=2, max_attempts=5,
            created_at=old_time,
        ))
        db.commit()

        before = datetime.now(timezone.utc) - timedelta(days=30)
        d_del, j_del = cleanup_old_records(db, before=before)
        db.close()

        assert j_del == 0  # neither pending nor failed should be deleted

    def test_cleanup_dry_run(self, store_db) -> None:
        db = store_db()
        old_time = datetime.now(timezone.utc) - timedelta(days=60)

        db.add(WebhookDelivery(
            webhook_id=1, tenant_id=1, attempt=1, success=True,
            status_code=200, created_at=old_time,
        ))
        db.add(WebhookJob(
            webhook_id=1, tenant_id=1, agent_id="agent-dry",
            payload="{}", state="completed", attempts=1, max_attempts=5,
            created_at=old_time,
        ))
        db.commit()

        before = datetime.now(timezone.utc) - timedelta(days=30)
        d_del, j_del = cleanup_old_records(db, before=before, dry_run=True)

        # Counts returned but nothing actually deleted
        assert d_del == 1
        assert j_del == 1

        # Verify records still exist
        deliveries = list(db.scalars(select(WebhookDelivery)).all())
        jobs = list(db.scalars(select(WebhookJob)).all())
        assert len(deliveries) == 1
        assert len(jobs) == 1
        db.close()


# ==========================================================================
# API Key Management — Store-level
# ==========================================================================

class TestApiKeyStore:
    def test_create_and_list(self, store_db) -> None:
        db = store_db()
        key_row, raw_key = create_api_key_for_tenant(db, 1, name="store-test")
        assert raw_key.startswith("atb_")
        assert key_row.name == "store-test"
        assert key_row.is_active is True

        keys = list_api_keys(db, 1)
        names = [k.name for k in keys]
        assert "store-test" in names
        db.close()

    def test_revoke_sets_inactive(self, store_db) -> None:
        db = store_db()
        key_row, _ = create_api_key_for_tenant(db, 1, name="revoke-test")
        revoked = revoke_api_key(db, key_row.id, 1)
        assert revoked.is_active is False
        assert revoked.revoked_at is not None
        db.close()

    def test_revoke_wrong_tenant(self, store_db) -> None:
        db = store_db()
        key_row, _ = create_api_key_for_tenant(db, 1, name="wrong-tenant")
        result = revoke_api_key(db, key_row.id, 999)
        assert result is None
        db.close()


# ==========================================================================
# Webhook Stats — Store-level
# ==========================================================================

class TestWebhookStatsStore:
    def test_stats_counts(self, store_db) -> None:
        db = store_db()
        wh = db.scalars(select(PolicyWebhook).where(PolicyWebhook.tenant_id == 1)).first()

        for state in ["pending", "pending", "completed", "dead"]:
            db.add(WebhookJob(
                webhook_id=wh.id, tenant_id=1, agent_id="a",
                payload="{}", state=state, attempts=1, max_attempts=5,
            ))
        for s in [True, True, False]:
            db.add(WebhookDelivery(
                webhook_id=wh.id, tenant_id=1, attempt=1,
                status_code=200 if s else 500, success=s,
            ))
        db.commit()

        stats = get_webhook_stats(db, wh.id, 1)
        assert stats["pending"] == 2
        assert stats["completed"] == 1
        assert stats["dead"] == 1
        assert stats["failed"] == 0
        # 2/3 successful
        assert abs(stats["recent_success_rate"] - 66.7) < 0.1
        db.close()
