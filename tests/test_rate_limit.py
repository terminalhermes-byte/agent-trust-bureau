from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import get_db
from app.main import app
from app.models import Base, Tenant
from app.rate_limit import reset_rate_limits


@pytest.fixture
def limited_client() -> TestClient:
    """Client with a very low rate limit for easy testing."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    # Seed default tenant
    db = testing_session()
    db.add(Tenant(id=1, name="Default", slug="default", is_active=True))
    db.commit()
    db.close()

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    original_limit = settings.score_rate_limit_per_minute
    original_auth = settings.require_auth
    settings.score_rate_limit_per_minute = 3
    settings.require_auth = False

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        settings.score_rate_limit_per_minute = original_limit
        settings.require_auth = original_auth
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()


def test_score_rate_limit_returns_429(limited_client: TestClient) -> None:
    agent_id = "agent-rl"

    # First 3 requests should succeed (limit=3)
    for i in range(3):
        response = limited_client.get(f"/v1/trust/score/{agent_id}")
        assert response.status_code == 200, f"Request {i+1} should succeed"

    # 4th request should be rate-limited
    response = limited_client.get(f"/v1/trust/score/{agent_id}")
    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]


def test_rate_limit_is_per_agent(limited_client: TestClient) -> None:
    # Exhaust limit for agent-a
    for _ in range(3):
        limited_client.get("/v1/trust/score/agent-a")

    # agent-a is now limited
    response = limited_client.get("/v1/trust/score/agent-a")
    assert response.status_code == 429

    # agent-b should still work
    response = limited_client.get("/v1/trust/score/agent-b")
    assert response.status_code == 200


def test_history_endpoint_not_rate_limited(limited_client: TestClient) -> None:
    """The history endpoint should not be affected by score rate limits."""
    # Exhaust score limit
    for _ in range(3):
        limited_client.get("/v1/trust/score/agent-hist-rl")

    # Score is limited
    response = limited_client.get("/v1/trust/score/agent-hist-rl")
    assert response.status_code == 429

    # History is not
    response = limited_client.get("/v1/trust/score/agent-hist-rl/history")
    assert response.status_code == 200


def test_rate_limit_disabled_when_zero(client: TestClient) -> None:
    """Default client has limit=30 which is plenty, but test with 0."""
    original = settings.score_rate_limit_per_minute
    settings.score_rate_limit_per_minute = 0
    reset_rate_limits()
    try:
        # Should never 429 with limit=0
        for _ in range(50):
            response = client.get("/v1/trust/score/agent-unlimited")
            assert response.status_code == 200
    finally:
        settings.score_rate_limit_per_minute = original
        reset_rate_limits()
