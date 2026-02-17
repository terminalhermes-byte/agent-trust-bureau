"""Admin CRUD queries for policy configs, agent overrides, and webhooks.

All functions are tenant-scoped — they accept tenant_id and never touch
another tenant's data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import generate_api_key, hash_api_key
from app.models import AgentPolicyOverride, ApiKey, PolicyConfig, PolicyWebhook, WebhookDelivery, WebhookJob


# ---------------------------------------------------------------------------
# Policy Config
# ---------------------------------------------------------------------------

def get_policy_config(db: Session, tenant_id: int) -> PolicyConfig | None:
    return db.scalars(
        select(PolicyConfig).where(PolicyConfig.tenant_id == tenant_id)
    ).first()


def upsert_policy_config(
    db: Session,
    tenant_id: int,
    *,
    allow_threshold: float,
    review_threshold: float,
    block_threshold: float,
) -> PolicyConfig:
    cfg = get_policy_config(db, tenant_id)
    if cfg is None:
        cfg = PolicyConfig(
            tenant_id=tenant_id,
            allow_threshold=allow_threshold,
            review_threshold=review_threshold,
            block_threshold=block_threshold,
        )
        db.add(cfg)
    else:
        cfg.allow_threshold = allow_threshold
        cfg.review_threshold = review_threshold
        cfg.block_threshold = block_threshold
    db.commit()
    db.refresh(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Agent Policy Overrides
# ---------------------------------------------------------------------------

def create_agent_override(
    db: Session,
    tenant_id: int,
    *,
    agent_id: str,
    allow_threshold: float | None,
    review_threshold: float | None,
    block_threshold: float | None,
) -> AgentPolicyOverride:
    override = AgentPolicyOverride(
        tenant_id=tenant_id,
        agent_id=agent_id,
        allow_threshold=allow_threshold,
        review_threshold=review_threshold,
        block_threshold=block_threshold,
    )
    db.add(override)
    db.commit()
    db.refresh(override)
    return override


def delete_agent_override(db: Session, tenant_id: int, agent_id: str) -> bool:
    """Delete an override. Returns True if a row was deleted."""
    override = db.scalars(
        select(AgentPolicyOverride).where(
            AgentPolicyOverride.tenant_id == tenant_id,
            AgentPolicyOverride.agent_id == agent_id,
        )
    ).first()
    if override is None:
        return False
    db.delete(override)
    db.commit()
    return True


def list_agent_overrides(
    db: Session, tenant_id: int, *, limit: int = 50
) -> list[AgentPolicyOverride]:
    stmt = (
        select(AgentPolicyOverride)
        .where(AgentPolicyOverride.tenant_id == tenant_id)
        .order_by(AgentPolicyOverride.agent_id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def create_webhook(
    db: Session, tenant_id: int, *, url: str
) -> PolicyWebhook:
    secret = secrets.token_urlsafe(32)
    webhook = PolicyWebhook(
        tenant_id=tenant_id,
        url=url,
        secret=secret,
        enabled=True,
        revoked_at=None,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return webhook


def get_webhook(db: Session, webhook_id: int, tenant_id: int) -> PolicyWebhook | None:
    webhook = db.get(PolicyWebhook, webhook_id)
    if webhook is None or webhook.tenant_id != tenant_id:
        return None
    return webhook


def patch_webhook(
    db: Session,
    webhook_id: int,
    tenant_id: int,
    *,
    enabled: bool | None = None,
    rotate_secret: bool | None = None,
) -> PolicyWebhook | None:
    webhook = get_webhook(db, webhook_id, tenant_id)
    if webhook is None:
        return None
    if enabled is not None:
        webhook.enabled = enabled
        webhook.revoked_at = None if enabled else datetime.now(timezone.utc)
    if rotate_secret:
        webhook.secret = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(webhook)
    return webhook


def list_webhooks(db: Session, tenant_id: int) -> list[PolicyWebhook]:
    stmt = (
        select(PolicyWebhook)
        .where(PolicyWebhook.tenant_id == tenant_id)
        .order_by(PolicyWebhook.id.asc())
    )
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Webhook Deliveries
# ---------------------------------------------------------------------------

def list_webhook_deliveries(
    db: Session, webhook_id: int, tenant_id: int, *, limit: int = 50
) -> list[WebhookDelivery]:
    """Return recent delivery attempts for a specific webhook, tenant-scoped."""
    stmt = (
        select(WebhookDelivery)
        .where(
            WebhookDelivery.webhook_id == webhook_id,
            WebhookDelivery.tenant_id == tenant_id,
        )
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Webhook Jobs (async queue visibility)
# ---------------------------------------------------------------------------

def list_webhook_jobs(
    db: Session, webhook_id: int, tenant_id: int, *, limit: int = 50,
    state: str | None = None,
) -> list[WebhookJob]:
    """Return recent jobs for a specific webhook, tenant-scoped."""
    stmt = (
        select(WebhookJob)
        .where(
            WebhookJob.webhook_id == webhook_id,
            WebhookJob.tenant_id == tenant_id,
        )
    )
    if state is not None:
        stmt = stmt.where(WebhookJob.state == state)
    stmt = stmt.order_by(WebhookJob.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def get_webhook_job(db: Session, job_id: int, tenant_id: int) -> WebhookJob | None:
    """Fetch a single job by ID, tenant-scoped."""
    job = db.get(WebhookJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


# ---------------------------------------------------------------------------
# API Key Management
# ---------------------------------------------------------------------------

def create_api_key_for_tenant(
    db: Session, tenant_id: int, *, name: str = "default",
) -> tuple[ApiKey, str]:
    """Create a new API key for *tenant_id*.

    Returns (key_row, raw_key).  The raw key is returned exactly once.
    """
    raw_key, prefix = generate_api_key()
    key_row = ApiKey(
        tenant_id=tenant_id,
        key_prefix=prefix,
        key_hash=hash_api_key(raw_key),
        name=name,
    )
    db.add(key_row)
    db.commit()
    db.refresh(key_row)
    return key_row, raw_key


def list_api_keys(
    db: Session, tenant_id: int, *, limit: int = 50,
) -> list[ApiKey]:
    stmt = (
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def revoke_api_key(db: Session, key_id: int, tenant_id: int) -> ApiKey | None:
    """Revoke (soft-delete) a key. Returns the key row, or None if not found."""
    key = db.get(ApiKey, key_id)
    if key is None or key.tenant_id != tenant_id:
        return None
    if not key.is_active:
        return key  # already revoked, idempotent
    key.is_active = False
    key.revoked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(key)
    return key


# ---------------------------------------------------------------------------
# Webhook Replay
# ---------------------------------------------------------------------------

def replay_webhook_job(db: Session, job: WebhookJob) -> WebhookJob:
    """Reset a failed/dead job back to pending for re-delivery.

    Preserves original payload and audit trail (attempts counter keeps going).
    """
    job.state = "pending"
    job.scheduled_at = datetime.now(timezone.utc)
    job.started_at = None
    job.completed_at = None
    db.commit()
    db.refresh(job)
    return job


def get_last_failed_job(
    db: Session, webhook_id: int, tenant_id: int,
) -> WebhookJob | None:
    """Get the most recent failed or dead job for a webhook."""
    stmt = (
        select(WebhookJob)
        .where(
            WebhookJob.webhook_id == webhook_id,
            WebhookJob.tenant_id == tenant_id,
            WebhookJob.state.in_(["failed", "dead"]),
        )
        .order_by(WebhookJob.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


# ---------------------------------------------------------------------------
# Webhook Queue Stats
# ---------------------------------------------------------------------------

def get_webhook_stats(
    db: Session, webhook_id: int, tenant_id: int,
) -> dict[str, int | float | None]:
    """Return counts by state + recent success rate for a webhook."""
    # Counts by state
    stmt = (
        select(WebhookJob.state, func.count())
        .where(
            WebhookJob.webhook_id == webhook_id,
            WebhookJob.tenant_id == tenant_id,
        )
        .group_by(WebhookJob.state)
    )
    rows = db.execute(stmt).all()
    counts: dict[str, int] = {state: cnt for state, cnt in rows}

    # Recent success rate (last 100 deliveries)
    delivery_stmt = (
        select(WebhookDelivery.success)
        .where(
            WebhookDelivery.webhook_id == webhook_id,
            WebhookDelivery.tenant_id == tenant_id,
        )
        .order_by(WebhookDelivery.id.desc())
        .limit(100)
    )
    deliveries = list(db.scalars(delivery_stmt).all())
    if deliveries:
        success_rate = round(sum(1 for s in deliveries if s) / len(deliveries) * 100, 1)
    else:
        success_rate = None

    return {
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "failed": counts.get("failed", 0),
        "dead": counts.get("dead", 0),
        "completed": counts.get("completed", 0),
        "recent_success_rate": success_rate,
    }


# ---------------------------------------------------------------------------
# Retention Cleanup
# ---------------------------------------------------------------------------

def cleanup_old_records(
    db: Session,
    *,
    before: datetime,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Delete deliveries and terminal jobs older than *before*.

    Terminal jobs are those in completed/dead state.
    Returns (deliveries_deleted, jobs_deleted).
    """
    # Count first
    del_count_stmt = (
        select(func.count())
        .select_from(WebhookDelivery)
        .where(WebhookDelivery.created_at < before)
    )
    deliveries_count = db.scalar(del_count_stmt) or 0

    job_count_stmt = (
        select(func.count())
        .select_from(WebhookJob)
        .where(
            WebhookJob.created_at < before,
            WebhookJob.state.in_(["completed", "dead"]),
        )
    )
    jobs_count = db.scalar(job_count_stmt) or 0

    if dry_run:
        return deliveries_count, jobs_count

    # Actually delete
    db.execute(
        delete(WebhookDelivery).where(WebhookDelivery.created_at < before)
    )
    db.execute(
        delete(WebhookJob).where(
            WebhookJob.created_at < before,
            WebhookJob.state.in_(["completed", "dead"]),
        )
    )
    db.commit()
    return deliveries_count, jobs_count
