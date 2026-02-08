"""
Integration tests for Agent Trust Bureau API.
"""
import pytest
import time


class TestEventsEndpoint:
    """Tests for POST /events endpoint."""
    
    def test_ingest_events_success(self, client, valid_api_key):
        """Successfully ingest events with valid API key."""
        events = [
            {
                "rater_id": "marketplace_1",
                "subject_id": "https://agent.example.com",
                "capability": "python-scripting",
                "outcome": 1,
                "cost_usd": 0.05
            },
            {
                "rater_id": "marketplace_1",
                "subject_id": "https://agent.example.com",
                "capability": "python-scripting",
                "outcome": 1,
                "cost_usd": 0.03
            }
        ]
        
        response = client.post(
            "/v1/events",
            json=events,
            headers={"X-API-Key": valid_api_key}
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["message"] == "Ingested 2 events"
        assert "https://agent.example.com" in data["agents_affected"]
        assert data["scores_updating"] is True
    
    def test_ingest_events_missing_api_key(self, client):
        """Reject events without API key."""
        events = [
            {
                "rater_id": "marketplace_1",
                "subject_id": "https://agent.example.com",
                "capability": "python-scripting",
                "outcome": 1
            }
        ]
        
        response = client.post("/v1/events", json=events)
        
        assert response.status_code == 401
        assert "Missing API key" in response.json()["detail"]
    
    def test_ingest_events_invalid_api_key(self, client, invalid_api_key):
        """Reject events with invalid API key."""
        events = [
            {
                "rater_id": "marketplace_1",
                "subject_id": "https://agent.example.com",
                "capability": "python-scripting",
                "outcome": 1
            }
        ]
        
        response = client.post(
            "/v1/events",
            json=events,
            headers={"X-API-Key": invalid_api_key}
        )
        
        assert response.status_code == 403
        assert "Invalid API key" in response.json()["detail"]
    
    def test_ingest_events_validation_error(self, client, valid_api_key):
        """Reject events with invalid data."""
        events = [
            {
                "rater_id": "marketplace_1",
                "subject_id": "https://agent.example.com",
                "capability": "python-scripting",
                "outcome": 5  # Invalid: must be 0 or 1
            }
        ]
        
        response = client.post(
            "/v1/events",
            json=events,
            headers={"X-API-Key": valid_api_key}
        )
        
        assert response.status_code == 422  # Validation error
    
    def test_ingest_empty_events(self, client, valid_api_key):
        """Handle empty events list."""
        response = client.post(
            "/v1/events",
            json=[],
            headers={"X-API-Key": valid_api_key}
        )
        
        assert response.status_code == 201
        assert response.json()["message"] == "Ingested 0 events"


class TestScoresEndpoint:
    """Tests for GET /scores/{agent_id} endpoint."""
    
    def test_get_score_not_found(self, client):
        """Return zero score for unknown agent."""
        response = client.get("/v1/scores/unknown-agent-12345")
        
        assert response.status_code == 200
        data = response.json()
        assert data["trust_score"] == 0.0
        assert data["confidence"] == "none"
    
    def test_get_score_after_events(self, client, valid_api_key):
        """Return calculated score after events ingestion."""
        # First, ingest some events
        events = [
            {"rater_id": "m1", "subject_id": "agent-1", "capability": "coding", "outcome": 1},
            {"rater_id": "m1", "subject_id": "agent-1", "capability": "coding", "outcome": 1},
            {"rater_id": "m1", "subject_id": "agent-1", "capability": "coding", "outcome": 1},
            {"rater_id": "m1", "subject_id": "agent-1", "capability": "coding", "outcome": 0},
        ]
        
        client.post("/v1/events", json=events, headers={"X-API-Key": valid_api_key})
        
        # Wait a moment for background task
        time.sleep(0.5)
        
        # Check score
        response = client.get("/v1/scores/agent-1")
        
        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "agent-1"
        assert data["trust_score"] > 0
        assert data["confidence"] == "low"  # Only 4 events


class TestBadgeEndpoint:
    """Tests for GET /badge/{agent_id} endpoint."""
    
    def test_get_badge_unverified(self, client):
        """Return unverified badge for unknown agent."""
        response = client.get("/v1/badge/unknown-agent")
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert b"unverified" in response.content
    
    def test_get_badge_with_score(self, client, valid_api_key):
        """Return score badge after events."""
        # Ingest events for high score
        events = [
            {"rater_id": "m1", "subject_id": "good-agent", "capability": "c", "outcome": 1}
            for _ in range(20)
        ]
        
        client.post("/v1/events", json=events, headers={"X-API-Key": valid_api_key})
        time.sleep(0.5)
        
        response = client.get("/v1/badge/good-agent")
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert b"%" in response.content  # Should contain percentage


class TestDashboard:
    """Tests for dashboard endpoints."""
    
    def test_dashboard_html(self, client):
        """Dashboard returns HTML."""
        response = client.get("/dashboard")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
    
    def test_dashboard_stats(self, client):
        """Dashboard stats returns JSON."""
        response = client.get("/dashboard/stats")
        
        assert response.status_code == 200
        data = response.json()
        assert "stats" in data
        assert "total_events" in data["stats"]
        assert "total_agents" in data["stats"]


class TestHealthCheck:
    """Tests for health endpoint."""
    
    def test_health_check(self, client):
        """Health check returns ok."""
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "Agent Trust Bureau"
