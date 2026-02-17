# Agent Trust Bureau v1.0.0 — Pilot Quickstart

Get ATB running and verified in under 10 minutes.

## Prerequisites

- Docker and Docker Compose **OR** Python 3.11+ with PostgreSQL
- `curl` and `jq` installed
- A Render account (for production) or local dev environment

## Option A: Docker Compose (recommended for first run)

```bash
git clone https://github.com/jerbey4/agent-trust-bureau.git
cd agent-trust-bureau
docker compose up --build -d
```

Wait ~15 seconds for Postgres health check, then bootstrap:

```bash
docker compose exec app python -m app.cli bootstrap
```

Save the printed API key — it cannot be retrieved later.

```bash
export BASE="http://127.0.0.1:8010"
export KEY="atb_<paste-your-key-here>"
```

## Option B: Local venv

```bash
git clone https://github.com/jerbey4/agent-trust-bureau.git
cd agent-trust-bureau
make setup          # creates venv, installs deps
createdb agent_trust_bureau
make migrate        # runs alembic upgrade head
make bootstrap      # creates default tenant + API key
make devup          # starts uvicorn on port 8010
```

```bash
export BASE="http://127.0.0.1:8010"
export KEY="atb_<paste-your-key-here>"
```

## 10-Minute Verification Flow

Copy-paste each block. Every command should succeed.

### 1. Health check

```bash
curl -sS "$BASE/health" | jq .
# Expected: {"status": "ok"}
```

### 2. Ingest an event

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
    "event_id": "evt-001",
    "agent_id": "agent-pilot",
    "event_type": "task_completed_without_rework",
    "source": "quickstart",
    "occurred_at": "2025-01-01T00:00:00Z"
  }' "$BASE/v1/intake/events" | jq .
# Expected: {"accepted": true, "event_id": "evt-001", "agent_id": "agent-pilot"}
```

### 3. Compute trust score

```bash
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/trust/score/agent-pilot" | jq .
# Expected: score ~54.0, tier "medium"
```

### 4. Get policy decision

```bash
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/policy/decision/agent-pilot" | jq .
# Expected: decision "review", score ~54.0
```

### 5. Configure a webhook

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://httpbin.org/post"}' \
  "$BASE/v1/admin/policy/webhooks" | jq .
# Expected: id, url, secret (save the secret!), enabled: true
```

### 6. Create an additional API key

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name": "ci-service"}' "$BASE/v1/admin/keys" | jq .
# Expected: id, name "ci-service", raw_key (save it!)
```

### 7. List keys (raw key never shown)

```bash
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/keys" | jq .
# Expected: count >= 2, no raw_key in list
```

### 8. Check webhook queue stats

```bash
WH_ID=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks" | jq '.webhooks[0].id')
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/stats" | jq .
# Expected: pending/completed/failed/dead counts + recent_success_rate
```

### 9. Check jobs and deliveries

```bash
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/jobs?limit=5" | jq .
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/deliveries?limit=5" | jq .
```

### 10. Run cleanup dry-run

```bash
# Docker:
docker compose exec app python -m app.cli cleanup --days 30 --dry-run

# Local:
python -m app.cli cleanup --days 30 --dry-run
```

## Automated Smoke Test

Run the full smoke test in one command:

```bash
./scripts/v1_smoke.sh "$BASE" "$KEY"
```

Exits 0 on success, non-zero on failure.

## Next Steps

- **Production deploy**: See `render.yaml` Blueprint or `fly.toml`
- **Ops runbook**: See `docs/V1_RUNBOOK.md`
- **Full API reference**: See `$BASE/docs` (interactive Swagger UI)
- **Changelog**: See `CHANGELOG.md`
