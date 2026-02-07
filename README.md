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

### 4. Deploy (Free Tier)
**Option A: Render (Zero Config)**
1. Fork this repo to your GitHub.
2. Sign up at [render.com](https://render.com).
3. Click **New > Web Service**.
4. Connect your repo.
5. In **Root Directory**, keep blank.
6. In **Build Command**, use: `pip install -r requirements.txt`
7. In **Start Command**, use: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
8. (Optional) Add a Disk at `/data` if you want persistent storage.

**Option B: Fly.io (CLI)**
1. Install flyctl: `curl -L https://fly.io/install.sh | sh`
2. `fly launch` (Use existing Dockerfile)
3. `fly deploy`

## Architecture

- **FastAPI**: Core API framework.
- **SQLite**: Simple file-based DB for MVP (easy to backup/migrate).
- **Wilson Score Interval**: The algorithm used to calculate trust scores (balances rating count vs. average).
