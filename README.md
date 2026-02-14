# Agent Trust Bureau

Trust-scoring and policy layer for AI agents. Ingests behavior events, computes explainable trust scores, and exposes them via API.

## Quick Start

```bash
make setup          # creates venv, installs deps, copies .env
```

### Database

Requires PostgreSQL. Default connection string in `.env`:

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/agent_trust_bureau
```

Create the database and run migrations:

```bash
createdb agent_trust_bureau   # or via psql
make migrate                  # runs alembic upgrade head
```

### Run

```bash
make dev    # uvicorn with --reload on port 8010
make run    # production mode (no reload)
```

- API docs: http://127.0.0.1:8010/docs
- Health: http://127.0.0.1:8010/health

### Tests

Tests use an in-memory SQLite database — no Postgres required.

```bash
make test
```

## API Surface (v1)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/intake/events` | Ingest a behavior event |
| GET | `/v1/intake/events/{agent_id}` | List events for an agent |
| GET | `/v1/trust/score/{agent_id}` | Compute and persist trust score |

## Database Tables

- **events** — raw behavior events ingested via API
- **score_history** — snapshots of computed scores (written on each score request)

Migrations are managed with Alembic. After model changes:

```bash
alembic revision --autogenerate -m "describe change"
make migrate
```

## Project Structure

```
app/
  main.py              # FastAPI app + startup
  config.py            # Settings from env vars
  db.py                # Engine, session, init_db
  models.py            # SQLAlchemy models (EventRecord, ScoreSnapshot)
  schemas.py           # Pydantic request/response models
  store.py             # DB queries (insert, list, save snapshot)
  routers/
    events.py          # /v1/intake/* routes
    trust.py           # /v1/trust/* routes
  services/
    scoring.py         # Trust score computation
alembic/               # Migration config and versions
tests/                 # pytest suite
```

## Scoring Model

Baseline score is 50. Events shift it up or down by fixed weights:

| Event Type | Weight |
|------------|--------|
| human_approved_action | +6 |
| task_completed_without_rework | +4 |
| policy_compliant_response | +3 |
| safe_tool_usage | +2 |
| hallucination_detected | -8 |
| manual_rollback_required | -10 |
| unsafe_tool_call | -15 |
| policy_violation | -20 |
| sensitive_data_leak_attempt | -25 |

Score is clamped to [0, 100]. Tiers: high (>=80), medium (>=60), watch (>=40), restricted (<40).

## Next Steps

1. Auth + API keys for tenant isolation
2. Async score computation (background worker)
3. Policy webhooks (allow/review/block thresholds)
4. Score drift alerting and dashboard
