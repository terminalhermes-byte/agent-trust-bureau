"""Tests that verify cross-tenant data isolation.

Uses the same auth_client fixture from test_auth (two tenants, two keys).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_api_key
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import ApiKey, Base, Tenant
from app.rate_limit import reset_rate_limits


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


@pytest.fixture
def mt_client() -> TestClient:
    """Multi-tenant client with auth enabled and two tenants."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    db = testing_session()
    db.add_all([
        Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True),
        Tenant(id=2, name="Tenant B", slug="tenant-b", is_active=True),
    ])
    db.commit()
    db.add_all([
        ApiKey(tenant_id=1, key_prefix=_RAW_KEY_T1[:12], key_hash=hash_api_key(_RAW_KEY_T1), name="t1-key"),
        ApiKey(tenant_id=2, key_prefix=_RAW_KEY_T2[:12], key_hash=hash_api_key(_RAW_KEY_T2), name="t2-key"),
    ])
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


def _ingest(client: TestClient, headers: dict, event_id: str, agent_id: str = "agent-shared") -> None:
    r = client.post(
        "/v1/intake/events",
        json={
            "event_id": event_id,
            "agent_id": agent_id,
            "event_type": "safe_tool_usage",
            "source": "test",
            "occurred_at": "2026-02-13T12:00:00Z",
            "metadata": {},
        },
        headers=headers,
    )
    assert r.status_code == 200


def test_tenant_cannot_see_other_tenants_events(mt_client: TestClient) -> None:
    """Tenant A ingests events. Tenant B cannot read them."""
    _ingest(mt_client, _H1, "evt-iso-1", "agent-shared")
    _ingest(mt_client, _H1, "evt-iso-2", "agent-shared")

    # Tenant A sees both
    r1 = mt_client.get("/v1/intake/events/agent-shared", headers=_H1)
    assert r1.status_code == 200
    assert r1.json()["event_count"] == 2

    # Tenant B sees none
    r2 = mt_client.get("/v1/intake/events/agent-shared", headers=_H2)
    assert r2.status_code == 200
    assert r2.json()["event_count"] == 0


def test_tenant_cannot_see_other_tenants_score(mt_client: TestClient) -> None:
    """Tenant A's score reflects its events; Tenant B gets baseline for same agent_id."""
    _ingest(mt_client, _H1, "evt-score-1", "agent-scored")

    r1 = mt_client.get("/v1/trust/score/agent-scored", headers=_H1)
    assert r1.status_code == 200
    assert r1.json()["trust_score"] == 52.0  # baseline 50 + safe_tool_usage 2

    r2 = mt_client.get("/v1/trust/score/agent-scored", headers=_H2)
    assert r2.status_code == 200
    assert r2.json()["trust_score"] == 50.0  # baseline — no events for tenant B


def test_tenant_cannot_see_other_tenants_history(mt_client: TestClient) -> None:
    """Score history is scoped to tenant."""
    _ingest(mt_client, _H1, "evt-hist-1", "agent-hist")

    # Generate snapshot for tenant A
    mt_client.get("/v1/trust/score/agent-hist", headers=_H1)

    # Tenant A sees history
    r1 = mt_client.get("/v1/trust/score/agent-hist/history", headers=_H1)
    assert r1.status_code == 200
    assert r1.json()["count"] == 1

    # Tenant B sees empty history
    r2 = mt_client.get("/v1/trust/score/agent-hist/history", headers=_H2)
    assert r2.status_code == 200
    assert r2.json()["count"] == 0


def test_same_event_id_allowed_across_tenants(mt_client: TestClient) -> None:
    """event_id uniqueness is per-tenant, not global."""
    _ingest(mt_client, _H1, "evt-shared-id", "agent-x")
    _ingest(mt_client, _H2, "evt-shared-id", "agent-x")  # should NOT 409

    r1 = mt_client.get("/v1/intake/events/agent-x", headers=_H1)
    assert r1.json()["event_count"] == 1

    r2 = mt_client.get("/v1/intake/events/agent-x", headers=_H2)
    assert r2.json()["event_count"] == 1
