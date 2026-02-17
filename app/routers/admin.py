"""Admin CRUD endpoints for policy configuration, agent overrides, and webhooks.

All endpoints are tenant-scoped via AuthContext and protected by API key auth
(even when REQUIRE_AUTH is false — admin always requires a key).
Secrets are never returned in full — only the last 4 characters are exposed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_store import (
    create_agent_override,
    create_api_key_for_tenant,
    create_webhook,
    delete_agent_override,
    get_last_failed_job,
    get_policy_config,
    get_webhook,
    get_webhook_job,
    get_webhook_stats,
    list_agent_overrides,
    list_api_keys,
    list_webhook_deliveries,
    list_webhook_jobs,
    list_webhooks,
    patch_webhook,
    replay_webhook_job,
    revoke_api_key,
    upsert_policy_config,
)
from app.auth import AuthContext, require_api_key_strict
from app.db import get_db
from app.schemas import (
    AgentOverrideCreate,
    AgentOverrideListResponse,
    AgentOverrideOut,
    ApiKeyCreate,
    ApiKeyListResponse,
    ApiKeyOut,
    PolicyConfigOut,
    PolicyConfigUpdate,
    WebhookCreate,
    WebhookDeliveryListResponse,
    WebhookDeliveryOut,
    WebhookJobListResponse,
    WebhookJobOut,
    WebhookListResponse,
    WebhookOut,
    WebhookPatch,
    WebhookReplayRequest,
    WebhookReplayResponse,
    WebhookStatsOut,
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
    auth: AuthContext = Depends(require_api_key_strict),
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
    auth: AuthContext = Depends(require_api_key_strict),
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
    auth: AuthContext = Depends(require_api_key_strict),
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
    auth: AuthContext = Depends(require_api_key_strict),
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
    auth: AuthContext = Depends(require_api_key_strict),
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
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookOut:
    try:
        wh = create_webhook(db, auth.tenant_id, url=body.url)
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
        secret=wh.secret,
        enabled=wh.enabled,
        revoked_at=wh.revoked_at,
        created_at=wh.created_at,
        updated_at=wh.updated_at,
    )


@router.patch("/policy/webhooks/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: int,
    body: WebhookPatch,
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookOut:
    rotate_value = bool(body.rotate_secret)
    wh = patch_webhook(
        db,
        webhook_id,
        auth.tenant_id,
        enabled=body.enabled,
        rotate_secret=rotate_value,
    )
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    return WebhookOut(
        id=wh.id,
        url=wh.url,
        secret_last4=_secret_last4(wh.secret),
        secret=wh.secret if rotate_value else None,
        enabled=wh.enabled,
        revoked_at=wh.revoked_at,
        created_at=wh.created_at,
        updated_at=wh.updated_at,
    )


@router.get("/policy/webhooks", response_model=WebhookListResponse)
def get_webhooks(
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookListResponse:
    webhooks = list_webhooks(db, auth.tenant_id)
    items = [
        WebhookOut(
            id=wh.id,
            url=wh.url,
            secret_last4=_secret_last4(wh.secret),
            secret=None,
            enabled=wh.enabled,
            revoked_at=wh.revoked_at,
            created_at=wh.created_at,
            updated_at=wh.updated_at,
        )
        for wh in webhooks
    ]
    return WebhookListResponse(count=len(items), webhooks=items)


# ---------------------------------------------------------------------------
# Webhook Deliveries: GET (list)
# ---------------------------------------------------------------------------

@router.get("/policy/webhooks/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
def get_deliveries(
    webhook_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookDeliveryListResponse:
    # Verify webhook exists and belongs to this tenant
    wh = get_webhook(db, webhook_id, auth.tenant_id)
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    deliveries = list_webhook_deliveries(db, webhook_id, auth.tenant_id, limit=limit)
    items = [
        WebhookDeliveryOut(
            id=d.id,
            webhook_id=d.webhook_id,
            attempt=d.attempt,
            status_code=d.status_code,
            error=d.error,
            success=d.success,
            created_at=d.created_at,
        )
        for d in deliveries
    ]
    return WebhookDeliveryListResponse(count=len(items), deliveries=items)


# ---------------------------------------------------------------------------
# Webhook Jobs: GET (list)
# ---------------------------------------------------------------------------

@router.get("/policy/webhooks/{webhook_id}/jobs", response_model=WebhookJobListResponse)
def get_jobs(
    webhook_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    state: str | None = Query(default=None),
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookJobListResponse:
    # Verify webhook exists and belongs to this tenant
    wh = get_webhook(db, webhook_id, auth.tenant_id)
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    jobs = list_webhook_jobs(db, webhook_id, auth.tenant_id, limit=limit, state=state)
    items = [
        WebhookJobOut(
            id=j.id,
            webhook_id=j.webhook_id,
            tenant_id=j.tenant_id,
            agent_id=j.agent_id,
            state=j.state,
            attempts=j.attempts,
            max_attempts=j.max_attempts,
            last_error=j.last_error,
            scheduled_at=j.scheduled_at,
            started_at=j.started_at,
            completed_at=j.completed_at,
            created_at=j.created_at,
        )
        for j in jobs
    ]
    return WebhookJobListResponse(count=len(items), jobs=items)


# ---------------------------------------------------------------------------
# Webhook Stats: GET
# ---------------------------------------------------------------------------

@router.get("/policy/webhooks/{webhook_id}/stats", response_model=WebhookStatsOut)
def get_stats(
    webhook_id: int,
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookStatsOut:
    wh = get_webhook(db, webhook_id, auth.tenant_id)
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    stats = get_webhook_stats(db, webhook_id, auth.tenant_id)
    return WebhookStatsOut(webhook_id=webhook_id, **stats)


# ---------------------------------------------------------------------------
# Webhook Replay: POST
# ---------------------------------------------------------------------------

@router.post("/policy/webhooks/{webhook_id}/replay", response_model=WebhookReplayResponse)
def replay(
    webhook_id: int,
    body: WebhookReplayRequest,
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> WebhookReplayResponse:
    # Verify webhook belongs to tenant
    wh = get_webhook(db, webhook_id, auth.tenant_id)
    if wh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

    replayed_jobs: list[WebhookJobOut] = []

    if body.job_id is not None:
        job = get_webhook_job(db, body.job_id, auth.tenant_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        if job.webhook_id != webhook_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job does not belong to this webhook")
        if job.state not in ("failed", "dead"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Job is in state '{job.state}'; only failed/dead jobs can be replayed",
            )
        job = replay_webhook_job(db, job)
        replayed_jobs.append(_job_to_out(job))

    elif body.last_failed:
        job = get_last_failed_job(db, webhook_id, auth.tenant_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No failed/dead jobs found for this webhook",
            )
        job = replay_webhook_job(db, job)
        replayed_jobs.append(_job_to_out(job))

    return WebhookReplayResponse(replayed=len(replayed_jobs), jobs=replayed_jobs)


def _job_to_out(j) -> WebhookJobOut:
    return WebhookJobOut(
        id=j.id,
        webhook_id=j.webhook_id,
        tenant_id=j.tenant_id,
        agent_id=j.agent_id,
        state=j.state,
        attempts=j.attempts,
        max_attempts=j.max_attempts,
        last_error=j.last_error,
        scheduled_at=j.scheduled_at,
        started_at=j.started_at,
        completed_at=j.completed_at,
        created_at=j.created_at,
    )


# ---------------------------------------------------------------------------
# API Keys: POST / GET / POST (revoke)
# ---------------------------------------------------------------------------

@router.post("/keys", response_model=ApiKeyOut, status_code=status.HTTP_201_CREATED)
def create_key(
    body: ApiKeyCreate,
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> ApiKeyOut:
    key_row, raw_key = create_api_key_for_tenant(db, auth.tenant_id, name=body.name)
    return ApiKeyOut(
        id=key_row.id,
        name=key_row.name,
        key_prefix=key_row.key_prefix,
        is_active=key_row.is_active,
        created_at=key_row.created_at,
        revoked_at=key_row.revoked_at,
        raw_key=raw_key,
    )


@router.get("/keys", response_model=ApiKeyListResponse)
def get_keys(
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> ApiKeyListResponse:
    keys = list_api_keys(db, auth.tenant_id, limit=limit)
    items = [
        ApiKeyOut(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_at=k.created_at,
            revoked_at=k.revoked_at,
            raw_key=None,
        )
        for k in keys
    ]
    return ApiKeyListResponse(count=len(items), keys=items)


@router.post("/keys/{key_id}/revoke", response_model=ApiKeyOut)
def revoke_key(
    key_id: int,
    auth: AuthContext = Depends(require_api_key_strict),
    db: Session = Depends(get_db),
) -> ApiKeyOut:
    key = revoke_api_key(db, key_id, auth.tenant_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        is_active=key.is_active,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
        raw_key=None,
    )
