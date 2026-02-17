#!/usr/bin/env bash
# deploy_verify.sh — Post-deploy verification for Agent Trust Bureau
#
# Runs after a deployment to verify the service is healthy and all
# critical endpoints respond correctly. Designed for CI/CD pipelines
# and Render deploy hooks.
#
# Usage:
#   ./scripts/deploy_verify.sh <BASE_URL> <API_KEY>
#
# Example:
#   ./scripts/deploy_verify.sh https://agent-trust-bureau.onrender.com atb_xxx
#
# Exit codes:
#   0 — All checks passed
#   1 — One or more checks failed
set -euo pipefail

BASE="${1:?Usage: $0 <BASE_URL> <API_KEY>}"
KEY="${2:?Usage: $0 <BASE_URL> <API_KEY>}"
MAX_WAIT="${3:-60}"  # Max seconds to wait for health

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "  ✅ $label"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $label — expected $expected, got $actual"
    FAIL=$((FAIL + 1))
  fi
}

check_range() {
  local label="$1" min="$2" max="$3" actual="$4"
  if [ "$actual" -ge "$min" ] && [ "$actual" -le "$max" ]; then
    echo "  ✅ $label (HTTP $actual)"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $label — expected ${min}-${max}, got $actual"
    FAIL=$((FAIL + 1))
  fi
}

echo ""
echo "======================================================================"
echo "  Agent Trust Bureau — Deploy Verification"
echo "  Target: $BASE"
echo "  Max wait: ${MAX_WAIT}s"
echo "======================================================================"
echo ""

# -----------------------------------------------------------------------
# Phase 1: Wait for health
# -----------------------------------------------------------------------
echo "--- Phase 1: Health Check ---"
ELAPSED=0
HEALTHY=false
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 "$BASE/health" 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    HEALTHY=true
    break
  fi
  echo "  ⏳ Waiting for health... (${ELAPSED}s, got HTTP $STATUS)"
  sleep 5
  ELAPSED=$((ELAPSED + 5))
done

if [ "$HEALTHY" = true ]; then
  echo "  ✅ Health check passed (${ELAPSED}s)"
  PASS=$((PASS + 1))
else
  echo "  ❌ Health check failed after ${MAX_WAIT}s"
  FAIL=$((FAIL + 1))
  echo ""
  echo "  Service is not reachable. Aborting remaining checks."
  exit 1
fi

# -----------------------------------------------------------------------
# Phase 2: Version check
# -----------------------------------------------------------------------
echo ""
echo "--- Phase 2: Version ---"
VERSION=$(curl -sS -H "Accept: application/json" "$BASE/" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")
echo "  ℹ️  Version: $VERSION"

# -----------------------------------------------------------------------
# Phase 3: Auth enforcement
# -----------------------------------------------------------------------
echo ""
echo "--- Phase 3: Auth Enforcement ---"
# Admin endpoint without key should be 401
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/v1/admin/keys" 2>/dev/null || echo "000")
check "Admin without key → 401" "401" "$STATUS"

# Admin endpoint with valid key should be 200
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/keys" 2>/dev/null || echo "000")
check "Admin with valid key → 200" "200" "$STATUS"

# -----------------------------------------------------------------------
# Phase 4: Core endpoints
# -----------------------------------------------------------------------
echo ""
echo "--- Phase 4: Core Endpoints ---"

# Policy config
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/config" 2>/dev/null || echo "000")
check "GET /v1/admin/policy/config → 200" "200" "$STATUS"

# Overrides
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/overrides" 2>/dev/null || echo "000")
check "GET /v1/admin/policy/overrides → 200" "200" "$STATUS"

# Webhooks
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks" 2>/dev/null || echo "000")
check "GET /v1/admin/policy/webhooks → 200" "200" "$STATUS"

# Event ingestion
EVT_ID="deploy-verify-$(date +%s)"
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"event_id\":\"$EVT_ID\",\"agent_id\":\"deploy-verify\",\"event_type\":\"task_completed_without_rework\",\"source\":\"deploy-verify\",\"occurred_at\":\"2025-01-01T00:00:00Z\"}" \
  "$BASE/v1/intake/events" 2>/dev/null || echo "000")
check "POST /v1/intake/events → 200" "200" "$STATUS"

# Trust score
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/trust/score/deploy-verify" 2>/dev/null || echo "000")
check "GET /v1/trust/score/deploy-verify → 200" "200" "$STATUS"

# Policy decision
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/policy/decision/deploy-verify" 2>/dev/null || echo "000")
check "GET /v1/policy/decision/deploy-verify → 200" "200" "$STATUS"

# Score history
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" "$BASE/v1/trust/score/deploy-verify/history" 2>/dev/null || echo "000")
check "GET /v1/trust/score/deploy-verify/history → 200" "200" "$STATUS"

# -----------------------------------------------------------------------
# Phase 5: UI pages
# -----------------------------------------------------------------------
echo ""
echo "--- Phase 5: UI Pages ---"
for path in "/" "/console" "/docs" "/guide"; do
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$path" 2>/dev/null || echo "000")
  check "GET $path → 200" "200" "$STATUS"
done

# -----------------------------------------------------------------------
# Phase 6: Latency check
# -----------------------------------------------------------------------
echo ""
echo "--- Phase 6: Latency ---"
HEALTH_TIME=$(curl -sS -o /dev/null -w '%{time_total}' "$BASE/health" 2>/dev/null || echo "9999")
HEALTH_MS=$(python3 -c "print(int(float('$HEALTH_TIME') * 1000))" 2>/dev/null || echo "9999")
if [ "$HEALTH_MS" -lt 2000 ]; then
  echo "  ✅ Health latency: ${HEALTH_MS}ms (< 2s)"
  PASS=$((PASS + 1))
else
  echo "  ❌ Health latency: ${HEALTH_MS}ms (≥ 2s — cold start?)"
  FAIL=$((FAIL + 1))
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "======================================================================"
echo "  Deploy Verification: $PASS passed, $FAIL failed"
echo "======================================================================"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  ⚠️  Some checks failed. Review the output above."
  exit 1
fi

echo ""
echo "  ✅ All checks passed. Deployment is verified."
