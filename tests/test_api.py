from __future__ import annotations

from fastapi.testclient import TestClient


def test_ingest_and_fetch_events(client: TestClient) -> None:
    payload = {
        "event_id": "evt-001",
        "agent_id": "agent-123",
        "event_type": "safe_tool_usage",
        "source": "test-suite",
        "occurred_at": "2026-02-13T12:00:00Z",
        "metadata": {"tool": "search"},
    }

    ingest_response = client.post("/v1/intake/events", json=payload)
    assert ingest_response.status_code == 200
    assert ingest_response.json() == {"accepted": True, "event_id": "evt-001", "agent_id": "agent-123"}

    fetch_response = client.get("/v1/intake/events/agent-123")
    assert fetch_response.status_code == 200

    body = fetch_response.json()
    assert body["agent_id"] == "agent-123"
    assert body["event_count"] == 1
    assert body["events"][0]["event_id"] == "evt-001"
    assert body["events"][0]["metadata"] == {"tool": "search"}


def test_duplicate_event_id_returns_409(client: TestClient) -> None:
    payload = {
        "event_id": "evt-dup",
        "agent_id": "agent-123",
        "event_type": "safe_tool_usage",
        "source": "test-suite",
        "occurred_at": "2026-02-13T12:05:00Z",
        "metadata": {},
    }

    first = client.post("/v1/intake/events", json=payload)
    assert first.status_code == 200

    second = client.post("/v1/intake/events", json=payload)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


def test_trust_score_aggregates_saved_events(client: TestClient) -> None:
    client.post(
        "/v1/intake/events",
        json={
            "event_id": "evt-positive",
            "agent_id": "agent-score",
            "event_type": "human_approved_action",
            "source": "test-suite",
            "occurred_at": "2026-02-13T12:10:00Z",
            "metadata": {},
        },
    )
    client.post(
        "/v1/intake/events",
        json={
            "event_id": "evt-negative",
            "agent_id": "agent-score",
            "event_type": "hallucination_detected",
            "source": "test-suite",
            "occurred_at": "2026-02-13T12:11:00Z",
            "metadata": {},
        },
    )

    score_response = client.get("/v1/trust/score/agent-score")
    assert score_response.status_code == 200

    body = score_response.json()
    assert body["agent_id"] == "agent-score"
    assert body["trust_score"] == 48.0
    assert body["trust_tier"] == "watch"
    assert body["factors"]["positive_delta"] == 6.0
    assert body["factors"]["negative_delta"] == -8.0


def test_trust_score_for_unknown_agent_returns_baseline(client: TestClient) -> None:
    """An agent with no events should get the baseline score (50.0, watch)."""
    response = client.get("/v1/trust/score/agent-never-seen")
    assert response.status_code == 200

    body = response.json()
    assert body["agent_id"] == "agent-never-seen"
    assert body["trust_score"] == 50.0
    assert body["trust_tier"] == "watch"
    assert body["factors"]["positive_delta"] == 0.0
    assert body["factors"]["negative_delta"] == 0.0


def test_score_endpoint_persists_snapshot(client: TestClient) -> None:
    """Calling the score endpoint should write a row to score_history."""
    from app.models import ScoreSnapshot

    client.post(
        "/v1/intake/events",
        json={
            "event_id": "evt-persist-1",
            "agent_id": "agent-persist",
            "event_type": "safe_tool_usage",
            "source": "test-suite",
            "occurred_at": "2026-02-13T12:20:00Z",
            "metadata": {},
        },
    )

    response = client.get("/v1/trust/score/agent-persist")
    assert response.status_code == 200

    # Verify snapshot was written to DB via a direct DB query
    from sqlalchemy import select
    from app.db import get_db
    from app.main import app

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        snapshots = list(db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.agent_id == "agent-persist")).all())
        assert len(snapshots) == 1
        assert snapshots[0].score == 52.0  # baseline 50 + safe_tool_usage 2
        assert snapshots[0].tier == "watch"
    finally:
        db.close()


def test_negative_heavy_agent_via_api(client: TestClient) -> None:
    """End-to-end: ingest several severe negatives, verify restricted tier via API."""
    for i, event_type in enumerate(["policy_violation", "sensitive_data_leak_attempt", "unsafe_tool_call"]):
        client.post(
            "/v1/intake/events",
            json={
                "event_id": f"evt-neg-{i}",
                "agent_id": "agent-bad-actor",
                "event_type": event_type,
                "source": "test-suite",
                "occurred_at": f"2026-02-13T13:0{i}:00Z",
                "metadata": {},
            },
        )

    response = client.get("/v1/trust/score/agent-bad-actor")
    assert response.status_code == 200

    body = response.json()
    assert body["trust_score"] == 0.0
    assert body["trust_tier"] == "restricted"
