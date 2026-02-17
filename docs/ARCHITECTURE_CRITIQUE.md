# Agent Trust Bureau — Architecture Critique & Decision Memo

**Author**: Claude (automated analysis)
**Date**: 2026-02-17
**Scope**: Full codebase at v1.1-hardening (branch `v1.1/hardening-and-reliability`)
**Status**: Read-only analysis — no code changes proposed in this document

---

## 1. Architecture Map

### Component Inventory

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **HTTP** | `app/main.py`, `app/routers/{admin,events,policy,trust}.py` | FastAPI request handling, auth, response serialization |
| **Auth** | `app/auth.py` | API key resolution, SHA-256 hashing, constant-time compare |
| **Business Logic** | `app/services/{scoring,policy,webhook}.py` | Trust scoring, policy evaluation, webhook delivery |
| **Persistence** | `app/store.py`, `app/admin_store.py`, `app/db.py` | SQLAlchemy ORM, tenant-scoped CRUD |
| **Models** | `app/models.py` | 9 declarative ORM models |
| **Schemas** | `app/schemas.py` | Pydantic request/response validation |
| **Worker** | `app/worker.py` | Separate process for async webhook delivery |
| **CLI** | `app/cli.py` | Bootstrap, key creation, cleanup commands |
| **Config** | `app/config.py` | Pydantic settings from env vars |
| **Migrations** | `alembic/versions/0001-0007` | Schema evolution (7 migrations) |

### Request Flow

```
Client → FastAPI Router → Auth Middleware → Service Layer → Store/DB
                                              ↓ (webhook)
                                         enqueue_webhook() → webhook_jobs table
                                              ↓ (async)
                                         Worker process → process_webhook_job() → HTTP POST
```

### Data Model

```
Tenant (1) ──→ (N) ApiKey
       (1) ──→ (N) EventRecord
       (1) ──→ (N) ScoreSnapshot
       (1) ──→ (1) PolicyConfig
       (1) ──→ (N) AgentPolicyOverride
       (1) ──→ (1) PolicyWebhook ──→ (N) WebhookJob
                                  ──→ (N) WebhookDelivery
```

### Codebase Stats

- **~8,800 lines** of Python (app + tests)
- **204 tests** across 15 test files
- **22 API endpoints** + health + console + docs
- **9 ORM models**, 7 Alembic migrations
- **Zero external runtime dependencies** beyond FastAPI, SQLAlchemy, httpx, Pydantic

---

## 2. Strengths

### S1: Clean Layering
The codebase has a clear separation: routers → services → store → models. No router directly accesses ORM models. This makes testing and refactoring straightforward.

### S2: Multi-Tenant by Default
Every table has `tenant_id`. Every query is tenant-scoped. Cross-tenant isolation is enforced at the store layer, not just at the router level. This is a fundamentally sound design decision.

### S3: DB-Backed Job Queue
Using the existing Postgres database as a job queue (vs. Redis/RabbitMQ) was the right call for a v1. It eliminates operational complexity, uses existing infrastructure, and provides transactional guarantees. The `FOR UPDATE SKIP LOCKED` claim pattern is production-grade.

### S4: Comprehensive Test Coverage
204 tests including unit, integration, reliability, and security tests. The test fixtures are well-structured with reusable `_make_db_and_seed()` patterns.

### S5: Operational Tooling
The smoke test, demo script, benchmark script, deploy verification script, and CLI cleanup command show operational maturity unusual for a v1.

---

## 3. Critique — Current Architecture Weaknesses

### C1: Scoring Model is Hardcoded (HIGH)

**Problem**: The trust scoring model (`app/services/scoring.py`) uses hardcoded event weights in two dictionaries. There is no way to customize event types or weights per tenant without a code deploy.

**Impact**: Every new customer with different event types or risk profiles requires a code change. The scoring model cannot evolve independently of the API.

**Evidence**: `POSITIVE_EVENTS` and `NEGATIVE_EVENTS` are module-level constants. Unknown event types are silently counted but contribute nothing to the score.

### C2: Score Recomputation on Every Request (MEDIUM)

**Problem**: `GET /v1/policy/decision/{agent_id}` recomputes the trust score from scratch by loading ALL events for the agent, running the scoring model, saving a snapshot, and then evaluating the policy. This is O(N) in the number of events.

**Impact**: As agents accumulate thousands of events, this endpoint will slow down. Every policy decision triggers a full rescore even if no new events have occurred.

**Evidence**: In `app/routers/policy.py`, lines 24-28, every decision call runs `list_agent_events()` → `calculate_trust_score()` → `save_score_snapshot()`.

### C3: Single Webhook Per Tenant (MEDIUM)

**Problem**: `PolicyWebhook` has a `UNIQUE(tenant_id)` constraint, limiting each tenant to exactly one webhook URL. Multi-destination fanout, environment-specific webhooks (staging vs prod), or event-type filtering are impossible.

**Impact**: Tenants cannot receive webhooks at multiple endpoints. Migration to multiple webhooks would require a schema change.

### C4: Synchronous Inline Score Saves (LOW)

**Problem**: `save_score_snapshot()` commits to the database inside the request path for both `/v1/trust/score/{agent_id}` and `/v1/policy/decision/{agent_id}`. Every score lookup creates a new row in `score_history`.

**Impact**: The `score_history` table will grow rapidly (1 row per score lookup). The synchronous write adds latency to every score request. There is no deduplication — the same agent scored twice in 1 second creates two identical rows.

### C5: Rate Limiter is Per-Process Only (LOW)

**Problem**: The rate limiter in `app/rate_limit.py` uses an in-memory `defaultdict(deque)`. With multiple web workers (Gunicorn, Render scaling), each process has its own rate limit window.

**Impact**: Effective rate limit is multiplied by the number of workers. A 30/min limit with 4 workers becomes effectively 120/min.

### C6: No Event Deduplication Across Requests (LOW)

**Problem**: While `EventRecord` has a `UNIQUE(tenant_id, event_id)` constraint preventing duplicate event IDs, there's no idempotency at the ingestion level — the API returns a 500 on duplicate instead of a 200.

**Evidence**: In `app/routers/events.py`, `IntegrityError` is caught and returns 409, but callers might expect idempotent 200 behavior.

---

## 4. Alternative Architecture A — Event-Sourced Scoring

### Core Idea
Replace the "rescore from all events" pattern with an incremental scoring model where each new event updates a running score.

### Changes
1. Add a `current_score` table: `(tenant_id, agent_id, score, tier, event_count, last_event_id, updated_at)`
2. On event ingestion, compute the delta from the single new event and update the running score in-place
3. `GET /v1/trust/score/{agent_id}` reads the pre-computed score (O(1) instead of O(N))
4. `GET /v1/policy/decision/{agent_id}` reads the pre-computed score + evaluates policy (no rescore)
5. Score history snapshots are written only when the score materially changes (e.g., > 1 point delta)

### Tradeoffs

| Dimension | Current | Alternative A |
|-----------|---------|--------------|
| Score freshness | Always current | Always current (updated on ingest) |
| Score request latency | O(N events) | O(1) read |
| Event processing latency | O(1) ingest | O(1) ingest + O(1) score update |
| Score history growth | 1 row per score request | 1 row per material change |
| Scoring model changes | Recompute all on next request | Need migration/backfill script |
| Complexity | Simple | Moderate (need backfill tooling) |

### Recommendation
**Adopt when**: An agent has > 500 events, or decision latency exceeds 100ms. The incremental model is strictly better at scale, but requires a backfill mechanism when scoring weights change.

---

## 5. Alternative Architecture B — Pluggable Scoring Models

### Core Idea
Make the scoring model a configurable, tenant-level entity rather than hardcoded Python dictionaries.

### Changes
1. Add a `scoring_models` table: `(id, tenant_id, name, version, config_json, is_active)`
2. `config_json` contains event type → weight mappings
3. Add a `scoring_model_id` FK to `current_score` (or re-score with the active model)
4. Admin endpoint: `PUT /v1/admin/scoring/model` to update weights
5. Support multiple model versions with A/B testing: score with both, return the active one, log both

### Tradeoffs

| Dimension | Current | Alternative B |
|-----------|---------|--------------|
| Customization | Code deploy required | API-driven, per-tenant |
| Model versioning | Single hardcoded version | Multiple versions, A/B capable |
| Auditing | No model audit trail | Full version history |
| Complexity | Trivial | Moderate (schema + API + migration tooling) |
| Testing | Unit tests only | Need model validation API |
| Onboarding | Same model for everyone | Tenant-specific tuning |

### Recommendation
**Adopt when**: The second paying customer wants different event weights. This is a natural v2 feature that unlocks per-tenant customization without code deploys.

---

## 6. 90-Day Roadmap

### Weeks 1-2: Stability & Monitoring
- [ ] Set up Render alerting on health endpoint + worker status
- [ ] Add structured JSON logging (replace print-style logging)
- [ ] Implement Redis-backed rate limiting (or Postgres advisory locks)
- [ ] Add event ingestion idempotency (return 200 on duplicate instead of 409)

### Weeks 3-4: Scoring Performance (Alternative A)
- [ ] Add `current_scores` table with running score per agent
- [ ] Update event ingestion to incrementally update running score
- [ ] Modify score/decision endpoints to read pre-computed score
- [ ] Build backfill CLI command for when weights change
- [ ] Add score change webhook (notify when score crosses a threshold)

### Weeks 5-6: Multi-Webhook & Filtering
- [ ] Remove `UNIQUE(tenant_id)` from `policy_webhooks`
- [ ] Add webhook event filters (e.g., only fire on "block" decisions)
- [ ] Add webhook delivery retry configuration per webhook
- [ ] Support multiple webhook URLs per tenant

### Weeks 7-8: Pluggable Scoring (Alternative B)
- [ ] Add `scoring_models` table and admin API
- [ ] Implement model loading from database config
- [ ] Build model validation endpoint (dry-run scoring)
- [ ] Add A/B scoring support (score with two models, return active)

### Weeks 9-10: Scale & Operational Excellence
- [ ] Connection pooling with PgBouncer for Render
- [ ] Read replica routing for score lookups
- [ ] Grafana dashboard for queue depth, delivery latency, error rates
- [ ] Canary deployment support (health check versioning)

### Weeks 11-12: API v2 Planning
- [ ] Design v2 API with breaking changes collected over 90 days
- [ ] Plan migration path from v1 → v2
- [ ] Build integration SDK (Python client library)
- [ ] Write OpenAPI-first documentation with examples

---

## 7. Decision Memo

### Decisions Made in v1 (and their rationale)

| Decision | Rationale | Revisit When |
|----------|-----------|-------------|
| Postgres as job queue | No new infrastructure needed | > 1000 jobs/min or need pub/sub |
| Single webhook per tenant | YAGNI — start simple | Second customer needs multiple |
| Hardcoded scoring model | Ship fast, iterate later | First customer customization request |
| In-memory rate limiter | Acceptable for single-instance | Scale to multiple web workers |
| Score on every request | Correct by construction | Decision latency > 100ms |
| SHA-256 for key hashing | Standard, fast, sufficient | Brute-force concerns (switch to bcrypt) |
| HMAC-SHA256 for webhooks | Industry standard (Stripe, GitHub) | Never — this is correct |
| Alembic for migrations | Standard SQLAlchemy tooling | Never — this is correct |
| SQLite for tests | Fast, no external deps | Need to test Postgres-specific features |

### Key Risks

1. **Scoring latency at scale**: The O(N) rescore will become the bottleneck. Plan to implement Alternative A by week 4.

2. **Single point of failure**: One worker process, one web process. Render can autoscale web but worker needs manual scaling or a queue-aware orchestrator.

3. **Tenant data volume**: No per-tenant quotas on events, scores, or API calls beyond the simple rate limiter. A runaway client could fill the database.

4. **Schema evolution**: The 1:1 webhook-per-tenant constraint will need to change. Plan the migration before it becomes urgent.

### What NOT to Change

- **Multi-tenant data model**: The `tenant_id` on every table is correct and should remain.
- **API key auth pattern**: Prefix-based lookup + constant-time hash comparison is sound.
- **Webhook HMAC signing**: Industry-standard, well-implemented.
- **Test infrastructure**: SQLite in-memory for tests with StaticPool is fast and reliable.
- **FastAPI + SQLAlchemy stack**: Mature, well-documented, good for this problem size.
