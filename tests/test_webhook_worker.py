"""Tests for the async webhook worker: enqueue, process, retry, idempotency.

Covers:
- enqueue_webhook creates a pending job row
- enqueue_webhook returns None when no webhook configured
- process_webhook_job succeeds on 2xx → state=completed + delivery logged
- process_webhook_job fails on non-2xx → state=failed with backoff
- process_webhook_job marks dead after max_attempts exhausted
- process_webhook_job handles network errors
- process_webhook_job marks dead when webhook disabled
- requeue_failed_jobs moves failed→pending when scheduled_at passes
- recover_stale_jobs resets stuck in_progress jobs
- claim_pending_job_sqlite picks and transitions job
- Policy endpoint enqueues (not sends) when WEBHOOK_ASYNC=true
- Admin jobs endpoint returns job list, tenant-isolated
- Worker run_once processes pending jobs end-to-end
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    WebhookJob,
)
from app.rate_limit import reset_rate_limits
from app.services.policy import PolicyDecision, Thresholds
from app.services.webhook import (
    claim_pending_job_sqlite,
    enqueue_webhook,
    process_webhook_job,
    recover_stale_jobs,
    requeue_failed_jobs,
)


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


def _make_db_and_seed(*, tenant2: bool = False, webhook_enabled: bool = True):
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
    if webhook_enabled:
        db.add(PolicyWebhook(tenant_id=1, url="https://hooks.example.com/policy", secret="test-webhook-secret", enabled=True))
    if tenant2:
        db.add(PolicyConfig(tenant_id=2, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
        db.add(PolicyWebhook(tenant_id=2, url="https://hooks.example.com/other", secret="other-secret", enabled=True))
    db.commit()
    db.close()
    return engine, sf


def _make_decision() -> PolicyDecision:
    return PolicyDecision(
        decision="allow",
        score=85.0,
        thresholds=Thresholds(allow=80.0, review=60.0, block=40.0),
        explanation="Score 85.0 >= allow threshold 80.0",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_db():
    """Returns (session_factory) with seeded data."""
    engine, sf = _make_db_and_seed()
    yield sf
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def worker_db_no_webhook():
    engine, sf = _make_db_and_seed(webhook_enabled=False)
    yield sf
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def async_client():
    """TestClient with WEBHOOK_ASYNC=true."""
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
        with TestClient(app) as c:
            yield c, sf
    finally:
        settings.require_auth = original_auth
        settings.webhook_async = original_async
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


@pytest.fixture
def multi_tenant_async_client():
    """TestClient with WEBHOOK_ASYNC=true and two tenants."""
    engine, sf = _make_db_and_seed(tenant2=True)

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
        with TestClient(app) as c:
            yield c, sf
    finally:
        settings.require_auth = original_auth
        settings.webhook_async = original_async
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


# ==========================================================================
# enqueue_webhook
# ==========================================================================

def test_enqueue_creates_pending_job(worker_db) -> None:
    """enqueue_webhook creates a job row in pending state."""
    db = worker_db()
    decision = _make_decision()
    job = enqueue_webhook(db, tenant_id=1, agent_id="agent-enq", decision=decision)
    db.close()

    assert job is not None
    assert job.state == "pending"
    assert job.attempts == 0
    assert job.agent_id == "agent-enq"
    assert job.tenant_id == 1
    assert job.webhook_id is not None

    # Payload should be valid JSON with expected fields
    payload = json.loads(job.payload)
    assert payload["agent_id"] == "agent-enq"
    assert payload["decision"] == "allow"
    assert payload["score"] == 85.0


def test_enqueue_returns_none_without_webhook(worker_db_no_webhook) -> None:
    """enqueue_webhook returns None when no webhook is configured."""
    db = worker_db_no_webhook()
    decision = _make_decision()
    job = enqueue_webhook(db, tenant_id=1, agent_id="agent-no-wh", decision=decision)
    db.close()

    assert job is None


# ==========================================================================
# claim_pending_job_sqlite
# ==========================================================================

def test_claim_pending_job_transitions_state(worker_db) -> None:
    """claim_pending_job_sqlite transitions pending → in_progress."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-claim", decision=decision)

    job = claim_pending_job_sqlite(db)
    db.close()

    assert job is not None
    assert job.state == "in_progress"
    assert job.started_at is not None


def test_claim_returns_none_when_empty(worker_db) -> None:
    """claim_pending_job_sqlite returns None with no pending jobs."""
    db = worker_db()
    job = claim_pending_job_sqlite(db)
    db.close()
    assert job is None


def test_claim_skips_future_scheduled(worker_db) -> None:
    """Jobs with scheduled_at in the future are not claimed."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-future", decision=decision)

    # Push scheduled_at into the future
    job_row = db.scalars(select(WebhookJob)).first()
    job_row.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()

    claimed = claim_pending_job_sqlite(db)
    db.close()
    assert claimed is None


# ==========================================================================
# process_webhook_job — success
# ==========================================================================

def test_process_job_success(worker_db) -> None:
    """Successful delivery sets state=completed and logs a delivery."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-ok", decision=decision)
    job = claim_pending_job_sqlite(db)

    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        result = process_webhook_job(db, job)

    assert result is True
    assert job.state == "completed"
    assert job.attempts == 1
    assert job.completed_at is not None

    # Delivery should be logged
    deliveries = list(db.scalars(select(WebhookDelivery)).all())
    assert len(deliveries) == 1
    assert deliveries[0].success is True
    assert deliveries[0].status_code == 200
    db.close()


# ==========================================================================
# process_webhook_job — failure with retry
# ==========================================================================

def test_process_job_failure_reschedules(worker_db) -> None:
    """Non-2xx response sets state=failed with backoff schedule."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-fail", decision=decision)
    job = claim_pending_job_sqlite(db)

    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        result = process_webhook_job(db, job)

    assert result is False
    assert job.state == "failed"
    assert job.attempts == 1
    assert job.last_error == "HTTP 503"
    # scheduled_at should be in the future (SQLite may return naive datetimes)
    now = datetime.now(timezone.utc)
    sched = job.scheduled_at if job.scheduled_at.tzinfo else job.scheduled_at.replace(tzinfo=timezone.utc)
    assert sched > now  # rescheduled in future

    # Delivery should be logged
    deliveries = list(db.scalars(select(WebhookDelivery)).all())
    assert len(deliveries) == 1
    assert deliveries[0].success is False
    assert deliveries[0].status_code == 503
    db.close()


# ==========================================================================
# process_webhook_job — network error
# ==========================================================================

def test_process_job_network_error(worker_db) -> None:
    """Network error sets state=failed with error message."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-net-err", decision=decision)
    job = claim_pending_job_sqlite(db)

    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        MockClientClass.return_value = mock_client

        result = process_webhook_job(db, job)

    assert result is False
    assert job.state == "failed"
    assert "Connection refused" in job.last_error

    deliveries = list(db.scalars(select(WebhookDelivery)).all())
    assert len(deliveries) == 1
    assert deliveries[0].success is False
    assert "Connection refused" in deliveries[0].error
    db.close()


# ==========================================================================
# process_webhook_job — dead after max attempts
# ==========================================================================

def test_process_job_dead_after_max_attempts(worker_db) -> None:
    """Job transitions to dead when max_attempts exhausted."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-dead", decision=decision)
    job = claim_pending_job_sqlite(db)

    # Simulate previous attempts
    job.attempts = 4  # max_attempts is 5, this will be attempt #5
    db.commit()

    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        result = process_webhook_job(db, job)

    assert result is False
    assert job.state == "dead"
    assert job.attempts == 5
    assert job.completed_at is not None
    db.close()


# ==========================================================================
# process_webhook_job — webhook disabled
# ==========================================================================

def test_process_job_dead_when_webhook_disabled(worker_db) -> None:
    """Job is marked dead if webhook was disabled after enqueue."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-disabled", decision=decision)
    job = claim_pending_job_sqlite(db)

    # Disable the webhook
    webhook = db.scalars(select(PolicyWebhook)).first()
    webhook.enabled = False
    db.commit()

    result = process_webhook_job(db, job)
    assert result is False
    assert job.state == "dead"
    assert "disabled" in job.last_error.lower()
    db.close()


# ==========================================================================
# requeue_failed_jobs
# ==========================================================================

def test_requeue_failed_jobs(worker_db) -> None:
    """Failed jobs with passed scheduled_at are moved back to pending."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-requeue", decision=decision)

    # Manually set state to failed with scheduled_at in the past
    job = db.scalars(select(WebhookJob)).first()
    job.state = "failed"
    job.attempts = 1
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.commit()

    count = requeue_failed_jobs(db)
    assert count == 1

    db.refresh(job)
    assert job.state == "pending"
    db.close()


def test_requeue_skips_future_scheduled(worker_db) -> None:
    """Failed jobs with future scheduled_at are not requeued."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-skip", decision=decision)

    job = db.scalars(select(WebhookJob)).first()
    job.state = "failed"
    job.attempts = 1
    job.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()

    count = requeue_failed_jobs(db)
    assert count == 0

    db.refresh(job)
    assert job.state == "failed"
    db.close()


def test_requeue_skips_max_attempts(worker_db) -> None:
    """Failed jobs at max_attempts are not requeued."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-maxed", decision=decision)

    job = db.scalars(select(WebhookJob)).first()
    job.state = "failed"
    job.attempts = 5  # matches max_attempts
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.commit()

    count = requeue_failed_jobs(db)
    assert count == 0
    db.close()


# ==========================================================================
# recover_stale_jobs
# ==========================================================================

def test_recover_stale_jobs(worker_db) -> None:
    """Jobs stuck in in_progress are recovered to failed."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-stale", decision=decision)

    job = db.scalars(select(WebhookJob)).first()
    job.state = "in_progress"
    job.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.commit()

    count = recover_stale_jobs(db)
    assert count == 1

    db.refresh(job)
    assert job.state == "failed"
    assert "timeout" in job.last_error.lower()
    db.close()


def test_recover_skips_recent_in_progress(worker_db) -> None:
    """Recently started in_progress jobs are not recovered."""
    db = worker_db()
    decision = _make_decision()
    enqueue_webhook(db, tenant_id=1, agent_id="agent-recent", decision=decision)

    job = db.scalars(select(WebhookJob)).first()
    job.state = "in_progress"
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.commit()

    count = recover_stale_jobs(db)
    assert count == 0

    db.refresh(job)
    assert job.state == "in_progress"
    db.close()


# ==========================================================================
# Policy endpoint with WEBHOOK_ASYNC=true
# ==========================================================================

def test_async_policy_enqueues_job(async_client) -> None:
    """Policy decision endpoint enqueues a job when WEBHOOK_ASYNC=true."""
    client, sf = async_client

    r = client.get("/v1/policy/decision/agent-async-test", headers=_H1)
    assert r.status_code == 200

    # Verify a job was created (not a delivery — no webhook call was made)
    db = sf()
    jobs = list(db.scalars(select(WebhookJob)).all())
    deliveries = list(db.scalars(select(WebhookDelivery)).all())
    db.close()

    assert len(jobs) == 1
    assert jobs[0].state == "pending"
    assert jobs[0].agent_id == "agent-async-test"
    # No deliveries since webhook was only enqueued, not sent
    assert len(deliveries) == 0


# ==========================================================================
# Admin jobs endpoint
# ==========================================================================

def test_admin_jobs_endpoint(async_client) -> None:
    """GET /v1/admin/policy/webhooks/{id}/jobs returns job list."""
    client, sf = async_client

    # Trigger a policy decision to create a job
    client.get("/v1/policy/decision/agent-jobs-test", headers=_H1)

    # Get webhook ID
    wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
    wh_id = wh_resp.json()["webhooks"][0]["id"]

    r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/jobs", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    j = data["jobs"][0]
    assert j["state"] == "pending"
    assert j["agent_id"] == "agent-jobs-test"
    assert "webhook_id" in j
    assert "attempts" in j
    assert "scheduled_at" in j


def test_admin_jobs_filter_by_state(async_client) -> None:
    """GET /v1/admin/policy/webhooks/{id}/jobs?state=pending filters."""
    client, sf = async_client

    client.get("/v1/policy/decision/agent-filter-test", headers=_H1)

    wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
    wh_id = wh_resp.json()["webhooks"][0]["id"]

    # Filter by pending — should find the job
    r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/jobs?state=pending", headers=_H1)
    assert r.status_code == 200
    assert r.json()["count"] >= 1

    # Filter by completed — should find nothing
    r = client.get(f"/v1/admin/policy/webhooks/{wh_id}/jobs?state=completed", headers=_H1)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_admin_jobs_tenant_isolation(multi_tenant_async_client) -> None:
    """Tenant B cannot see tenant A's webhook jobs."""
    client, sf = multi_tenant_async_client

    # Trigger job for tenant A
    client.get("/v1/policy/decision/agent-iso", headers=_H1)

    # Get tenant A's webhook ID
    wh_resp = client.get("/v1/admin/policy/webhooks", headers=_H1)
    wh_id_a = wh_resp.json()["webhooks"][0]["id"]

    # Tenant A can see jobs
    r_a = client.get(f"/v1/admin/policy/webhooks/{wh_id_a}/jobs", headers=_H1)
    assert r_a.status_code == 200
    assert r_a.json()["count"] >= 1

    # Tenant B gets 404
    r_b = client.get(f"/v1/admin/policy/webhooks/{wh_id_a}/jobs", headers=_H2)
    assert r_b.status_code == 404


# ==========================================================================
# Worker run_once end-to-end
# ==========================================================================

def test_worker_run_once_processes_jobs(async_client) -> None:
    """worker.run_once picks up and processes pending jobs."""
    client, sf = async_client

    # Create a pending job via policy endpoint
    client.get("/v1/policy/decision/agent-worker-e2e", headers=_H1)

    # Verify job exists
    db = sf()
    jobs = list(db.scalars(select(WebhookJob)).all())
    assert len(jobs) == 1
    assert jobs[0].state == "pending"
    db.close()

    # Patch the worker's session factory to use our test DB and mock HTTP
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.worker.get_session_factory", return_value=sf):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        from app.worker import run_once
        count = run_once(use_sqlite_claim=True)

    assert count == 1

    # Verify job is now completed
    db = sf()
    job = db.scalars(select(WebhookJob)).first()
    assert job.state == "completed"
    assert job.attempts == 1

    # Verify delivery was logged
    deliveries = list(db.scalars(select(WebhookDelivery)).all())
    assert len(deliveries) == 1
    assert deliveries[0].success is True
    db.close()


def test_worker_run_once_handles_failure_and_requeue(async_client) -> None:
    """Worker processes a failing job, then requeues it on next pass."""
    client, sf = async_client

    # Create a pending job
    client.get("/v1/policy/decision/agent-worker-fail", headers=_H1)

    # First pass: job fails
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.worker.get_session_factory", return_value=sf):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        from app.worker import run_once
        count = run_once(use_sqlite_claim=True)

    assert count == 1

    db = sf()
    job = db.scalars(select(WebhookJob)).first()
    assert job.state == "failed"
    assert job.attempts == 1

    # Manually set scheduled_at to past so requeue picks it up
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    db.close()

    # Second pass: requeue + process (now succeeds)
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.worker.get_session_factory", return_value=sf):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        count = run_once(use_sqlite_claim=True)

    assert count == 1

    db = sf()
    job = db.scalars(select(WebhookJob)).first()
    assert job.state == "completed"
    assert job.attempts == 2
    db.close()
