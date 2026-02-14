"""Admin CRUD endpoints for policy configuration, agent overrides, and webhooks.

All endpoints are tenant-scoped via AuthContext and protected by API key auth.
Secrets are never returned in full — only the last 4 characters are exposed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_store import (
    create_agent_override,
    create_webhook,
    delete_agent_override,
    get_policy_config,
    list_agent_overrides,
    list_webhooks,
    patch_webhook,
    upsert_policy_config,
)
from app.auth import AuthContext, require_api_key
from app.db import get_db
from app.schemas import (
    AgentOverrideCreate,
    AgentOverrideListResponse,
    AgentOverrideOut,
    PolicyConfigOut,
    PolicyConfigUpdate,
    WebhookCreate,
    WebhookListResponse,
    WebhookOut,
    WebhookPatch,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _secret_last4(secret: str) -> str:
    """Return the last 4 characters of a secret, or masked if too short."""
    if len(secret) >= 4:
        return "***" + secret[-4:]
    return "***"


# ---------------------------------------------------------------------------
# Policy Config: GET / PUT
# ---------------------------------------------------------------------------

@router.get("/policy/config", response_model=PolicyConfigOut)
def get_config(
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PolicyConfigOut:
    cfg = get_policy_config(db, auth.tenant_id)
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No policy config found")
    return PolicyConfigOut(
        allow_threshold=cfg.allow_threshold,
        review_threshold=cfg.review_threshold,
        block_threshold=cfg.block_threshold,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@router.put("/policy/config", response_model=PolicyConfigOut)
def update_config(
    body: PolicyConfigUpdate,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PolicyConfigOut:
    cfg = upsert_policy_config(
        db,
        auth.tenant_id,
        allow_threshold=body.allow_threshold,
        review_threshold=body.review_threshold,
        block_threshold=body.block_threshold,
    )
    return PolicyConfigOut(
        allow_threshold=cfg.allow_threshold,
        review_threshold=cfg.review_threshold,
        block_threshold=cfg.block_threshold,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


# ---------------------------------------------------------------------------
# Agent Policy Overrides: POST / DELETE / GET (list)
# ---------------------------------------------------------------------------

@router.post("/policy/overrides", response_model=AgentOverrideOut, status_code=status.HTTP_201_CREATED)
def create_override(
    body: AgentOverrideCreate,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> AgentOverrideOut:
    try:
        override = create_agent_override(
            db,
            auth.tenant_id,
            agent_id=body.agent_id,
            allow_threshold=body.allow_threshold,
            review_threshold=body.review_threshold,
            block_threshold=body.block_threshold,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Override for agent '{body.agent_id}' already exists",
        ) from exc
    return AgentOverrideOut(
        agent_id=override.agent_id,
        allow_threshold=override.allow_threshold,
        review_threshold=override.review_threshold,
        block_threshold=override.block_threshold,
        created_at=override.created_at,
        updated_at=override.updated_at,
    )


@router.delete("/policy/overrides/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def remove_override(
    agent_id: str,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> None:
    deleted = delete_agent_override(db, auth.tenant_id, agent_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No override for agent '{agent_id}'",
        )


@router.get("/policy/overrides", response_model=AgentOverrideListResponse)
def get_overrides(
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> AgentOverrideListResponse:
    overrides = list_agent_overrides(db, auth.tenant_id, limit=limit)
    items = [
        AgentOverrideOut(
            agent_id=o.agent_id,
            allow_threshold=o.allow_threshold,
            review_threshold=o.review_threshold,
            block_threshold=o.block_threshold,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )
        for o in overrides
    ]
    return AgentOverrideListResponse(count=len(items), overrides=items)


# ---------------------------------------------------------------------------
# Webhooks: POST / PATCH / GET (list)
# ---------------------------------------------------------------------------

@router.post("/policy/webhooks", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
def add_webhook(
    body: WebhookCreate,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> WebhookOut:
    try:
        wh = create_webhook(db, auth.tenant_id, url=body.url, secret=body.secret)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Webhook already exists for this tenant",
        ) from exc
    return WebhookOut(
        id=wh.id,
        url=wh.url,
        secret_last4=_secret_last4(wh.secret),
        enabled=wh.enabled,
        created_at=wh.created_at,
        updated_at=wh.updated_at,
    )


@router.patch("/policy/webhooks/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: int,
    body: WebhookPatch,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> WebhookOut:
    wh = patch_webhook(
        db,
        webhook_id,
        auth.tenant_id,
        enabled=body.enabled,
        rotate_secret=body.rotate_secret,
    )
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    return WebhookOut(
        id=wh.id,
        url=wh.url,
        secret_last4=_secret_last4(wh.secret),
        enabled=wh.enabled,
        created_at=wh.created_at,
        updated_at=wh.updated_at,
    )


@router.get("/policy/webhooks", response_model=WebhookListResponse)
def get_webhooks(
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> WebhookListResponse:
    webhooks = list_webhooks(db, auth.tenant_id)
    items = [
        WebhookOut(
            id=wh.id,
            url=wh.url,
            secret_last4=_secret_last4(wh.secret),
            enabled=wh.enabled,
            created_at=wh.created_at,
            updated_at=wh.updated_at,
        )
        for wh in webhooks
    ]
    return WebhookListResponse(count=len(items), webhooks=items)
