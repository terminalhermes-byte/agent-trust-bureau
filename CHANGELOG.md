# Changelog

All notable changes to Agent Trust Bureau are documented here.

## [1.1.0] — 2026-02-17

Second production release focused on reliability hardening, security controls, operator tooling, and architecture planning.

### Highlights

- Reliability fixes for async webhook processing edge cases:
  - Safe exception handling in `process_webhook_job`
  - Proper attempt increment during stale-job recovery
  - Replay behavior corrected for dead jobs at max attempts
- Security hardening:
  - HTTPS-only webhook URL validation (SSRF guardrails)
  - Tightened admin input validation and state checks
- Operator and deployment tooling:
  - `scripts/deploy_verify.sh` for post-deploy verification
  - `scripts/benchmark.sh` for queue/perf sanity checks
  - New `/console` operator UI route for rapid runtime checks
- Documentation:
  - Architecture critique and 90-day roadmap (`docs/ARCHITECTURE_CRITIQUE.md`)
  - One-page business white paper (`docs/ATB_WHITE_PAPER.md`)
- Test coverage expanded to 205 passing tests.

## [1.0.0] — 2026-02-17

First pilot-ready release. All core trust-scoring, policy evaluation, webhook delivery, and admin operations are implemented, tested, and documented.

### Features

**Core API (v0.1–v0.3)**
- Event ingestion with tenant-scoped deduplication
- Trust score computation (weighted event model, 0–100 scale, 4 tiers)
- Score history with pagination and time filtering
- DB-backed API key authentication (SHA-256 hashing, constant-time comparison)
- Multi-tenant isolation across all data and endpoints
- Per-agent rate limiting on score endpoint

**Policy Layer (v0.4)**
- Policy decision endpoint with configurable allow/review/block thresholds
- Per-agent threshold overrides
- HMAC-SHA256 signed webhook notifications on policy decisions
- Synchronous webhook delivery with exponential backoff retries

**Admin API (v0.5)**
- Tenant-scoped CRUD for policy config, agent overrides, and webhooks
- Server-generated webhook secrets (revealed only on create/rotate)
- CI pipeline (GitHub Actions with Postgres service container)

**Webhook Delivery Logging (v0.6)**
- Every webhook attempt persisted for operator visibility
- Delivery log endpoint with filtering

**Async Webhook Worker (v0.7)**
- DB-backed job queue (pending → in_progress → completed/failed/dead)
- Separate worker process with exponential backoff and stale job recovery
- Render Blueprint with web + worker + managed Postgres
- Toggle via `WEBHOOK_ASYNC=true`

**Ops + Admin Completeness (v0.8)**
- API key lifecycle endpoints (create/list/revoke via REST)
- Webhook replay (requeue failed/dead jobs by ID or last-failed)
- Queue stats endpoint (counts by state + recent success rate)
- Retention cleanup CLI (`python -m app.cli cleanup --days N`)

**v1.0.0 Release Package**
- Pilot quickstart guide (`docs/V1_QUICKSTART.md`)
- Operator runbook (`docs/V1_RUNBOOK.md`)
- Automated smoke test script (`scripts/v1_smoke.sh`)
- 140 tests covering all endpoints, tenant isolation, and edge cases

### API Endpoints (22 total)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/v1/intake/events` | Ingest event |
| GET | `/v1/intake/events/{agent_id}` | List agent events |
| GET | `/v1/trust/score/{agent_id}` | Compute trust score |
| GET | `/v1/trust/score/{agent_id}/history` | Score history |
| GET | `/v1/policy/decision/{agent_id}` | Policy decision |
| GET | `/v1/admin/policy/config` | Get policy config |
| PUT | `/v1/admin/policy/config` | Update policy config |
| POST | `/v1/admin/policy/overrides` | Create agent override |
| DELETE | `/v1/admin/policy/overrides/{agent_id}` | Delete agent override |
| GET | `/v1/admin/policy/overrides` | List agent overrides |
| POST | `/v1/admin/policy/webhooks` | Create webhook |
| PATCH | `/v1/admin/policy/webhooks/{id}` | Update webhook |
| GET | `/v1/admin/policy/webhooks` | List webhooks |
| GET | `/v1/admin/policy/webhooks/{id}/deliveries` | Delivery log |
| GET | `/v1/admin/policy/webhooks/{id}/jobs` | Job queue |
| GET | `/v1/admin/policy/webhooks/{id}/stats` | Queue stats |
| POST | `/v1/admin/policy/webhooks/{id}/replay` | Replay job |
| POST | `/v1/admin/keys` | Create API key |
| GET | `/v1/admin/keys` | List API keys |
| POST | `/v1/admin/keys/{id}/revoke` | Revoke API key |

### Infrastructure

- Docker Compose (local dev)
- Render Blueprint (web + worker + Postgres)
- Fly.io config
- GitHub Actions CI
- Alembic migrations (7 versions)

### Database

9 tables: `tenants`, `api_keys`, `events`, `score_history`, `policy_configs`, `agent_policy_overrides`, `policy_webhooks`, `webhook_deliveries`, `webhook_jobs`
