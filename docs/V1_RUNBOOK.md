# Agent Trust Bureau v1.1.0 — Operator Runbook

Troubleshooting and operational procedures for production ATB.

## Table of Contents

1. [Worker Down](#worker-down)
2. [Dead Queue Growth](#dead-queue-growth)
3. [Webhook Endpoint Failures](#webhook-endpoint-failures)
4. [Rollback to Sync Webhooks](#rollback-to-sync-webhooks)
5. [Database Connection Issues](#database-connection-issues)
6. [API Key Compromise](#api-key-compromise)
7. [Retention Cleanup Cadence](#retention-cleanup-cadence)
8. [Monitoring Checklist](#monitoring-checklist)

---

## Worker Down

**Symptoms**: Jobs stuck in `pending` state, `stats` endpoint shows growing `pending` count, no new deliveries.

**Diagnose**:
```bash
# Check stats for a webhook
curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/stats" | jq .

# Check worker logs on Render
render logs --tail 100 --service $WORKER_SERVICE_ID
```

**Fix**:
1. **Render**: Check the worker service in the dashboard. If crashed, click "Manual Deploy" → "Clear build cache and deploy".
2. **Docker**: `docker compose restart worker` or `docker compose up -d worker`.
3. **Verify recovery**: Watch `pending` count decrease in stats endpoint.

**Impact**: No data loss. Pending jobs accumulate and will be delivered when the worker restarts. The web API continues to accept requests normally.

---

## Dead Queue Growth

**Symptoms**: `stats` endpoint shows increasing `dead` count. These are jobs that exhausted all 5 retry attempts.

**Diagnose**:
```bash
# See dead jobs
curl -sS -H "X-API-Key: $KEY" \
  "$BASE/v1/admin/policy/webhooks/$WH_ID/jobs?state=dead&limit=10" | jq .

# Check last_error field for root cause
```

**Fix**:
1. **Identify root cause**: Check `last_error` — common causes: endpoint down, SSL error, DNS failure, HTTP 4xx/5xx.
2. **Fix the upstream webhook endpoint**.
3. **Replay dead jobs**:
   ```bash
   # Replay most recent failed/dead job
   curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"last_failed": true}' \
     "$BASE/v1/admin/policy/webhooks/$WH_ID/replay" | jq .

   # Or replay a specific job by ID
   curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"job_id": 42}' \
     "$BASE/v1/admin/policy/webhooks/$WH_ID/replay" | jq .
   ```
4. **Bulk replay**: For many dead jobs, replay them one at a time by job ID. The worker will pick them up automatically.

---

## Webhook Endpoint Failures

**Symptoms**: Deliveries endpoint shows `success: false`, jobs cycling through `failed` state with backoff.

**Diagnose**:
```bash
# Recent delivery attempts
curl -sS -H "X-API-Key: $KEY" \
  "$BASE/v1/admin/policy/webhooks/$WH_ID/deliveries?limit=20" | jq .

# Check status_code and error fields
```

**Common Causes & Fixes**:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `status_code: 401/403` | Webhook endpoint auth changed | Update webhook URL or endpoint config |
| `status_code: 500` | Upstream server error | Contact webhook endpoint owner |
| `error: "Connection refused"` | Endpoint down | Wait for recovery; jobs auto-retry |
| `error: "SSL..."` | Certificate issue | Fix SSL on webhook endpoint |
| `status_code: 429` | Rate limited by endpoint | Reduce policy decision frequency |

**Retry Behavior**: Jobs retry up to 5 times with exponential backoff (10s, 20s, 40s, 80s, 160s). After 5 failures → `dead` state.

---

## Rollback to Sync Webhooks

If the async worker is causing issues, you can switch back to synchronous (inline) webhook delivery.

**Steps**:
1. Set `WEBHOOK_ASYNC=false` on the web service environment variables.
2. Redeploy the web service.
3. Optionally suspend the worker service (it will safely idle with no pending jobs).

**Impact**:
- Policy decision responses will be slightly slower (webhook delivery happens inline).
- Any pending/failed jobs in the queue will remain. They will be processed if you re-enable async later.
- No data loss.

**Re-enable**:
1. Set `WEBHOOK_ASYNC=true` on web service.
2. Resume the worker service.
3. Redeploy both.

---

## Database Connection Issues

**Symptoms**: HTTP 500 errors, "connection refused" or "too many connections" in logs.

**Diagnose**:
```bash
# Health endpoint will fail if DB is unreachable
curl -sS "$BASE/health"
```

**Fix**:
- **Render managed Postgres**: Check Render dashboard for DB status. May need to restart the database.
- **Connection pool exhaustion**: Restart web and worker services. Consider reducing pool size or adding connection timeouts.
- **Disk full**: Check Render dashboard for Postgres disk usage. Run cleanup to free space:
  ```bash
  python -m app.cli cleanup --days 30
  ```

---

## API Key Compromise

If an API key is compromised:

**Immediate Response**:
```bash
# 1. Identify the compromised key by prefix
curl -sS -H "X-API-Key: $ADMIN_KEY" "$BASE/v1/admin/keys" | jq .

# 2. Revoke it immediately
curl -sS -X POST -H "X-API-Key: $ADMIN_KEY" \
  "$BASE/v1/admin/keys/$COMPROMISED_KEY_ID/revoke" | jq .

# 3. Create a replacement key
curl -sS -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "replacement-key"}' "$BASE/v1/admin/keys" | jq .
```

**Post-Incident**:
- Review recent API activity for the compromised key.
- Rotate webhook secrets if the attacker may have accessed them.
- Audit all active keys: `GET /v1/admin/keys`.

---

## Retention Cleanup Cadence

Old webhook deliveries and terminal jobs accumulate over time. Schedule regular cleanup:

**Recommended cadence**: Weekly or bi-weekly.

```bash
# Preview what will be deleted
python -m app.cli cleanup --days 30 --dry-run

# Actually delete
python -m app.cli cleanup --days 30
```

**What gets deleted**:
- Webhook delivery records older than N days (all statuses)
- Webhook jobs in `completed` or `dead` state older than N days

**What is preserved**:
- Active jobs (`pending`, `in_progress`, `failed`) are never deleted regardless of age
- All events, scores, policy configs, and API keys are untouched

**Render**: Run via Render Shell or add as a cron job.

---

## Monitoring Checklist

Regular health checks for production ATB:

| Check | Command | Expected | Frequency |
|-------|---------|----------|-----------|
| Health | `curl $BASE/health` | `{"status":"ok"}` | Every 1 min |
| Pending jobs | `GET .../stats` → `pending` | < 50 | Every 5 min |
| Dead jobs | `GET .../stats` → `dead` | 0 or stable | Every 5 min |
| Success rate | `GET .../stats` → `recent_success_rate` | > 90% | Every 15 min |
| Worker alive | Check worker logs or Render dashboard | Running | Every 5 min |
| DB disk | Render dashboard | < 80% | Daily |

**Alert thresholds** (suggested):
- `pending > 100` → Worker may be down
- `dead` increasing → Webhook endpoint issue
- `recent_success_rate < 50%` → Investigate delivery failures
- Health endpoint returns non-200 → Service down

---

## Support

- **API docs**: `$BASE/docs` (Swagger UI)
- **Quickstart**: `docs/V1_QUICKSTART.md`
- **Changelog**: `CHANGELOG.md`
- **Source**: https://github.com/jerbey4/agent-trust-bureau
