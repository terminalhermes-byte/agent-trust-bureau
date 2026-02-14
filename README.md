# Agent Trust Bureau

Trust-scoring and policy layer for AI agents. Ingests behavior events, computes explainable trust scores, and exposes them via a multi-tenant API.

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

### Bootstrap a Tenant and API Key

Before making authenticated requests you need at least one tenant and one API key:

```bash
make bootstrap    # creates "default" tenant + prints a one-time API key
```

Save the key that is printed — it is hashed before storage and **cannot be retrieved later**.

To generate additional keys for an existing tenant:

```bash
make create-key TENANT=default NAME=my-service
```

### Run

```bash
make devup  # migrate + uvicorn with --reload on port 8010
make dev    # uvicorn with --reload (skip migration)
make run    # production mode (no reload)
```

- API docs: http://127.0.0.1:8010/docs
- Health: http://127.0.0.1:8010/health

### Tests

Tests use an in-memory SQLite database — no Postgres required.

```bash
make test
```

## Authentication

All `/v1` routes require an `X-API-Key` header when `REQUIRE_AUTH=true`.

API keys are stored as SHA-256 hashes in the database. Each key is bound to a tenant, and the resolved tenant scopes all data access for that request.

```bash
# Enable auth in .env
REQUIRE_AUTH=true
```

```bash
# Example authenticated request
curl -H "X-API-Key: atb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
     http://127.0.0.1:8010/v1/trust/score/agent-1
```

When `REQUIRE_AUTH=false` (default), auth is disabled and all requests run as tenant_id=1 ("default").

`/health`, `/docs`, and `/` are always accessible without a key.

### Key Format

Generated keys follow the pattern `atb_<32 random hex chars>`. The first 12 characters (`atb_<8 hex>`) are stored as a prefix for efficient DB lookup; the full key is verified via constant-time hash comparison.

### Revoking Keys

Keys can be revoked by setting `revoked_at` on the `api_keys` row. A revoked key returns `401 Unauthorized`.

## Multi-Tenancy

All data (events, scores, score history) is scoped to a tenant via `tenant_id`. Tenant isolation is enforced at the query layer — a tenant can never read or write another tenant's data.

Key behaviors:
- The same `agent_id` can exist independently under different tenants
- `event_id` uniqueness is per-tenant, not global
- Score computation only considers a tenant's own events
- Score history only includes a tenant's own snapshots

## Rate Limiting

The score endpoint (`GET /v1/trust/score/{agent_id}`) is rate-limited per agent_id.

```bash
# In .env — max requests per agent per minute (default 30)
SCORE_RATE_LIMIT_PER_MINUTE=30
```

Returns `429 Too Many Requests` when exceeded. Set to `0` to disable.

## API Surface (v1)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/intake/events` | Ingest a behavior event |
| GET | `/v1/intake/events/{agent_id}` | List events for an agent |
| GET | `/v1/trust/score/{agent_id}` | Compute, persist, and return trust score |
| GET | `/v1/trust/score/{agent_id}/history` | Score history (supports `?limit=` and `?before=`) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://...` | PostgreSQL connection string |
| `REQUIRE_AUTH` | `false` | Enable API key authentication; `true` for production |
| `SCORE_RATE_LIMIT_PER_MINUTE` | `30` | Max score requests per agent per minute; 0 = disabled |
| `ENVIRONMENT` | `development` | Environment name |
| `DB_ECHO` | `false` | Log SQL statements |
| `AUTO_CREATE_TABLES` | `false` | Create tables on startup (use migrations instead) |

## Database Tables

| Table | Description |
|-------|-------------|
| **tenants** | Registered tenants (id, name, slug, is_active) |
| **api_keys** | Hashed API keys bound to a tenant (prefix lookup, SHA-256 hash, revokable) |
| **events** | Raw behavior events scoped to a tenant + agent |
| **score_history** | Computed score snapshots scoped to a tenant + agent |

Migrations are managed with Alembic. After model changes:

```bash
alembic revision --autogenerate -m "describe change"
make migrate
```

## Project Structure

```
app/
  main.py              # FastAPI app + lifespan
  config.py            # Settings from env vars
  auth.py              # DB-backed API key auth (SHA-256, constant-time compare)
  rate_limit.py        # Per-agent sliding window rate limiter
  cli.py               # Bootstrap CLI (create tenant, generate keys)
  db.py                # Engine, session, init_db
  models.py            # SQLAlchemy models (Tenant, ApiKey, EventRecord, ScoreSnapshot)
  schemas.py           # Pydantic request/response models
  store.py             # DB queries (all tenant-scoped)
  routers/
    events.py          # /v1/intake/* routes
    trust.py           # /v1/trust/* routes
  services/
    scoring.py         # Trust score computation
alembic/               # Migration config and versions
tests/                 # pytest suite (33 tests)
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

## Upgrading from v0.2

v0.3 replaces the env-var `API_KEYS` with DB-backed authentication:

1. Remove `API_KEYS` from your `.env`
2. Add `REQUIRE_AUTH=true` to `.env`
3. Run `make migrate` to create the `tenants` and `api_keys` tables (existing data is migrated to a "default" tenant)
4. Run `make bootstrap` to generate your first API key
5. Update clients to use the new key

## Next Steps

1. Async score computation (background worker)
2. Policy webhooks (allow/review/block thresholds)
3. Score drift alerting and dashboard
4. Tenant management admin API
