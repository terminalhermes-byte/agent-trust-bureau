from __future__ import annotations

from fastapi.testclient import TestClient


def _ingest_event(client: TestClient, agent_id: str, event_id: str, event_type: str = "safe_tool_usage") -> None:
    client.post(
        "/v1/intake/events",
        json={
            "event_id": event_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "source": "test-suite",
            "occurred_at": "2026-02-13T12:00:00Z",
            "metadata": {},
        },
    )


def test_history_returns_empty_for_unknown_agent(client: TestClient) -> None:
    response = client.get("/v1/trust/score/agent-ghost/history")
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == "agent-ghost"
    assert body["count"] == 0
    assert body["snapshots"] == []


def test_history_returns_snapshots_in_desc_order(client: TestClient) -> None:
    _ingest_event(client, "agent-hist", "evt-h1")

    # Generate 3 score snapshots by calling the score endpoint 3 times
    for _ in range(3):
        client.get("/v1/trust/score/agent-hist")

    response = client.get("/v1/trust/score/agent-hist/history")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3

    # All should be the same score
    for snap in body["snapshots"]:
        assert snap["agent_id"] == "agent-hist"
        assert snap["score"] == 52.0

    # Verify desc ordering by computed_at
    timestamps = [s["computed_at"] for s in body["snapshots"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_history_respects_limit_param(client: TestClient) -> None:
    _ingest_event(client, "agent-lim", "evt-l1")

    for _ in range(5):
        client.get("/v1/trust/score/agent-lim")

    response = client.get("/v1/trust/score/agent-lim/history?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2


def test_history_limit_validation(client: TestClient) -> None:
    # limit=0 should fail validation
    response = client.get("/v1/trust/score/agent-x/history?limit=0")
    assert response.status_code == 422

    # limit=501 should fail validation
    response = client.get("/v1/trust/score/agent-x/history?limit=501")
    assert response.status_code == 422


def test_history_before_filter(client: TestClient) -> None:
    _ingest_event(client, "agent-bf", "evt-bf1")

    # Generate snapshots
    client.get("/v1/trust/score/agent-bf")
    client.get("/v1/trust/score/agent-bf")

    # Get all history to find timestamps
    all_response = client.get("/v1/trust/score/agent-bf/history")
    all_body = all_response.json()
    assert all_body["count"] == 2

    # Use the earliest snapshot's timestamp as "before" — should return 0 results
    # since both snapshots are at or after that time
    # Instead, use a timestamp far in the past
    response = client.get("/v1/trust/score/agent-bf/history?before=2020-01-01T00:00:00Z")
    body = response.json()
    assert body["count"] == 0
