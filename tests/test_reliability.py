"""Reliability, concurrency, and failure-injection tests for the webhook subsystem.

Tests cover:
- Concurrent job claiming does not double-complete jobs
- Transient webhook failures eventually succeed after retry
- Dead jobs after max attempts remain dead until replay
- Replay transitions dead/failed -> pending and then completes
- Stale in_progress jobs are recovered and processed
- Failure injection: timeouts, network errors, HTTP 500
- Recover stale jobs increments attempts (no infinite retry)
- Replay of dead jobs extends max_attempts to allow re-processing
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin_store import replay_webhook_job
from app.auth import hash_api_key
from app.models import (
    ApiKey,
    Base,
    PolicyConfig,
    PolicyWebhook,
    Tenant,
    WebhookDelivery,
    WebhookJob,
)
from app.services.webhook import (
    DEFAULT_MAX_JOB_ATTEMPTS,
    JOB_BACKOFF_BASE_SECONDS,
    STALE_JOB_TIMEOUT_SECONDS,
    claim_pending_job_sqlite,
    compute_signature,
    enqueue_webhook,
    process_webhook_job,
    recover_stale_jobs,
    requeue_failed_jobs,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_engine_and_factory():
    """Create an in-memory SQLite engine and session factory."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    return engine, sf


def _seed_basics(sf) -> dict:
    """Seed a tenant, API key, policy config, and enabled webhook. Returns IDs."""
    db = sf()
    tenant = Tenant(id=1, name="Test Tenant", slug="test", is_active=True)
    db.add(tenant)
    db.commit()

    webhook = PolicyWebhook(
        tenant_id=1,
        url="https://httpbin.org/post",
        secret="test-secret-key",
        enabled=True,
    )
    db.add(webhook)
    db.add(PolicyConfig(tenant_id=1, allow_threshold=80, review_threshold=60, block_threshold=40))
    db.add(ApiKey(tenant_id=1, key_prefix="atb_test1234", key_hash=hash_api_key("atb_test1234aaaa"), name="test"))
    db.commit()
    db.refresh(webhook)
    result = {"tenant_id": 1, "webhook_id": webhook.id}
    db.close()
    return result


def _create_pending_job(sf, *, webhook_id: int, tenant_id: int = 1, agent_id: str = "agent-1") -> int:
    """Insert a pending webhook job and return its ID."""
    db = sf()
    job = WebhookJob(
        webhook_id=webhook_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        payload=json.dumps({"agent_id": agent_id, "decision": "allow", "score": 85}),
        state="pending",
        attempts=0,
        max_attempts=DEFAULT_MAX_JOB_ATTEMPTS,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()
    return jid


@pytest.fixture
def db_env():
    """Provide engine, session factory, and seeded IDs."""
    engine, sf = _make_engine_and_factory()
    ids = _seed_basics(sf)
    yield sf, ids
    Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# 1. Concurrent job claiming — no double-complete
# ---------------------------------------------------------------------------

class TestConcurrentClaiming:
    def test_claim_returns_different_jobs(self, db_env):
        """Two sequential claims should return two different jobs."""
        sf, ids = db_env
        j1 = _create_pending_job(sf, webhook_id=ids["webhook_id"])
        j2 = _create_pending_job(sf, webhook_id=ids["webhook_id"], agent_id="agent-2")

        db = sf()
        claimed1 = claim_pending_job_sqlite(db)
        assert claimed1 is not None
        db.close()

        db = sf()
        claimed2 = claim_pending_job_sqlite(db)
        assert claimed2 is not None
        db.close()

        assert claimed1.id != claimed2.id

    def test_sequential_claim_exhaustion(self, db_env):
        """After all jobs are claimed, no more claims should succeed."""
        sf, ids = db_env
        # Create exactly 3 jobs
        for i in range(3):
            _create_pending_job(sf, webhook_id=ids["webhook_id"], agent_id=f"agent-exhaust-{i}")

        claimed_ids = []
        for _ in range(5):  # Try to claim more than available
            db = sf()
            job = claim_pending_job_sqlite(db)
            if job is not None:
                claimed_ids.append(job.id)
            db.close()

        # Exactly 3 should have been claimed
        assert len(claimed_ids) == 3
        # All unique
        assert len(set(claimed_ids)) == 3

    def test_concurrent_process_does_not_double_complete(self, db_env):
        """Processing the same job from two sessions — only one should succeed."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        # Claim the job
        db = sf()
        job = claim_pending_job_sqlite(db)
        assert job is not None
        assert job.id == jid
        db.close()

        # Mock successful HTTP response
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        # Process from first session — should succeed
        db1 = sf()
        job1 = db1.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result1 = process_webhook_job(db1, job1)
        assert result1 is True
        db1.close()

        # Second session tries to process — job already completed
        db2 = sf()
        job2 = db2.get(WebhookJob, jid)
        assert job2.state == "completed"
        result2 = process_webhook_job(db2, job2)
        assert result2 is False  # Refuses to process non-in_progress job
        db2.close()


# ---------------------------------------------------------------------------
# 2. Failure injection — timeout, network error, HTTP 500
# ---------------------------------------------------------------------------

class TestFailureInjection:
    def test_http_500_fails_delivery(self, db_env):
        """HTTP 500 response should mark delivery as failed, not completed."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        # Claim
        db = sf()
        job = claim_pending_job_sqlite(db)
        db.close()

        # Mock 500 response
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state in ("failed", "dead")
        assert "HTTP 500" in job.last_error
        assert job.attempts == 1
        db.close()

    def test_connection_timeout_fails_delivery(self, db_env):
        """Connection timeout should fail gracefully."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        db = sf()
        job = claim_pending_job_sqlite(db)
        db.close()

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = httpx.ConnectTimeout("Connection timed out")

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state in ("failed", "dead")
        assert "timed out" in job.last_error.lower()
        db.close()

    def test_network_error_fails_delivery(self, db_env):
        """Network error (e.g. DNS failure) should fail gracefully."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        db = sf()
        job = claim_pending_job_sqlite(db)
        db.close()

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = httpx.ConnectError("DNS resolution failed")

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state in ("failed", "dead")
        assert "DNS" in job.last_error or "failed" in job.last_error.lower()
        db.close()

    def test_unexpected_exception_handled(self, db_env):
        """Non-HTTP exceptions (e.g. RuntimeError) should not crash the worker."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        db = sf()
        job = claim_pending_job_sqlite(db)
        db.close()

        # Simulate httpx.Client() constructor raising an unexpected error
        with patch("app.services.webhook.httpx.Client", side_effect=RuntimeError("Out of memory")):
            db = sf()
            job = db.get(WebhookJob, jid)
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state in ("failed", "dead")
        assert "Out of memory" in job.last_error
        db.close()

    def test_disabled_webhook_marks_job_dead(self, db_env):
        """Job for disabled webhook should be marked dead immediately."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        # Disable the webhook
        db = sf()
        wh = db.get(PolicyWebhook, ids["webhook_id"])
        wh.enabled = False
        db.commit()

        job = claim_pending_job_sqlite(db)
        db.close()

        db = sf()
        job = db.get(WebhookJob, jid)
        result = process_webhook_job(db, job)

        assert result is False
        assert job.state == "dead"
        assert "disabled" in job.last_error.lower() or "not found" in job.last_error.lower()
        db.close()


# ---------------------------------------------------------------------------
# 3. Max attempts exhausted → dead
# ---------------------------------------------------------------------------

class TestMaxAttemptsExhausted:
    def test_dead_after_max_attempts(self, db_env):
        """Job should transition to dead after max_attempts failures."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-max",
            payload=json.dumps({"agent_id": "agent-max", "decision": "block"}),
            state="in_progress",
            attempts=4,  # This will be attempt 5 (== max_attempts)
            max_attempts=5,
        )
        job.started_at = datetime.now(timezone.utc)
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id
        db.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state == "dead"
        assert job.attempts == 5
        assert job.completed_at is not None
        db.close()

    def test_failed_not_dead_before_max(self, db_env):
        """Job with retries remaining should go to failed, not dead."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-retry",
            payload=json.dumps({"agent_id": "agent-retry", "decision": "allow"}),
            state="in_progress",
            attempts=1,  # attempt 2 of 5
            max_attempts=5,
        )
        job.started_at = datetime.now(timezone.utc)
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id
        db.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 503

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is False
        assert job.state == "failed"
        assert job.attempts == 2
        assert job.completed_at is None
        # Should have a scheduled_at set (backoff)
        assert job.scheduled_at is not None
        db.close()


# ---------------------------------------------------------------------------
# 4. Replay transitions dead/failed → pending → completes
# ---------------------------------------------------------------------------

class TestReplayLifecycle:
    def test_replay_dead_job_to_completion(self, db_env):
        """A dead job, when replayed, should go pending → in_progress → completed."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-replay",
            payload=json.dumps({"agent_id": "agent-replay", "decision": "review"}),
            state="dead",
            attempts=5,
            max_attempts=5,
            last_error="HTTP 500 five times",
            completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id

        # Replay
        job = replay_webhook_job(db, job)
        assert job.state == "pending"
        assert job.max_attempts == 6  # Extended by 1
        db.close()

        # Claim
        db = sf()
        claimed = claim_pending_job_sqlite(db)
        assert claimed is not None
        assert claimed.id == jid
        assert claimed.state == "in_progress"
        db.close()

        # Process with success
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is True
        assert job.state == "completed"
        assert job.attempts == 6
        db.close()

    def test_replay_failed_job(self, db_env):
        """A failed job can be replayed without needing max_attempts extension."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-failed",
            payload=json.dumps({"agent_id": "agent-failed", "decision": "block"}),
            state="failed",
            attempts=2,
            max_attempts=5,
            last_error="HTTP 503",
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Replay: attempts=2 < max=5, so max_attempts stays at 5
        job = replay_webhook_job(db, job)
        assert job.state == "pending"
        assert job.max_attempts == 5  # Not extended — still has retries
        db.close()

    def test_replay_extends_max_attempts_for_dead_job(self, db_env):
        """Dead job at max_attempts should get max_attempts bumped on replay."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-dead-replay",
            payload="{}",
            state="dead",
            attempts=5,
            max_attempts=5,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        replayed = replay_webhook_job(db, job)
        assert replayed.max_attempts == 6
        assert replayed.state == "pending"
        db.close()


# ---------------------------------------------------------------------------
# 5. Stale job recovery
# ---------------------------------------------------------------------------

class TestStaleJobRecovery:
    def test_stale_job_recovered_to_failed(self, db_env):
        """Jobs stuck in in_progress longer than timeout should be recovered."""
        sf, ids = db_env

        stale_time = datetime.now(timezone.utc) - timedelta(seconds=STALE_JOB_TIMEOUT_SECONDS + 60)

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-stale",
            payload="{}",
            state="in_progress",
            attempts=1,
            max_attempts=5,
            started_at=stale_time,
        )
        db.add(job)
        db.commit()
        jid = job.id
        db.close()

        db = sf()
        recovered = recover_stale_jobs(db)
        assert recovered == 1

        job = db.get(WebhookJob, jid)
        assert job.state == "failed"
        assert "timeout" in job.last_error.lower() or "stale" in job.last_error.lower()
        assert job.attempts == 2  # Incremented from 1 to 2
        db.close()

    def test_stale_recovery_increments_attempts(self, db_env):
        """Each stale recovery should count as a failed attempt."""
        sf, ids = db_env

        stale_time = datetime.now(timezone.utc) - timedelta(seconds=STALE_JOB_TIMEOUT_SECONDS + 60)

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-stale-count",
            payload="{}",
            state="in_progress",
            attempts=3,
            max_attempts=5,
            started_at=stale_time,
        )
        db.add(job)
        db.commit()
        jid = job.id
        db.close()

        db = sf()
        recover_stale_jobs(db)
        job = db.get(WebhookJob, jid)
        assert job.attempts == 4  # Was 3, now 4
        assert job.state == "failed"
        db.close()

    def test_stale_at_max_attempts_requeues_to_failed_then_stays(self, db_env):
        """Stale job at attempts=4/max=5 recovers to failed at 5, then requeue makes it dead-eligible."""
        sf, ids = db_env

        stale_time = datetime.now(timezone.utc) - timedelta(seconds=STALE_JOB_TIMEOUT_SECONDS + 60)

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-stale-max",
            payload="{}",
            state="in_progress",
            attempts=4,
            max_attempts=5,
            started_at=stale_time,
        )
        db.add(job)
        db.commit()
        jid = job.id
        db.close()

        db = sf()
        recover_stale_jobs(db)
        job = db.get(WebhookJob, jid)
        # attempts was 4, now 5 = max_attempts
        assert job.attempts == 5
        assert job.state == "failed"

        # requeue_failed_jobs should NOT move this back to pending
        # because attempts (5) >= max_attempts (5)
        requeued = requeue_failed_jobs(db)
        assert requeued == 0
        db.close()

    def test_fresh_in_progress_not_recovered(self, db_env):
        """Jobs that started recently should not be recovered."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-fresh",
            payload="{}",
            state="in_progress",
            attempts=0,
            max_attempts=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        db.close()

        db = sf()
        recovered = recover_stale_jobs(db)
        assert recovered == 0
        db.close()


# ---------------------------------------------------------------------------
# 6. Requeue failed jobs
# ---------------------------------------------------------------------------

class TestRequeueFailedJobs:
    def test_requeue_moves_failed_to_pending(self, db_env):
        """Failed jobs with retries remaining and past scheduled_at should be requeued."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-requeue",
            payload="{}",
            state="failed",
            attempts=2,
            max_attempts=5,
            scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        db.add(job)
        db.commit()
        jid = job.id
        db.close()

        db = sf()
        requeued = requeue_failed_jobs(db)
        assert requeued == 1

        job = db.get(WebhookJob, jid)
        assert job.state == "pending"
        db.close()

    def test_requeue_skips_future_scheduled(self, db_env):
        """Failed jobs with future scheduled_at should not be requeued yet."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-future",
            payload="{}",
            state="failed",
            attempts=1,
            max_attempts=5,
            scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        db.add(job)
        db.commit()
        db.close()

        db = sf()
        requeued = requeue_failed_jobs(db)
        assert requeued == 0
        db.close()

    def test_requeue_skips_exhausted_jobs(self, db_env):
        """Failed jobs that have exhausted retries should not be requeued."""
        sf, ids = db_env

        db = sf()
        job = WebhookJob(
            webhook_id=ids["webhook_id"],
            tenant_id=1,
            agent_id="agent-exhausted",
            payload="{}",
            state="failed",
            attempts=5,
            max_attempts=5,
            scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        db.add(job)
        db.commit()
        db.close()

        db = sf()
        requeued = requeue_failed_jobs(db)
        assert requeued == 0
        db.close()


# ---------------------------------------------------------------------------
# 7. Delivery recording
# ---------------------------------------------------------------------------

class TestDeliveryRecording:
    def test_successful_delivery_logged(self, db_env):
        """Successful webhook delivery should create a delivery record."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        db = sf()
        job = claim_pending_job_sqlite(db)
        db.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            process_webhook_job(db, job)

        deliveries = list(db.scalars(
            select(WebhookDelivery).where(WebhookDelivery.webhook_id == ids["webhook_id"])
        ).all())
        assert len(deliveries) == 1
        assert deliveries[0].success is True
        assert deliveries[0].status_code == 200
        db.close()

    def test_failed_delivery_logged(self, db_env):
        """Failed webhook delivery should create a delivery record with error."""
        sf, ids = db_env
        jid = _create_pending_job(sf, webhook_id=ids["webhook_id"])

        db = sf()
        claim_pending_job_sqlite(db)
        db.close()

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            process_webhook_job(db, job)

        deliveries = list(db.scalars(
            select(WebhookDelivery).where(WebhookDelivery.webhook_id == ids["webhook_id"])
        ).all())
        assert len(deliveries) == 1
        assert deliveries[0].success is False
        assert "Connection refused" in deliveries[0].error
        db.close()


# ---------------------------------------------------------------------------
# 8. End-to-end: enqueue → claim → process
# ---------------------------------------------------------------------------

class TestEndToEndFlow:
    def test_enqueue_claim_process_success(self, db_env):
        """Full lifecycle: enqueue → claim → process with 200 → completed."""
        sf, ids = db_env

        from app.services.policy import PolicyDecision, Thresholds

        thresholds = Thresholds(allow=80, review=60, block=40)
        decision = PolicyDecision(
            decision="allow",
            score=85.0,
            thresholds=thresholds,
            explanation="High trust",
        )

        db = sf()
        job = enqueue_webhook(db, tenant_id=1, agent_id="agent-e2e", decision=decision)
        assert job is not None
        assert job.state == "pending"
        jid = job.id
        db.close()

        db = sf()
        claimed = claim_pending_job_sqlite(db)
        assert claimed.id == jid
        assert claimed.state == "in_progress"
        db.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        db = sf()
        job = db.get(WebhookJob, jid)
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            result = process_webhook_job(db, job)

        assert result is True
        assert job.state == "completed"

        # Verify the POST was called with correct HMAC signature
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
        assert "X-ATB-Signature" in headers
        payload_bytes = call_args.kwargs.get("content") or call_args[1].get("content")
        expected_sig = compute_signature(payload_bytes, "test-secret-key")
        assert headers["X-ATB-Signature"] == expected_sig
        db.close()

    def test_enqueue_returns_none_without_webhook(self, db_env):
        """Enqueue should return None when no enabled webhook exists."""
        sf, ids = db_env

        # Disable the webhook
        db = sf()
        wh = db.get(PolicyWebhook, ids["webhook_id"])
        wh.enabled = False
        db.commit()
        db.close()

        from app.services.policy import PolicyDecision, Thresholds

        thresholds = Thresholds(allow=80, review=60, block=40)
        decision = PolicyDecision(
            decision="block",
            score=30.0,
            thresholds=thresholds,
            explanation="Low trust",
        )

        db = sf()
        job = enqueue_webhook(db, tenant_id=1, agent_id="agent-no-wh", decision=decision)
        assert job is None
        db.close()


# ---------------------------------------------------------------------------
# 9. Stress test: many jobs claimed and processed
# ---------------------------------------------------------------------------

class TestStress:
    def test_batch_of_50_jobs(self, db_env):
        """50 jobs should all be claimed and processed without errors."""
        sf, ids = db_env

        # Create 50 pending jobs
        job_ids = []
        for i in range(50):
            jid = _create_pending_job(sf, webhook_id=ids["webhook_id"], agent_id=f"agent-{i}")
            job_ids.append(jid)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_resp

        completed = 0
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            while True:
                db = sf()
                job = claim_pending_job_sqlite(db)
                if job is None:
                    db.close()
                    break
                process_webhook_job(db, job)
                db.close()
                completed += 1

        assert completed == 50

        # Verify all jobs are completed
        db = sf()
        for jid in job_ids:
            job = db.get(WebhookJob, jid)
            assert job.state == "completed"
        db.close()

    def test_mixed_success_and_failure_batch(self, db_env):
        """Batch with alternating success/failure should handle all correctly."""
        sf, ids = db_env

        job_ids = []
        for i in range(20):
            jid = _create_pending_job(sf, webhook_id=ids["webhook_id"], agent_id=f"agent-mix-{i}")
            job_ids.append(jid)

        call_count = 0

        def alternating_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200 if call_count % 2 == 0 else 500
            return resp

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = alternating_post

        processed = 0
        with patch("app.services.webhook.httpx.Client", return_value=mock_client):
            while True:
                db = sf()
                job = claim_pending_job_sqlite(db)
                if job is None:
                    db.close()
                    break
                process_webhook_job(db, job)
                db.close()
                processed += 1

        assert processed == 20

        # Count final states
        db = sf()
        completed_count = 0
        failed_count = 0
        for jid in job_ids:
            job = db.get(WebhookJob, jid)
            if job.state == "completed":
                completed_count += 1
            elif job.state in ("failed", "dead"):
                failed_count += 1
        assert completed_count == 10  # Even calls succeed
        assert failed_count == 10     # Odd calls fail
        db.close()


# ---------------------------------------------------------------------------
# 10. HMAC signature verification
# ---------------------------------------------------------------------------

class TestHMACSignature:
    def test_compute_signature_deterministic(self):
        """Same payload + secret should always produce the same signature."""
        payload = b'{"agent_id":"test","decision":"allow"}'
        secret = "my-secret-key"
        sig1 = compute_signature(payload, secret)
        sig2 = compute_signature(payload, secret)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA-256 hex digest

    def test_different_secrets_different_signatures(self):
        """Different secrets should produce different signatures."""
        payload = b'{"agent_id":"test","decision":"allow"}'
        sig1 = compute_signature(payload, "secret-1")
        sig2 = compute_signature(payload, "secret-2")
        assert sig1 != sig2

    def test_different_payloads_different_signatures(self):
        """Different payloads should produce different signatures."""
        secret = "my-secret"
        sig1 = compute_signature(b'{"decision":"allow"}', secret)
        sig2 = compute_signature(b'{"decision":"block"}', secret)
        assert sig1 != sig2
