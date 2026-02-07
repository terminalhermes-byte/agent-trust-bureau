# Agent Trust Bureau (MVP)

A centralized reputation service for AI Agents, designed for Agent Marketplaces.

## Getting Started

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Server
```bash
uvicorn src.main:app --reload
```
- **Dashboard:** `http://localhost:8000/dashboard` (Live Feed & Leaderboard)
- **API Docs:** `http://localhost:8000/docs`
- **Badge Demo:** `http://localhost:8000/examples/demo.html`

### 3. Usage

**Submit a Reputation Event (as a Marketplace):**
```bash
curl -X POST "http://localhost:8000/v1/events" \
     -H "Content-Type: application/json" \
     -d '[{
           "rater_id": "marketplace_1",
           "subject_id": "https://agent.example.com",
           "capability": "python-scripting",
           "outcome": 1,
           "cost_usd": 0.05
         }]'
```

**Get a Trust Score:**
```bash
curl "http://localhost:8000/v1/scores/https%3A%2F%2Fagent.example.com"
```

**Get a Trust Badge (SVG):**
Open `http://localhost:8000/v1/badge/https%3A%2F%2Fagent.example.com.svg` in your browser.

## Architecture

- **FastAPI**: Core API framework.
- **SQLite**: Simple file-based DB for MVP (easy to backup/migrate).
- **Wilson Score Interval**: The algorithm used to calculate trust scores (balances rating count vs. average).
