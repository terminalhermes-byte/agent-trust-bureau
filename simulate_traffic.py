from fastapi.testclient import TestClient
from src.main import app
from src.database import init_db, get_db, SessionLocal
import sys

# Initialize DB
init_db()

client = TestClient(app)

def simulate():
    agent_id = "https://agent.example.com"
    
    print(f"--- Simulating Traffic for {agent_id} ---")
    
    # 1. Check initial score (should be 0 or empty)
    resp = client.get(f"/v1/scores/{agent_id}")
    print(f"Initial Score: {resp.json()}")

    # 2. Ingest 50 Successes (Marketplace A reporting)
    print("\n>>> Ingesting 50 SUCCESS events from Marketplace A...")
    events = []
    for i in range(50):
        events.append({
            "rater_id": "marketplace_A",
            "subject_id": agent_id,
            "capability": "coding",
            "outcome": 1,
            "cost_usd": 0.05
        })
    
    resp = client.post("/v1/events", json=events)
    print(f"Ingest Status: {resp.status_code} {resp.json()}")

    # 3. Check Score (Should be high, maybe max confidence?)
    resp = client.get(f"/v1/scores/{agent_id}")
    data = resp.json()
    print(f"Score after 50 successes: {data['trust_score']:.2f}% ({data['confidence']} confidence)")

    # 4. Ingest 10 Failures (Marketplace B reporting)
    print("\n>>> Ingesting 10 FAILURE events from Marketplace B...")
    events = []
    for i in range(10):
        events.append({
            "rater_id": "marketplace_B",
            "subject_id": agent_id,
            "capability": "coding",
            "outcome": 0,
            "cost_usd": 0.05
        })
    
    resp = client.post("/v1/events", json=events)
    print(f"Ingest Status: {resp.status_code} {resp.json()}")

    # 5. Check Final Score
    resp = client.get(f"/v1/scores/{agent_id}")
    data = resp.json()
    print(f"Final Score: {data['trust_score']:.2f}% ({data['confidence']} confidence)")

    # 6. Check Badge URL
    print(f"\nBadge URL: http://localhost:8000/v1/badge/{agent_id}.svg")

if __name__ == "__main__":
    simulate()
