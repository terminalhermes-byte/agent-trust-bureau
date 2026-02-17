"""Tests for async webhook job queue + worker processing.

These tests cover:
- Policy decision enqueues a WebhookJob when WEBHOOK_ASYNC is enabled
- Worker claim + process completes the job and logs a WebhookDelivery
- Idempotency: completed jobs are not re-claimed
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_api_key
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import ApiKey, Base, PolicyConfig, PolicyWebhook, Tenant, WebhookDelivery, WebhookJob
from app.rate_limit import reset_rate_limits
from app.services.webhook import claim_pending_job_sqlite, process_webhook_job


_RAW_KEY = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_H = {"X-API-Key": _RAW_KEY}


def _make_db_and_seed():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    db = sf()
    db.add(Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True))
    db.commit()
    db.add(ApiKey(tenant_id=1, key_prefix=_RAW_KEY[:12], key_hash=hash_api_key(_RAW_KEY), name="t1-key"))
    db.add(PolicyConfig(tenant_id=1, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
    db.add(PolicyWebhook(tenant_id=1, url="https://hooks.example.com/policy", secret="test-webhook-secret", enabled=True))
    db.commit()
    db.close()

    return engine, sf


def test_policy_decision_enqueues_job_when_async_enabled() -> None:
    engine, sf = _make_db_and_seed()

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    original_auth = settings.require_auth
    original_async = settings.webhook_async
    settings.require_auth = True
    settings.webhook_async = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as client:
            r = client.get("/v1/policy/decision/agent-1", headers=_H)
            assert r.status_code == 200

        db = sf()
        jobs = list(db.scalars(select(WebhookJob).order_by(WebhookJob.id)).all())
        deliveries = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
        db.close()

        assert len(jobs) == 1
        assert jobs[0].state == "pending"
        # Async enqueue should not deliver inline.
        assert deliveries == []
    finally:
        settings.require_auth = original_auth
        settings.webhook_async = original_async
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


def test_worker_claim_and_process_job_success() -> None:
    engine, sf = _make_db_and_seed()
    # Create a pending job via the real API path (enqueue).
    original_async = settings.webhook_async
    settings.webhook_async = True
    try:
        def override_get_db():
            s = sf()
            try:
                yield s
            finally:
                s.close()

        original_auth = settings.require_auth
        settings.require_auth = True
        app.dependency_overrides[get_db] = override_get_db
        reset_rate_limits()
        try:
            with TestClient(app) as client:
                r = client.get("/v1/policy/decision/agent-2", headers=_H)
                assert r.status_code == 200
        finally:
            settings.require_auth = original_auth
            app.dependency_overrides.clear()
            reset_rate_limits()
    finally:
        settings.webhook_async = original_async

    # Claim the job (SQLite claim) and process it.
    db = sf()
    try:
        job = claim_pending_job_sqlite(db)
        assert job is not None
        assert job.state == "in_progress"

        with patch("app.services.webhook.httpx.Client") as MockClientClass:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            MockClientClass.return_value = mock_client

            ok = process_webhook_job(db, job)
            assert ok is True

        db.refresh(job)
        assert job.state == "completed"
        assert job.attempts == 1

        deliveries = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
        assert len(deliveries) == 1
        assert deliveries[0].attempt == 1
        assert deliveries[0].status_code == 200
        assert deliveries[0].success is True

        # Idempotency: completed job should not be claimable again.
        job2 = claim_pending_job_sqlite(db)
        assert job2 is None
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_worker_job_reschedules_on_http_failure() -> None:
    engine, sf = _make_db_and_seed()

    db = sf()
    try:
        # Seed a pending job directly.
        job = WebhookJob(
            webhook_id=1,
            tenant_id=1,
            agent_id="agent-3",
            payload='{"agent_id":"agent-3","decision":"review","score":50.0,"thresholds":{"allow":80,"review":60,"block":40},"explanation":"x"}',
            state="pending",
            attempts=0,
            max_attempts=5,
        )
        db.add(job)
        db.commit()

        claimed = claim_pending_job_sqlite(db)
        assert claimed is not None
        assert claimed.state == "in_progress"

        with patch("app.services.webhook.httpx.Client") as MockClientClass:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_client.post.return_value = mock_response
            MockClientClass.return_value = mock_client

            ok = process_webhook_job(db, claimed)
            assert ok is False

        db.refresh(claimed)
        assert claimed.state in ("failed", "dead")
        assert claimed.attempts == 1

        deliveries = list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())
        assert len(deliveries) == 1
        assert deliveries[0].status_code == 503
        assert deliveries[0].success is False
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
