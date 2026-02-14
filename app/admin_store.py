"""Admin CRUD queries for policy configs, agent overrides, and webhooks.

All functions are tenant-scoped — they accept tenant_id and never touch
another tenant's data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentPolicyOverride, PolicyConfig, PolicyWebhook, WebhookDelivery


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
