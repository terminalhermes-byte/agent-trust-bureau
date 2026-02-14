"""Tests for webhook signature generation and delivery with retries.

Uses httpx mocking via monkeypatch to avoid real network calls.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import httpx
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
    ApiKey,
    Base,
    PolicyConfig,
    PolicyWebhook,
    Tenant,
)
from app.rate_limit import reset_rate_limits
from app.services.policy import PolicyDecision, Thresholds
from app.services.webhook import compute_signature, send_webhook


_RAW_KEY_T1 = "atb_test-tenant1-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_H1 = {"X-API-Key": _RAW_KEY_T1}


# ---------- Unit tests for signature computation ----------

def test_compute_signature_deterministic() -> None:
    """Same payload + secret always produces the same signature."""
    payload = b'{"agent_id":"x","decision":"allow"}'
    secret = "my-secret-123"
    sig1 = compute_signature(payload, secret)
    sig2 = compute_signature(payload, secret)
    assert sig1 == sig2


def test_compute_signature_matches_manual_hmac() -> None:
    """Signature matches a manually computed HMAC-SHA256."""
    payload = b'{"test":true}'
    secret = "webhook-secret"
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert compute_signature(payload, secret) == expected


def test_compute_signature_changes_with_different_secret() -> None:
    """Different secret produces a different signature."""
    payload = b'{"data":"same"}'
    sig1 = compute_signature(payload, "secret-a")
    sig2 = compute_signature(payload, "secret-b")
    assert sig1 != sig2


# ---------- Integration: webhook delivery via policy endpoint ----------

def _make_db_and_seed(*, webhook_enabled: bool = True, webhook_url: str = "https://hooks.example.com/policy"):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    db = session_factory()
    db.add(Tenant(id=1, name="Tenant A", slug="tenant-a", is_active=True))
    db.commit()
    db.add(ApiKey(tenant_id=1, key_prefix=_RAW_KEY_T1[:12], key_hash=hash_api_key(_RAW_KEY_T1), name="t1-key"))
    db.add(PolicyConfig(tenant_id=1, allow_threshold=80.0, review_threshold=60.0, block_threshold=40.0))
    if webhook_enabled:
        db.add(PolicyWebhook(tenant_id=1, url=webhook_url, secret="test-webhook-secret", enabled=True))
    db.commit()
    db.close()
    return engine, session_factory


@pytest.fixture
def webhook_client():
    """Client with a configured webhook for tenant 1."""
    engine, session_factory = _make_db_and_seed(webhook_enabled=True)

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
def no_webhook_client():
    """Client without any webhook configured."""
    engine, session_factory = _make_db_and_seed(webhook_enabled=False)

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


def test_webhook_fires_on_policy_decision(webhook_client: TestClient) -> None:
    """Policy decision endpoint fires webhook with correct signature header."""
    with patch("app.services.webhook.httpx.Client") as MockClientClass:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        r = webhook_client.get("/v1/policy/decision/agent-test", headers=_H1)
        assert r.status_code == 200

        # Verify webhook was called
        assert mock_client.post.call_count == 1
        call_kwargs = mock_client.post.call_args
        posted_url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url")
        assert posted_url == "https://hooks.example.com/policy"

        # Verify signature header
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "X-ATB-Signature" in headers

        # Verify payload is valid JSON with expected fields
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content")
        payload = json.loads(content)
        assert payload["agent_id"] == "agent-test"
        assert payload["decision"] in {"allow", "review", "block"}
        assert "score" in payload
        assert "thresholds" in payload

        # Verify signature matches
        expected_sig = compute_signature(content, "test-webhook-secret")
        assert headers["X-ATB-Signature"] == expected_sig


def test_no_webhook_configured_still_returns_decision(no_webhook_client: TestClient) -> None:
    """Policy decision works fine even without a webhook configured."""
    r = no_webhook_client.get("/v1/policy/decision/agent-test", headers=_H1)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "review"  # baseline 50


def test_webhook_retry_on_failure(webhook_client: TestClient) -> None:
    """Webhook retries on non-2xx responses then gives up."""
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.services.webhook.time.sleep") as mock_sleep:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.post.return_value = mock_response
        MockClientClass.return_value = mock_client

        r = webhook_client.get("/v1/policy/decision/agent-retry", headers=_H1)
        assert r.status_code == 200  # endpoint still succeeds

        # Should have retried 3 times (MAX_RETRIES)
        assert mock_client.post.call_count == 3
        # Should have slept between retries (2 sleeps for 3 attempts)
        assert mock_sleep.call_count == 2


def test_webhook_retry_on_network_error(webhook_client: TestClient) -> None:
    """Webhook retries on httpx.HTTPError."""
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.services.webhook.time.sleep") as mock_sleep:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        MockClientClass.return_value = mock_client

        r = webhook_client.get("/v1/policy/decision/agent-err", headers=_H1)
        assert r.status_code == 200  # endpoint still succeeds

        assert mock_client.post.call_count == 3
        assert mock_sleep.call_count == 2


def test_webhook_succeeds_on_second_try(webhook_client: TestClient) -> None:
    """Webhook succeeds on retry after initial failure."""
    with patch("app.services.webhook.httpx.Client") as MockClientClass, \
         patch("app.services.webhook.time.sleep"):
        mock_client = MagicMock()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_client.post.side_effect = [fail_resp, ok_resp]
        MockClientClass.return_value = mock_client

        r = webhook_client.get("/v1/policy/decision/agent-recover", headers=_H1)
        assert r.status_code == 200

        # Only 2 attempts needed (fail then succeed)
        assert mock_client.post.call_count == 2
