from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import generate_api_key, hash_api_key
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import ApiKey, Base, Tenant
from app.rate_limit import reset_rate_limits


# Fixed raw keys for deterministic tests
_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_RAW_KEY_REVOKED = "atb_test-revoked-key-cccccccccccccccccccccc"


@pytest.fixture
def auth_client() -> TestClient:
    """Client with require_auth=True, two tenants, active + revoked keys."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    # Seed tenants and keys
    db = testing_session()
    t1 = Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True)
    t2 = Tenant(id=2, name="Tenant B", slug="tenant-b", is_active=True)
    db.add_all([t1, t2])
    db.commit()

    k1 = ApiKey(tenant_id=1, key_prefix=_RAW_KEY_T1[:12], key_hash=hash_api_key(_RAW_KEY_T1), name="t1-key")
    k2 = ApiKey(tenant_id=2, key_prefix=_RAW_KEY_T2[:12], key_hash=hash_api_key(_RAW_KEY_T2), name="t2-key")
    k_revoked = ApiKey(
        tenant_id=1,
        key_prefix=_RAW_KEY_REVOKED[:12],
        key_hash=hash_api_key(_RAW_KEY_REVOKED),
        name="revoked-key",
        is_active=False,
    )
    db.add_all([k1, k2, k_revoked])
    db.commit()
    db.close()

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = True

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


# --- public endpoints ---

def test_health_accessible_without_key(auth_client: TestClient) -> None:
    response = auth_client.get("/health")
    assert response.status_code == 200


def test_root_accessible_without_key(auth_client: TestClient) -> None:
    response = auth_client.get("/")
    assert response.status_code == 200


# --- auth pass/fail ---

def test_v1_rejects_missing_key(auth_client: TestClient) -> None:
    response = auth_client.get("/v1/trust/score/agent-1")
    assert response.status_code == 401
    assert "Invalid or missing API key" in response.json()["detail"]


def test_v1_rejects_wrong_key(auth_client: TestClient) -> None:
    response = auth_client.get(
        "/v1/trust/score/agent-1",
        headers={"X-API-Key": "atb_totally-bogus-key-zzzzzzzzzzzzzzzzzzz"},
    )
    assert response.status_code == 401


def test_v1_rejects_revoked_key(auth_client: TestClient) -> None:
    response = auth_client.get(
        "/v1/trust/score/agent-1",
        headers={"X-API-Key": _RAW_KEY_REVOKED},
    )
    assert response.status_code == 401


def test_v1_accepts_valid_key(auth_client: TestClient) -> None:
    response = auth_client.get(
        "/v1/trust/score/agent-1",
        headers={"X-API-Key": _RAW_KEY_T1},
    )
    assert response.status_code == 200


def test_ingest_requires_key(auth_client: TestClient) -> None:
    payload = {
        "event_id": "evt-auth-1",
        "agent_id": "agent-1",
        "event_type": "safe_tool_usage",
        "source": "test",
        "occurred_at": "2026-02-13T12:00:00Z",
        "metadata": {},
    }
    response = auth_client.post("/v1/intake/events", json=payload)
    assert response.status_code == 401

    response = auth_client.post(
        "/v1/intake/events",
        json=payload,
        headers={"X-API-Key": _RAW_KEY_T1},
    )
    assert response.status_code == 200


def test_no_auth_required_allows_all(client: TestClient) -> None:
    """Default client has require_auth=False so all requests go through."""
    response = client.get("/v1/trust/score/agent-1")
    assert response.status_code == 200
