[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/terminalhermes-byte/agent-trust-bureau)

# Agent Trust Bureau (MVP)

A centralized reputation service for AI Agents, designed for Agent Marketplaces.

## Features

- **Trust Scores**: Wilson Score algorithm for fair ranking with low sample sizes
- **SVG Badges**: Embeddable trust badges for agent profiles
- **Live Dashboard**: Real-time feed and leaderboard
- **API Key Auth**: Protected event ingestion endpoint
- **Rate Limiting**: 100 req/min (authenticated), 30 req/min (public)
- **Async Processing**: Background score recomputation

## Getting Started

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API Keys (Optional)
```bash
# Set allowed API keys (comma-separated)
export ATB_API_KEYS="your-marketplace-key-1,your-marketplace-key-2"
```

### 3. Run the Server
```bash
uvicorn src.main:app --reload
```
- **Dashboard:** `http://localhost:8000/dashboard`
- **API Docs:** `http://localhost:8000/docs`
- **Badge Demo:** `http://localhost:8000/examples/demo.html`

### 4. Run Tests
```bash
pytest tests/ -v
```

## API Usage

### Submit Reputation Events (Authenticated)
```bash
curl -X POST "http://localhost:8000/v1/events" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your-api-key" \
     -d '[{
           "rater_id": "marketplace_1",
           "subject_id": "https://agent.example.com",
           "capability": "python-scripting",
           "outcome": 1,
           "cost_usd": 0.05
         }]'
```

### Get Trust Score (Public)
```bash
curl "http://localhost:8000/v1/scores/https%3A%2F%2Fagent.example.com"
```

Response:
```json
{
  "agent_id": "https://agent.example.com",
  "trust_score": 87.5,
  "confidence": "high",
  "breakdown": {
    "python-scripting": {"score": 92.1, "total": 45, "success_rate": 95.5}
  }
}
```

### Get Trust Badge (Public)
```
http://localhost:8000/v1/badge/https%3A%2F%2Fagent.example.com.svg
```

Returns an SVG badge like: ![trust: 87%](https://img.shields.io/badge/trust-87%25-brightgreen)

## Authentication

The `/events` endpoint requires an API key passed via the `X-API-Key` header.

Configure allowed keys via environment variable:
```bash
export ATB_API_KEYS="key1,key2,key3"
```

For production, use a secrets manager and rotate keys regularly.

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| `/events` (authenticated) | 100/minute |
| `/scores`, `/badge`, `/dashboard` | 30/minute |

Rate limits are keyed by API key (authenticated) or IP address (public).

## Deploy

### Render (Zero Config)
1. Fork this repo
2. Click the "Deploy to Render" button above
3. Add `ATB_API_KEYS` environment variable in Render dashboard

### Fly.io
```bash
fly launch
fly secrets set ATB_API_KEYS="your-key-1,your-key-2"
fly deploy
```

### Docker
```bash
docker build -t agent-trust-bureau .
docker run -p 8000:8000 -e ATB_API_KEYS="key1,key2" agent-trust-bureau
```

## Architecture

- **FastAPI**: Core API framework
- **SQLite**: File-based DB for MVP (easy backup/migrate)
- **Wilson Score**: Trust algorithm that handles low sample sizes well
- **slowapi**: Rate limiting
- **BackgroundTasks**: Async score recomputation

## Roadmap

- [ ] Redis backend for distributed rate limiting
- [ ] Webhook notifications for score changes
- [ ] Agent verification/claims
- [ ] Historical score tracking
- [ ] GraphQL API
