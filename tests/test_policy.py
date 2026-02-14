"""Tests for the policy evaluation endpoint and threshold logic.

Covers:
- Default policy thresholds (allow/review/block)
- Agent-level override thresholds
- Boundary conditions at exact threshold values
- Tenant isolation (cannot evaluate another tenant's agent events)
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
from app.models import (
    AgentPolicyOverride,
    ApiKey,
    Base,
    PolicyConfig,
    Tenant,
)
from app.rate_limit import reset_rate_limits


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_RAW_KEY_T2 = "atb_test-tenant2-key-bbbbbbbbbbbbbbbbbbbbbbbb"
_H1 = {"X-API-Key": _RAW_KEY_T1}
_H2 = {"X-API-Key": _RAW_KEY_T2}


def _make_db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    return engine, session_factory


def _seed(session_factory, *, with_overrides: bool = False, custom_thresholds: dict | None = None):
    """Seed 2 tenants, 2 keys, default policy configs."""
    db = session_factory()
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

    # Default policy configs
    t = custom_thresholds or {}
    db.add_all([
        PolicyConfig(tenant_id=1, allow_threshold=t.get("allow", 80.0), review_threshold=t.get("review", 60.0), block_threshold=t.get("block", 40.0)),
        PolicyConfig(tenant_id=2, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0),
    ])
    db.commit()

    if with_overrides:
        db.add(AgentPolicyOverride(
            tenant_id=1,
            agent_id="agent-strict",
            allow_threshold=90.0,
            review_threshold=70.0,
            block_threshold=50.0,
        ))
        db.commit()

    db.close()


@pytest.fixture
def policy_client() -> TestClient:
    """Client with auth, 2 tenants, default policy configs."""
    engine, session_factory = _make_db()
    _seed(session_factory)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


@pytest.fixture
def override_client() -> TestClient:
    """Client with agent-level policy overrides."""
    engine, session_factory = _make_db()
    _seed(session_factory, with_overrides=True)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    original = settings.require_auth
    settings.require_auth = True
    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        settings.require_auth = original
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


def _ingest(client: TestClient, headers: dict, event_id: str, agent_id: str, event_type: str = "safe_tool_usage") -> None:
    r = client.post(
        "/v1/intake/events",
        json={
            "event_id": event_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "source": "test",
            "occurred_at": "2026-02-13T12:00:00Z",
            "metadata": {},
        },
        headers=headers,
    )
    assert r.status_code == 200


# ---------- Default thresholds ----------

def test_policy_decision_allow_with_high_score(policy_client: TestClient) -> None:
    """Agent with many positive events gets 'allow'."""
    for i in range(10):
        _ingest(policy_client, _H1, f"evt-allow-{i}", "agent-good", "human_approved_action")
    # score = 50 + 10*6 = 110 → clamped to 100 → >= 80 → allow
    r = policy_client.get("/v1/policy/decision/agent-good", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "allow"
    assert data["score"] == 100.0
    assert data["thresholds"]["allow"] == 80.0


def test_policy_decision_review_with_medium_score(policy_client: TestClient) -> None:
    """Agent with baseline + a couple positive events gets 'review'."""
    # 2x safe_tool_usage = 50 + 4 = 54  → but that's below review(60)
    # 2x task_completed_without_rework = 50 + 8 = 58 → still below 60
    # 3x task_completed_without_rework = 50 + 12 = 62 → >= 60, < 80 → review
    for i in range(3):
        _ingest(policy_client, _H1, f"evt-rev-{i}", "agent-mid", "task_completed_without_rework")
    r = policy_client.get("/v1/policy/decision/agent-mid", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "review"
    assert data["score"] == 62.0


def test_policy_decision_block_with_low_score(policy_client: TestClient) -> None:
    """Agent with heavy negative events gets 'block'."""
    _ingest(policy_client, _H1, "evt-block-1", "agent-bad", "sensitive_data_leak_attempt")
    # 50 + (-25) = 25 → < 40 → block
    r = policy_client.get("/v1/policy/decision/agent-bad", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "block"
    assert data["score"] == 25.0


def test_policy_decision_baseline_is_review(policy_client: TestClient) -> None:
    """Agent with no events has score 50 → review zone (>= 40, < 60)."""
    r = policy_client.get("/v1/policy/decision/agent-new", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "review"
    assert data["score"] == 50.0


# ---------- Boundary cases ----------

def test_policy_boundary_exact_allow_threshold(policy_client: TestClient) -> None:
    """Score exactly at allow threshold → allow."""
    # 50 + 5*6 = 80 → exactly at allow → allow
    for i in range(5):
        _ingest(policy_client, _H1, f"evt-boundary-a-{i}", "agent-boundary-allow", "human_approved_action")
    r = policy_client.get("/v1/policy/decision/agent-boundary-allow", headers=_H1)
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"
    assert r.json()["score"] == 80.0


def test_policy_boundary_exact_review_threshold(policy_client: TestClient) -> None:
    """Score exactly at review threshold → review."""
    # Need score = 60.0 exactly. 50 + 2*3 + 1*4 = 60
    _ingest(policy_client, _H1, "evt-br-1", "agent-boundary-review", "policy_compliant_response")
    _ingest(policy_client, _H1, "evt-br-2", "agent-boundary-review", "policy_compliant_response")
    _ingest(policy_client, _H1, "evt-br-3", "agent-boundary-review", "task_completed_without_rework")
    r = policy_client.get("/v1/policy/decision/agent-boundary-review", headers=_H1)
    assert r.status_code == 200
    assert r.json()["decision"] == "review"
    assert r.json()["score"] == 60.0


def test_policy_boundary_exact_block_threshold(policy_client: TestClient) -> None:
    """Score exactly at block threshold → review (not block)."""
    # Need score = 40.0 exactly. 50 + (-10) = 40
    _ingest(policy_client, _H1, "evt-bb-1", "agent-boundary-block", "manual_rollback_required")
    r = policy_client.get("/v1/policy/decision/agent-boundary-block", headers=_H1)
    assert r.status_code == 200
    assert r.json()["decision"] == "review"
    assert r.json()["score"] == 40.0


def test_policy_boundary_just_below_block(policy_client: TestClient) -> None:
    """Score just below block threshold → block."""
    # 50 + (-15) = 35 → < 40 → block
    _ingest(policy_client, _H1, "evt-jbb-1", "agent-just-below-block", "unsafe_tool_call")
    r = policy_client.get("/v1/policy/decision/agent-just-below-block", headers=_H1)
    assert r.status_code == 200
    assert r.json()["decision"] == "block"
    assert r.json()["score"] == 35.0


# ---------- Agent override thresholds ----------

def test_policy_agent_override_uses_custom_thresholds(override_client: TestClient) -> None:
    """Agent 'agent-strict' has tighter thresholds via override."""
    # Baseline score 50. Override: allow=90, review=70, block=50.
    # 50 >= block(50) but < review(70) → review
    r = override_client.get("/v1/policy/decision/agent-strict", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "review"
    assert data["thresholds"]["allow"] == 90.0
    assert data["thresholds"]["review"] == 70.0
    assert data["thresholds"]["block"] == 50.0


def test_policy_agent_without_override_uses_tenant_defaults(override_client: TestClient) -> None:
    """Agent without override falls back to tenant policy config."""
    r = override_client.get("/v1/policy/decision/agent-normal", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    # Baseline 50 → >=40, <60 → review with default thresholds
    assert data["thresholds"]["allow"] == 80.0
    assert data["thresholds"]["review"] == 60.0
    assert data["thresholds"]["block"] == 40.0


# ---------- Tenant isolation ----------

def test_policy_tenant_isolation(policy_client: TestClient) -> None:
    """Tenant A's events don't influence Tenant B's policy decision for the same agent_id."""
    # Tenant A ingests positive events → agent-shared score goes up
    for i in range(5):
        _ingest(policy_client, _H1, f"evt-iso-{i}", "agent-shared", "human_approved_action")

    r1 = policy_client.get("/v1/policy/decision/agent-shared", headers=_H1)
    assert r1.status_code == 200
    assert r1.json()["decision"] == "allow"
    assert r1.json()["score"] == 80.0

    # Tenant B has no events → baseline 50 → review
    r2 = policy_client.get("/v1/policy/decision/agent-shared", headers=_H2)
    assert r2.status_code == 200
    assert r2.json()["decision"] == "review"
    assert r2.json()["score"] == 50.0


# ---------- Response structure ----------

def test_policy_response_has_all_fields(policy_client: TestClient) -> None:
    """Response includes all expected fields."""
    r = policy_client.get("/v1/policy/decision/agent-any", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert "agent_id" in data
    assert "decision" in data
    assert "score" in data
    assert "thresholds" in data
    assert "explanation" in data
    assert set(data["thresholds"].keys()) == {"allow", "review", "block"}
    assert data["decision"] in {"allow", "review", "block"}
