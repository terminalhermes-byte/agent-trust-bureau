#!/usr/bin/env bash
# v1_smoke.sh — Agent Trust Bureau v1.0.0 smoke test
#
# Usage:
#   ./scripts/v1_smoke.sh <BASE_URL> <API_KEY>
#
# Example:
#   ./scripts/v1_smoke.sh http://127.0.0.1:8010 atb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#
# Exits 0 if all checks pass, 1 on first failure.
set -euo pipefail

BASE="${1:?Usage: $0 <BASE_URL> <API_KEY>}"
KEY="${2:?Usage: $0 <BASE_URL> <API_KEY>}"

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "  ✅ $label (HTTP $actual)"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $label — expected $expected, got $actual"
    FAIL=$((FAIL + 1))
  fi
}

echo ""
echo "======================================================================"
echo "  Agent Trust Bureau v1.0.0 — Smoke Test"
echo "  Base: $BASE"
echo "======================================================================"
echo ""

# 1. Health
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/health")
check "GET /health" "200" "$STATUS"

# 2. Create API key
RESP=$(curl -sS -w '\n%{http_code}' -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"smoke-test-key"}' "$BASE/v1/admin/keys")
STATUS=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
check "POST /v1/admin/keys (create)" "201" "$STATUS"
NEW_KEY_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

# 3. List keys
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/keys")
check "GET /v1/admin/keys (list)" "200" "$STATUS"

# 4. Revoke key
if [ -n "$NEW_KEY_ID" ]; then
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "X-API-Key: $KEY" "$BASE/v1/admin/keys/$NEW_KEY_ID/revoke")
  check "POST /v1/admin/keys/$NEW_KEY_ID/revoke" "200" "$STATUS"
fi

# 5. Policy config
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/config")
check "GET /v1/admin/policy/config" "200" "$STATUS"

# 6. Create webhook (may 409 if exists — both are acceptable)
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://httpbin.org/post"}' "$BASE/v1/admin/policy/webhooks")
if [ "$STATUS" = "201" ] || [ "$STATUS" = "409" ]; then
  echo "  ✅ POST /v1/admin/policy/webhooks (HTTP $STATUS)"
  PASS=$((PASS + 1))
else
  echo "  ❌ POST /v1/admin/policy/webhooks — expected 201 or 409, got $STATUS"
  FAIL=$((FAIL + 1))
fi

# 7. List webhooks and get ID
WH_RESP=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks")
WH_ID=$(echo "$WH_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['webhooks'][0]['id'] if d.get('webhooks') else '')" 2>/dev/null || echo "")
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks")
check "GET /v1/admin/policy/webhooks (list)" "200" "$STATUS"

# 8. Policy decision (triggers async job if webhook exists)
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/policy/decision/smoke-agent")
check "GET /v1/policy/decision/smoke-agent" "200" "$STATUS"

# 9. Jobs endpoint
if [ -n "$WH_ID" ]; then
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/jobs")
  check "GET /v1/admin/policy/webhooks/$WH_ID/jobs" "200" "$STATUS"
fi

# 10. Deliveries endpoint
if [ -n "$WH_ID" ]; then
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/deliveries")
  check "GET /v1/admin/policy/webhooks/$WH_ID/deliveries" "200" "$STATUS"
fi

# 11. Stats endpoint
if [ -n "$WH_ID" ]; then
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/stats")
  check "GET /v1/admin/policy/webhooks/$WH_ID/stats" "200" "$STATUS"
fi

# 12. Replay (last_failed) — may 404 if no failed jobs, both OK for smoke
if [ -n "$WH_ID" ]; then
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"last_failed":true}' "$BASE/v1/admin/policy/webhooks/$WH_ID/replay")
  if [ "$STATUS" = "200" ] || [ "$STATUS" = "404" ]; then
    echo "  ✅ POST /v1/admin/policy/webhooks/$WH_ID/replay (HTTP $STATUS)"
    PASS=$((PASS + 1))
  else
    echo "  ❌ POST replay — expected 200 or 404, got $STATUS"
    FAIL=$((FAIL + 1))
  fi
fi

# 13. Overrides
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/overrides")
check "GET /v1/admin/policy/overrides" "200" "$STATUS"

# 14. Ingest event
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"event_id\":\"smoke-$(date +%s)\",\"agent_id\":\"smoke-agent\",\"event_type\":\"task_completed_without_rework\",\"source\":\"smoke\",\"occurred_at\":\"2025-01-01T00:00:00Z\"}" \
  "$BASE/v1/intake/events")
check "POST /v1/intake/events" "200" "$STATUS"

# 15. Trust score
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/trust/score/smoke-agent")
check "GET /v1/trust/score/smoke-agent" "200" "$STATUS"

# 16. Score history
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/trust/score/smoke-agent/history")
check "GET /v1/trust/score/smoke-agent/history" "200" "$STATUS"

echo ""
echo "======================================================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "======================================================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
