# Agent Trust Bureau

Trust-scoring and policy layer for AI agents. Ingests behavior events, computes explainable trust scores, evaluates policy decisions, and exposes them via a multi-tenant API with admin CRUD and optional webhook notifications.

## Quick Start

### Local (venv)

```bash
make setup          # creates venv, installs deps, copies .env
```

Requires PostgreSQL. Default connection string in `.env`:

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/agent_trust_bureau
```

Create the database and run migrations:

```bash
createdb agent_trust_bureau   # or via psql
make migrate                  # runs alembic upgrade head
```

### Docker

```bash
docker compose up --build
```

Starts PostgreSQL 16 and the app on port 8010. Migrations run automatically on startup.

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

CI runs automatically on push and PR via GitHub Actions (Postgres service container, migrations, full pytest).

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

Generated keys follow the pattern `atb_<random chars>`. The first 12 characters are stored as a prefix for efficient DB lookup; the full key is verified via constant-time hash comparison.

### Revoking Keys

Keys can be revoked by setting `revoked_at` on the `api_keys` row. A revoked key returns `401 Unauthorized`.

## Multi-Tenancy

All data (events, scores, score history, policy configs, webhooks) is scoped to a tenant via `tenant_id`. Tenant isolation is enforced at the query layer — a tenant can never read or write another tenant's data.

Key behaviors:
- The same `agent_id` can exist independently under different tenants
- `event_id` uniqueness is per-tenant, not global
- Score computation only considers a tenant's own events
- Policy thresholds and webhooks are per-tenant with optional per-agent overrides
- Admin endpoints operate only on the calling tenant's data

## Policy Layer

The policy layer evaluates trust scores against configurable thresholds and returns structured decisions.

### Policy Decision Endpoint

```bash
curl -H "X-API-Key: $KEY" http://127.0.0.1:8010/v1/policy/decision/agent-1
```

Response:

```json
{
  "agent_id": "agent-1",
  "decision": "review",
  "score": 50.0,
  "thresholds": {
    "allow": 80.0,
    "review": 60.0,
    "block": 40.0
  },
  "explanation": "Score 50.0 >= block threshold 40.0 but < review threshold 60.0"
}
```

### Decision Logic

| Score Range | Decision |
|-------------|----------|
| `>= allow_threshold` | **allow** |
| `>= review_threshold` | **review** |
| `>= block_threshold` | **review** |
| `< block_threshold` | **block** |

Default thresholds: allow=80, review=60, block=40. Each tenant gets its own `policy_configs` row (seeded on migration).

### Webhooks

Each tenant can optionally configure a webhook via the admin API. When a policy decision is computed:

1. A JSON payload is POSTed to the webhook URL
2. The payload is signed with `HMAC-SHA256` using the webhook secret
3. The signature is sent in the `X-ATB-Signature` header
4. Failed deliveries are retried up to 3 times with exponential backoff

Webhook delivery is best-effort — a failed webhook does not block the API response.

### Async Webhook Worker (v0.7)

For production you can move webhook delivery off the request path using a DB-backed queue and a separate worker process.

- Set `WEBHOOK_ASYNC=true` on the web service to enqueue delivery jobs instead of sending inline.
- Run the worker process (`python -m app.worker`) to claim pending jobs, deliver webhooks, and record attempts.

Local dev (2 terminals):

```bash
make devup
WEBHOOK_ASYNC=true make worker
```

Render:

- `render.yaml` defines both:
  - a `web` service (the API)
  - a `worker` service (`scripts/start-worker.sh`) that runs migrations then starts `python -m app.worker`

### Webhook Delivery Logs

Every webhook attempt (including retries) is persisted for operator visibility.

- Endpoint: `GET /v1/admin/policy/webhooks/{id}/deliveries?limit=50`

## Admin API

Tenant-scoped CRUD for policy configuration, agent overrides, and webhooks. All endpoints require auth and operate only on the calling tenant's data. Secrets are never returned in full — only the last 4 characters are shown.

### Policy Config

```bash
# Get current thresholds
curl -H "X-API-Key: $KEY" http://127.0.0.1:8010/v1/admin/policy/config

# Update thresholds (allow >= review >= block, all 0-100)
curl -X PUT -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"allow_threshold": 85, "review_threshold": 65, "block_threshold": 45}' \
  http://127.0.0.1:8010/v1/admin/policy/config
```

### Agent Overrides

Per-agent threshold overrides. Threshold fields are optional — `null` values fall back to the tenant default.

```bash
# Create override
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"agent_id": "agent-1", "allow_threshold": 90}' \
  http://127.0.0.1:8010/v1/admin/policy/overrides

# List overrides
curl -H "X-API-Key: $KEY" http://127.0.0.1:8010/v1/admin/policy/overrides

# Delete override
curl -X DELETE -H "X-API-Key: $KEY" \
  http://127.0.0.1:8010/v1/admin/policy/overrides/agent-1
```

### Webhooks

Webhook secrets are generated server-side and returned exactly once on creation or rotation. List and non-rotation updates only include masked `secret_last4`.

```bash
# Create webhook (secret returned once)
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://hooks.example.com/policy"}' \
  http://127.0.0.1:8010/v1/admin/policy/webhooks

# Rotate secret (secret returned once)
curl -X PATCH -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"rotate_secret": true}' \
  http://127.0.0.1:8010/v1/admin/policy/webhooks/1

# Disable/enable
curl -X PATCH -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  http://127.0.0.1:8010/v1/admin/policy/webhooks/1

# List webhooks (secrets masked — only last 4 chars shown)
curl -H "X-API-Key: $KEY" http://127.0.0.1:8010/v1/admin/policy/webhooks
```

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
| GET | `/v1/trust/score/{agent_id}/history` | Score history (`?limit=`, `?before=`) |
| GET | `/v1/policy/decision/{agent_id}` | Evaluate policy decision (score + thresholds) |
| GET | `/v1/admin/policy/config` | Get tenant policy thresholds |
| PUT | `/v1/admin/policy/config` | Update tenant policy thresholds |
| POST | `/v1/admin/policy/overrides` | Create agent override |
| DELETE | `/v1/admin/policy/overrides/{agent_id}` | Delete agent override |
| GET | `/v1/admin/policy/overrides` | List agent overrides (`?limit=`) |
| POST | `/v1/admin/policy/webhooks` | Create webhook |
| PATCH | `/v1/admin/policy/webhooks/{id}` | Update webhook (enable/disable, rotate secret) |
| GET | `/v1/admin/policy/webhooks` | List webhooks |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://...` | PostgreSQL connection string |
| `REQUIRE_AUTH` | `false` | Enable API key authentication; `true` for production |
| `SCORE_RATE_LIMIT_PER_MINUTE` | `30` | Max score requests per agent per minute; 0 = disabled |
| `ENVIRONMENT` | `development` | Environment name |
| `DB_ECHO` | `false` | Log SQL statements |
| `AUTO_CREATE_TABLES` | `false` | Create tables on startup (use migrations instead) |
| `PORT` | `8010` | Port for uvicorn (used by Docker / deploy scripts) |

## Database Tables

| Table | Description |
|-------|-------------|
| **tenants** | Registered tenants (id, name, slug, is_active) |
| **api_keys** | Hashed API keys bound to a tenant |
| **events** | Raw behavior events scoped to a tenant + agent |
| **score_history** | Computed score snapshots scoped to a tenant + agent |
| **policy_configs** | Per-tenant policy thresholds (allow, review, block) |
| **agent_policy_overrides** | Optional per-agent threshold overrides |
| **policy_webhooks** | Optional webhook URLs with HMAC secrets |

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
  models.py            # SQLAlchemy models (7 tables)
  schemas.py           # Pydantic request/response models
  store.py             # DB queries (events, scores — all tenant-scoped)
  admin_store.py       # DB queries (policy config, overrides, webhooks)
  routers/
    events.py          # /v1/intake/* routes
    trust.py           # /v1/trust/* routes
    policy.py          # /v1/policy/* routes
    admin.py           # /v1/admin/* CRUD routes
  services/
    scoring.py         # Trust score computation
    policy.py          # Policy evaluation engine (thresholds + overrides)
    webhook.py         # HMAC-signed webhook delivery with retries
scripts/
  start.sh             # Startup script (migrate + uvicorn)
alembic/               # Migration config and versions
tests/                 # pytest suite (83 tests)
.github/workflows/
  ci.yml               # GitHub Actions CI (Postgres, migrations, pytest)
Dockerfile             # Production container image
docker-compose.yml     # Local dev stack (Postgres + app)
render.yaml            # Render deployment blueprint
fly.toml               # Fly.io deployment config
```

## Deployment

### Docker Compose (local / self-hosted)

```bash
docker compose up --build
```

The app container runs `scripts/start.sh` which executes `alembic upgrade head` before starting uvicorn. PostgreSQL 16 is started as a sidecar with a health check.

After the stack is up, bootstrap your first tenant and key:

```bash
docker compose exec app python -m app.cli bootstrap
```

### Render

Push your repo and Render will auto-detect `render.yaml`:

1. Creates a managed PostgreSQL instance
2. Builds the Docker image
3. Sets `DATABASE_URL` automatically from the database
4. Runs migrations on every deploy via the startup script
5. Health checks hit `/health`

Set `REQUIRE_AUTH=true` in the Render dashboard (already in `render.yaml`), then bootstrap a key via the Render shell.

### Fly.io

```bash
fly launch          # uses fly.toml
fly postgres create # create a managed Postgres
fly secrets set DATABASE_URL="postgres://..."
fly deploy
```

Migrations run on every deploy via `scripts/start.sh`. After first deploy:

```bash
fly ssh console -C "python -m app.cli bootstrap"
```

## CI

GitHub Actions runs on every push and pull request:

- Spins up a Postgres 16 service container
- Installs Python 3.12 with pip caching
- Runs `alembic upgrade head` against the real database
- Runs the full pytest suite

See `.github/workflows/ci.yml`.

### Running CI locally

Tests use in-memory SQLite, so no Postgres is needed locally:

```bash
make test                    # or: .venv/bin/python -m pytest -v
```

To replicate the full CI pipeline locally (with Postgres):

```bash
# Start Postgres (e.g. via Docker)
docker run -d --name atb-pg -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=agent_trust_bureau_test postgres:16-alpine

# Run migrations + tests against Postgres
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/agent_trust_bureau_test \
  alembic upgrade head
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/agent_trust_bureau_test \
  python -m pytest -v
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

## Upgrade Notes

### v0.4 → v0.5

No database migrations. Adds admin CRUD endpoints and GitHub Actions CI. No breaking changes.

### v0.3 → v0.4

Run `make migrate` to create the `policy_configs`, `agent_policy_overrides`, and `policy_webhooks` tables. Existing tenants are seeded with default thresholds (allow=80, review=60, block=40). No env-var changes required.

### v0.2 → v0.3

Replaces `API_KEYS` env var with DB-backed auth. See migration 0003 for details.

## Next Steps

1. Async score computation (background worker)
2. Score drift alerting and dashboard
3. Tenant management admin API (create/deactivate tenants via REST)
4. Admin role separation (admin keys vs read-only keys)
