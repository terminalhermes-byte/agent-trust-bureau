"""Webhook notification service.

Sends HMAC-signed POST requests to tenant webhook URLs when a policy decision
is computed. Retries with exponential backoff on failure (bounded).
Each attempt is logged to the ``webhook_deliveries`` table for operator visibility.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PolicyWebhook, WebhookDelivery
from app.services.policy import PolicyDecision

logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
REQUEST_TIMEOUT_SECONDS = 10.0


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


def send_webhook(
    db: Session,
    tenant_id: int,
    agent_id: str,
    decision: PolicyDecision,
    *,
    _client: httpx.Client | None = None,
) -> bool:
    """Send a webhook notification for a policy decision. Returns True if sent successfully.

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
