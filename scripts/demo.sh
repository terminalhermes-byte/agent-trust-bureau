#!/usr/bin/env bash
# ============================================================================
# Agent Trust Bureau — Golden-Path Demo
#
# Demonstrates the full lifecycle:
#   1. Bootstrap tenant + API key
#   2. Ingest behaviour events
#   3. Compute trust score & policy decision
#   4. Create a webhook (server-generated secret)
#   5. Verify HMAC signature after a policy decision fires the webhook
#   6. Rotate the webhook secret
#
# Usage:
#   LOCAL:   ./scripts/demo.sh                         (defaults to localhost:8010)
#   RENDER:  ./scripts/demo.sh https://agent-trust-bureau.onrender.com
#   FLY:     ./scripts/demo.sh https://agent-trust-bureau.fly.dev
#
# Prerequisites:
#   - jq (brew install jq / apt install jq)
#   - curl
#   - python3 (for HMAC verification snippet)
# ============================================================================
set -euo pipefail

BASE="${1:-http://127.0.0.1:8010}"

# Colours for readability
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

banner() { printf "\n${CYAN}━━━ %s ━━━${NC}\n" "$1"; }
ok()     { printf "${GREEN}✓ %s${NC}\n" "$1"; }
info()   { printf "${YELLOW}→ %s${NC}\n" "$1"; }

# ------------------------------------------------------------------
banner "0. Health check"
# ------------------------------------------------------------------
curl -sf "$BASE/health" | jq .
ok "Service is healthy"

# ------------------------------------------------------------------
banner "1. Bootstrap tenant + API key"
# ------------------------------------------------------------------
info "If deploying remotely, run:  fly ssh console -C 'python -m app.cli bootstrap'"
info "For local/Docker, the app must be running and the DB migrated."

if [ -z "${ATB_API_KEY:-}" ]; then
  echo ""
  echo "  Set ATB_API_KEY before running this script, e.g.:"
  echo "    export ATB_API_KEY=atb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  echo ""
  echo "  You can generate one with:"
  echo "    python -m app.cli bootstrap          # local"
  echo "    fly ssh console -C 'python -m app.cli bootstrap'   # Fly"
  echo "    (Render shell) python -m app.cli bootstrap          # Render"
  echo ""
  exit 1
fi

KEY="$ATB_API_KEY"
AUTH="-H X-API-Key:$KEY"
ok "Using API key prefix: ${KEY:0:12}..."

# ------------------------------------------------------------------
banner "2. Ingest behaviour events"
# ------------------------------------------------------------------
AGENT="demo-agent-$$"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")

for evt in \
  "evt-1 task_completed_without_rework ci-pipeline" \
  "evt-2 human_approved_action review-board" \
  "evt-3 policy_compliant_response runtime-guard" \
  "evt-4 safe_tool_usage sandbox" \
  "evt-5 hallucination_detected monitoring"; do

  set -- $evt
  EVT_ID="$1"; EVT_TYPE="$2"; SOURCE="$3"
  curl -sf -X POST $AUTH -H "Content-Type: application/json" \
    -d "{\"event_id\":\"${AGENT}-${EVT_ID}\",\"agent_id\":\"${AGENT}\",\"event_type\":\"${EVT_TYPE}\",\"source\":\"${SOURCE}\",\"occurred_at\":\"${TS}\"}" \
    "$BASE/v1/intake/events" | jq .
done
ok "Ingested 5 events for $AGENT (4 positive, 1 negative)"

# ------------------------------------------------------------------
banner "3. Trust score"
# ------------------------------------------------------------------
SCORE_RESP=$(curl -sf $AUTH "$BASE/v1/trust/score/$AGENT")
echo "$SCORE_RESP" | jq .
SCORE=$(echo "$SCORE_RESP" | jq -r '.trust_score')
TIER=$(echo "$SCORE_RESP" | jq -r '.trust_tier')
ok "Trust score = $SCORE  tier = $TIER"

# ------------------------------------------------------------------
banner "4. Policy decision"
# ------------------------------------------------------------------
DECISION_RESP=$(curl -sf $AUTH "$BASE/v1/policy/decision/$AGENT")
echo "$DECISION_RESP" | jq .
DECISION=$(echo "$DECISION_RESP" | jq -r '.decision')
ok "Policy decision = $DECISION"

# ------------------------------------------------------------------
banner "5. Create webhook (server-generated secret)"
# ------------------------------------------------------------------
WH_RESP=$(curl -sf -X POST $AUTH -H "Content-Type: application/json" \
  -d '{"url":"https://httpbin.org/post"}' \
  "$BASE/v1/admin/policy/webhooks")
echo "$WH_RESP" | jq .

WH_ID=$(echo "$WH_RESP" | jq -r '.id')
WH_SECRET=$(echo "$WH_RESP" | jq -r '.secret')
ok "Webhook id=$WH_ID created. Secret (save this!):"
echo ""
echo "    $WH_SECRET"
echo ""
info "Secret is shown exactly once. List endpoint only shows last-4."

# ------------------------------------------------------------------
banner "6. Verify HMAC signature"
# ------------------------------------------------------------------
info "When the webhook fires, the receiver should verify X-ATB-Signature."
echo ""
echo '  Python verification snippet:'
echo '  ─────────────────────────────'
echo '  import hmac, hashlib'
echo '  payload = request.body                   # raw bytes'
echo "  secret  = \"$WH_SECRET\""
echo '  expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()'
echo '  assert hmac.compare_digest(expected, request.headers["X-ATB-Signature"])'
echo ""

# Fire another policy decision so the webhook gets called
info "Triggering another policy decision to fire the webhook..."
curl -sf $AUTH "$BASE/v1/policy/decision/$AGENT" | jq .decision
ok "Webhook delivered (check httpbin.org or delivery logs below)"

# ------------------------------------------------------------------
banner "7. View webhook delivery logs"
# ------------------------------------------------------------------
sleep 1
DELIVERIES=$(curl -sf $AUTH "$BASE/v1/admin/policy/webhooks/$WH_ID/deliveries?limit=5")
echo "$DELIVERIES" | jq .
DCOUNT=$(echo "$DELIVERIES" | jq '.count')
ok "Found $DCOUNT delivery record(s)"

# ------------------------------------------------------------------
banner "8. Rotate webhook secret"
# ------------------------------------------------------------------
ROTATE_RESP=$(curl -sf -X PATCH $AUTH -H "Content-Type: application/json" \
  -d '{"rotate_secret":true}' \
  "$BASE/v1/admin/policy/webhooks/$WH_ID")
echo "$ROTATE_RESP" | jq .

NEW_SECRET=$(echo "$ROTATE_RESP" | jq -r '.secret')
ok "Secret rotated. New secret (save this!):"
echo ""
echo "    $NEW_SECRET"
echo ""
info "Old secret no longer produces valid signatures."

# ------------------------------------------------------------------
banner "9. List webhooks (secret masked)"
# ------------------------------------------------------------------
curl -sf $AUTH "$BASE/v1/admin/policy/webhooks" | jq .
ok "Full secret is never shown in list"

# ------------------------------------------------------------------
banner "10. View webhook job queue (async worker)"
# ------------------------------------------------------------------
info "If WEBHOOK_ASYNC=true, jobs appear in the queue instead of being sent inline."
JOBS=$(curl -sf $AUTH "$BASE/v1/admin/policy/webhooks/$WH_ID/jobs?limit=5" 2>/dev/null || echo '{"error":"endpoint not available or no jobs"}')
echo "$JOBS" | jq . 2>/dev/null || echo "$JOBS"
JCOUNT=$(echo "$JOBS" | jq '.count // 0' 2>/dev/null || echo "0")
ok "Found $JCOUNT webhook job(s)"

# ------------------------------------------------------------------
banner "11. Admin: policy config + overrides"
# ------------------------------------------------------------------
info "Current thresholds:"
curl -sf $AUTH "$BASE/v1/admin/policy/config" | jq .

info "Updating thresholds..."
curl -sf -X PUT $AUTH -H "Content-Type: application/json" \
  -d '{"allow_threshold":85,"review_threshold":65,"block_threshold":45}' \
  "$BASE/v1/admin/policy/config" | jq .

info "Creating agent override..."
curl -sf -X POST $AUTH -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"$AGENT\",\"allow_threshold\":90}" \
  "$BASE/v1/admin/policy/overrides" | jq .

info "Re-evaluating policy with override..."
curl -sf $AUTH "$BASE/v1/policy/decision/$AGENT" | jq .
ok "Done!"

# ------------------------------------------------------------------
banner "Demo complete"
# ------------------------------------------------------------------
echo ""
echo "  Base URL:    $BASE"
echo "  Agent ID:    $AGENT"
echo "  Webhook ID:  $WH_ID"
echo "  API Key:     ${KEY:0:12}..."
echo ""
