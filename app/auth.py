from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ApiKey, Tenant

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Default tenant ID used when auth is disabled (dev mode)
DEFAULT_TENANT_ID = 1


@dataclass
class AuthContext:
    """Resolved identity for the current request."""

    tenant_id: int
    key_name: str


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key. One-way, deterministic."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, prefix). The raw key is shown once; only the hash is stored."""
    raw = "atb_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    return raw, prefix


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _resolve_key(db: Session, raw_key: str) -> AuthContext | None:
    """Look up an active API key by prefix, then constant-time compare the hash."""
    prefix = raw_key[:12]
    candidates = list(
        db.scalars(
            select(ApiKey)
            .where(ApiKey.key_prefix == prefix, ApiKey.is_active.is_(True))
        ).all()
    )
    incoming_hash = hash_api_key(raw_key)
    for key_row in candidates:
        if _constant_time_compare(incoming_hash, key_row.key_hash):
            # Verify tenant is active
            tenant = db.get(Tenant, key_row.tenant_id)
            if tenant and tenant.is_active:
                return AuthContext(tenant_id=key_row.tenant_id, key_name=key_row.name)
    return None


def require_api_key(
    api_key: str | None = Security(_api_key_header),
    db: Session = Depends(get_db),
) -> AuthContext:
    if not settings.require_auth:
        # Dev mode: auth disabled, use default tenant
        return AuthContext(tenant_id=DEFAULT_TENANT_ID, key_name="anonymous")

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

    ctx = _resolve_key(db, api_key)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return ctx
