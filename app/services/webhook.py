"""Webhook notification service.

Supports two delivery modes:

1. **Synchronous** (``send_webhook``): Sends HMAC-signed POST requests inline
   with retries and exponential backoff.  Used by the policy endpoint when
   ``WEBHOOK_ASYNC=false`` (the default for local dev / tests).

2. **Async queue** (``enqueue_webhook`` + worker): Creates a ``WebhookJob``
   row in ``pending`` state.  A separate worker process picks jobs and
   delivers them via ``process_webhook_job``.  Enable with
   ``WEBHOOK_ASYNC=true`` in production.

Each delivery attempt is logged to ``webhook_deliveries`` for operator
visibility regardless of mode.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import PolicyWebhook, WebhookDelivery, WebhookJob
from app.services.policy import PolicyDecision

logger = logging.getLogger(__name__)

# Retry config (used by both sync and async paths)
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
REQUEST_TIMEOUT_SECONDS = 10.0

# Async job defaults
DEFAULT_MAX_JOB_ATTEMPTS = 5
JOB_BACKOFF_BASE_SECONDS = 10.0  # 10s, 20s, 40s, 80s, 160s
JOB_BACKOFF_MULTIPLIER = 2.0
# Jobs stuck in "in_progress" for longer than this are considered stale
STALE_JOB_TIMEOUT_SECONDS = 300  # 5 minutes


def compute_signature(payload_bytes: bytes, secret: str) -> str:
    """HMAC-SHA256 signature of the payload using the webhook secret."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def _build_payload(agent_id: str, decision: PolicyDecision) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "decision": decision.decision,
        "score": decision.score,
        "thresholds": asdict(decision.thresholds),
        "explanation": decision.explanation,
    }


def _record_delivery(
    db: Session,
    webhook_id: int,
    tenant_id: int,
    attempt: int,
    *,
    status_code: int | None = None,
    error: str | None = None,
    success: bool = False,
) -> None:
    """Persist a single delivery attempt to the database."""
    row = WebhookDelivery(
        webhook_id=webhook_id,
        tenant_id=tenant_id,
        attempt=attempt,
        status_code=status_code,
        error=error,
        success=success,
    )
    db.add(row)
    db.commit()


# ---------------------------------------------------------------------------
# Async queue: enqueue + process
# ---------------------------------------------------------------------------

def enqueue_webhook(
    db: Session,
    tenant_id: int,
    agent_id: str,
    decision: PolicyDecision,
) -> WebhookJob | None:
    """Create a pending webhook job for async delivery.

    Returns the job row, or None if no enabled webhook exists for the tenant.
    """
    webhook = db.scalars(
        select(PolicyWebhook).where(
            PolicyWebhook.tenant_id == tenant_id,
            PolicyWebhook.enabled.is_(True),
        )
    ).first()

    if webhook is None:
        return None

    payload = _build_payload(agent_id, decision)
    payload_json = json.dumps(payload, sort_keys=True)

    job = WebhookJob(
        webhook_id=webhook.id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        payload=payload_json,
        state="pending",
        attempts=0,
        max_attempts=DEFAULT_MAX_JOB_ATTEMPTS,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Enqueued webhook job %d for tenant %d agent %s", job.id, tenant_id, agent_id)
    return job


def claim_pending_job(db: Session, *, batch_size: int = 1) -> WebhookJob | None:
    """Atomically claim the next pending job that is ready (scheduled_at <= now).

    Sets state to ``in_progress`` and records ``started_at``.
    Returns None if no jobs are available.
    """
    now = datetime.now(timezone.utc)

    # Find the oldest pending job whose scheduled_at has passed
    stmt = (
        select(WebhookJob)
        .where(
            WebhookJob.state == "pending",
            WebhookJob.scheduled_at <= now,
        )
        .order_by(WebhookJob.scheduled_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    job = db.scalars(stmt).first()
    if job is None:
        return None

    job.state = "in_progress"
    job.started_at = now
    db.commit()
    db.refresh(job)
    return job


def claim_pending_job_sqlite(db: Session) -> WebhookJob | None:
    """SQLite-compatible version of claim_pending_job (no FOR UPDATE SKIP LOCKED).

    Used in tests where SQLite is the backend.
    """
    now = datetime.now(timezone.utc)

    stmt = (
        select(WebhookJob)
        .where(
            WebhookJob.state == "pending",
            WebhookJob.scheduled_at <= now,
        )
        .order_by(WebhookJob.scheduled_at.asc())
        .limit(1)
    )

    job = db.scalars(stmt).first()
    if job is None:
        return None

    job.state = "in_progress"
    job.started_at = now
    db.commit()
    db.refresh(job)
    return job


def process_webhook_job(db: Session, job: WebhookJob) -> bool:
    """Deliver the webhook for a claimed job.

    - On success: state -> ``completed``, delivery logged with success=True
    - On failure: state -> ``failed`` (re-queued with backoff) or ``dead``
      (max attempts exhausted), delivery logged with success=False

    Returns True if delivery succeeded.
    """
    if job.state != "in_progress":
        logger.warning("Refusing to process job %d in state=%s (expected in_progress)", job.id, job.state)
        return False

    webhook = db.get(PolicyWebhook, job.webhook_id)
    if webhook is None or not webhook.enabled:
        logger.warning("Webhook %d not found or disabled for job %d", job.webhook_id, job.id)
        job.state = "dead"
        job.last_error = "Webhook not found or disabled"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        return False

    payload_bytes = job.payload.encode()
    signature = compute_signature(payload_bytes, webhook.secret)

    headers = {
        "Content-Type": "application/json",
        "X-ATB-Signature": signature,
    }

    attempt_num = job.attempts + 1
    now = datetime.now(timezone.utc)

    try:
        client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            resp = client.post(webhook.url, content=payload_bytes, headers=headers)
        finally:
            client.close()

        if 200 <= resp.status_code < 300:
            logger.info("Job %d delivered (attempt %d): %s", job.id, attempt_num, webhook.url)
            _record_delivery(
                db, webhook.id, job.tenant_id, attempt_num,
                status_code=resp.status_code, success=True,
            )
            job.state = "completed"
            job.attempts = attempt_num
            job.completed_at = now
            job.last_error = None
            db.commit()
            return True

        # Non-2xx response
        error_msg = f"HTTP {resp.status_code}"
        logger.warning("Job %d attempt %d got status %d", job.id, attempt_num, resp.status_code)
        _record_delivery(
            db, webhook.id, job.tenant_id, attempt_num,
            status_code=resp.status_code, success=False,
        )

    except httpx.HTTPError as exc:
        error_msg = str(exc)
        logger.warning("Job %d attempt %d failed: %s", job.id, attempt_num, exc)
        _record_delivery(
            db, webhook.id, job.tenant_id, attempt_num,
            error=error_msg, success=False,
        )

    # Failure path: reschedule or mark dead
    job.attempts = attempt_num
    job.last_error = error_msg

    if attempt_num >= job.max_attempts:
        job.state = "dead"
        job.completed_at = now
        logger.error("Job %d dead after %d attempts", job.id, attempt_num)
    else:
        backoff = JOB_BACKOFF_BASE_SECONDS * (JOB_BACKOFF_MULTIPLIER ** (attempt_num - 1))
        job.state = "failed"
        job.scheduled_at = now + timedelta(seconds=backoff)
        logger.info("Job %d rescheduled for retry in %.0fs", job.id, backoff)

    db.commit()
    return False


def recover_stale_jobs(db: Session) -> int:
    """Reset jobs stuck in ``in_progress`` back to ``failed`` for retry.

    This handles worker crashes — if a job has been ``in_progress`` for
    longer than STALE_JOB_TIMEOUT_SECONDS, it is assumed the worker died.

    Returns the number of recovered jobs.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_JOB_TIMEOUT_SECONDS)

    stmt = (
        update(WebhookJob)
        .where(
            WebhookJob.state == "in_progress",
            WebhookJob.started_at < cutoff,
        )
        .values(
            state="failed",
            last_error="Worker timeout — recovered by stale job sweeper",
        )
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount


def requeue_failed_jobs(db: Session) -> int:
    """Move ``failed`` jobs back to ``pending`` if they have retries remaining.

    Called periodically by the worker to re-enqueue jobs whose scheduled_at
    has passed.

    Returns the number of re-queued jobs.
    """
    now = datetime.now(timezone.utc)

    stmt = (
        update(WebhookJob)
        .where(
            WebhookJob.state == "failed",
            WebhookJob.scheduled_at <= now,
            WebhookJob.attempts < WebhookJob.max_attempts,
        )
        .values(state="pending")
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Synchronous delivery (original behavior, used when WEBHOOK_ASYNC=false)
# ---------------------------------------------------------------------------

def send_webhook(
    db: Session,
    tenant_id: int,
    agent_id: str,
    decision: PolicyDecision,
    *,
    _client: httpx.Client | None = None,
) -> bool:
    """Send a webhook notification synchronously. Returns True if sent successfully.

    The optional ``_client`` parameter is for test injection only.
    """
    webhook = db.scalars(
        select(PolicyWebhook).where(
            PolicyWebhook.tenant_id == tenant_id,
            PolicyWebhook.enabled.is_(True),
        )
    ).first()

    if webhook is None:
        return False

    payload = _build_payload(agent_id, decision)
    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    signature = compute_signature(payload_bytes, webhook.secret)

    headers = {
        "Content-Type": "application/json",
        "X-ATB-Signature": signature,
    }

    client = _client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    close_client = _client is None

    try:
        return _send_with_retry(
            db, webhook.id, tenant_id, client, webhook.url, payload_bytes, headers,
        )
    finally:
        if close_client:
            client.close()


def _send_with_retry(
    db: Session,
    webhook_id: int,
    tenant_id: int,
    client: httpx.Client,
    url: str,
    payload_bytes: bytes,
    headers: dict[str, str],
) -> bool:
    """POST with exponential backoff. Returns True on 2xx, False after exhausting retries."""
    backoff = INITIAL_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.post(url, content=payload_bytes, headers=headers)
            if 200 <= resp.status_code < 300:
                logger.info("Webhook delivered (attempt %d): %s", attempt, url)
                _record_delivery(
                    db, webhook_id, tenant_id, attempt,
                    status_code=resp.status_code, success=True,
                )
                return True
            logger.warning(
                "Webhook attempt %d/%d got status %d from %s",
                attempt, MAX_RETRIES, resp.status_code, url,
            )
            _record_delivery(
                db, webhook_id, tenant_id, attempt,
                status_code=resp.status_code, success=False,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Webhook attempt %d/%d failed: %s — %s",
                attempt, MAX_RETRIES, url, exc,
            )
            _record_delivery(
                db, webhook_id, tenant_id, attempt,
                error=str(exc), success=False,
            )

        if attempt < MAX_RETRIES:
            time.sleep(backoff)
            backoff *= BACKOFF_MULTIPLIER

    logger.error("Webhook delivery failed after %d attempts: %s", MAX_RETRIES, url)
    return False
