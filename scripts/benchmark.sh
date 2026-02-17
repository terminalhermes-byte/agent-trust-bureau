#!/usr/bin/env bash
# benchmark.sh — Agent Trust Bureau performance sanity benchmark
#
# Measures baseline throughput for the webhook job pipeline:
#   1. Enqueue N jobs via API
#   2. Process all jobs via worker
#   3. Report timing
#
# Usage:
#   ./scripts/benchmark.sh <BASE_URL> <API_KEY> [NUM_JOBS]
#
# Example:
#   ./scripts/benchmark.sh http://127.0.0.1:8010 atb_xxxx 100
#
# Requires: curl, jq, python3, time
set -euo pipefail

BASE="${1:?Usage: $0 <BASE_URL> <API_KEY> [NUM_JOBS]}"
KEY="${2:?Usage: $0 <BASE_URL> <API_KEY> [NUM_JOBS]}"
N="${3:-100}"

echo ""
echo "======================================================================"
echo "  Agent Trust Bureau — Performance Sanity Benchmark"
echo "  Base: $BASE"
echo "  Jobs: $N"
echo "======================================================================"
echo ""

# 0. Health check
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/health")
if [ "$STATUS" != "200" ]; then
  echo "❌ Health check failed (HTTP $STATUS). Is the server running?"
  exit 1
fi
echo "✅ Health check OK"

# 1. Ensure webhook exists
WH_RESP=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks")
WH_COUNT=$(echo "$WH_RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('webhooks',[])))" 2>/dev/null || echo "0")
if [ "$WH_COUNT" = "0" ]; then
  echo "Creating webhook for benchmarking..."
  curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"url":"https://httpbin.org/post"}' "$BASE/v1/admin/policy/webhooks" > /dev/null
fi

WH_RESP=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks")
WH_ID=$(echo "$WH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['webhooks'][0]['id'])" 2>/dev/null)
echo "✅ Webhook ready (ID: $WH_ID)"

# 2. Get baseline stats
BEFORE_STATS=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/stats")
BEFORE_COMPLETED=$(echo "$BEFORE_STATS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('completed',0))" 2>/dev/null)

# 3. Benchmark: Ingest N events (triggers policy decisions + webhook jobs)
echo ""
echo "--- Phase 1: Ingesting $N events ---"
INGEST_START=$(python3 -c "import time; print(time.time())")

for i in $(seq 1 "$N"); do
  curl -sS -o /dev/null -X POST \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d "{\"event_id\":\"bench-$(date +%s%N)-$i\",\"agent_id\":\"bench-agent-$((i % 10))\",\"event_type\":\"task_completed_without_rework\",\"source\":\"benchmark\",\"occurred_at\":\"2025-01-01T00:00:00Z\"}" \
    "$BASE/v1/intake/events"
done

INGEST_END=$(python3 -c "import time; print(time.time())")
INGEST_ELAPSED=$(python3 -c "print(round($INGEST_END - $INGEST_START, 2))")
INGEST_RPS=$(python3 -c "print(round($N / ($INGEST_END - $INGEST_START), 1))")
echo "  Ingested $N events in ${INGEST_ELAPSED}s (${INGEST_RPS} events/sec)"

# 4. Benchmark: Trigger policy decisions (creates webhook jobs)
echo ""
echo "--- Phase 2: Triggering $N policy decisions ---"
DECISION_START=$(python3 -c "import time; print(time.time())")

for i in $(seq 0 9); do
  curl -sS -o /dev/null -H "X-API-Key: $KEY" "$BASE/v1/policy/decision/bench-agent-$i"
done

DECISION_END=$(python3 -c "import time; print(time.time())")
DECISION_ELAPSED=$(python3 -c "print(round($DECISION_END - $DECISION_START, 2))")
echo "  Triggered 10 policy decisions in ${DECISION_ELAPSED}s"

# 5. Check queue growth
sleep 1
AFTER_STATS=$(curl -sS -H "X-API-Key: $KEY" "$BASE/v1/admin/policy/webhooks/$WH_ID/stats")
PENDING=$(echo "$AFTER_STATS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pending',0))" 2>/dev/null)
IN_PROGRESS=$(echo "$AFTER_STATS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('in_progress',0))" 2>/dev/null)
COMPLETED_NOW=$(echo "$AFTER_STATS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('completed',0))" 2>/dev/null)
NEW_COMPLETED=$((COMPLETED_NOW - BEFORE_COMPLETED))

echo ""
echo "--- Phase 3: Queue Stats ---"
echo "  Pending:     $PENDING"
echo "  In Progress: $IN_PROGRESS"
echo "  New Completed: $NEW_COMPLETED"

# 6. Benchmark: Score computation latency (single request)
echo ""
echo "--- Phase 4: Latency measurements ---"
SCORE_TIMES=""
for i in $(seq 1 5); do
  T=$(curl -sS -o /dev/null -w '%{time_total}' -H "X-API-Key: $KEY" "$BASE/v1/trust/score/bench-agent-0")
  SCORE_TIMES="$SCORE_TIMES $T"
done
AVG_SCORE=$(echo "$SCORE_TIMES" | python3 -c "
import sys
vals = [float(x) for x in sys.stdin.read().split()]
print(f'{sum(vals)/len(vals)*1000:.0f}')
")
echo "  Avg trust-score latency: ${AVG_SCORE}ms (5 samples)"

DECISION_TIMES=""
for i in $(seq 1 5); do
  T=$(curl -sS -o /dev/null -w '%{time_total}' -H "X-API-Key: $KEY" "$BASE/v1/policy/decision/bench-agent-0")
  DECISION_TIMES="$DECISION_TIMES $T"
done
AVG_DECISION=$(echo "$DECISION_TIMES" | python3 -c "
import sys
vals = [float(x) for x in sys.stdin.read().split()]
print(f'{sum(vals)/len(vals)*1000:.0f}')
")
echo "  Avg policy-decision latency: ${AVG_DECISION}ms (5 samples)"

HEALTH_TIMES=""
for i in $(seq 1 5); do
  T=$(curl -sS -o /dev/null -w '%{time_total}' "$BASE/health")
  HEALTH_TIMES="$HEALTH_TIMES $T"
done
AVG_HEALTH=$(echo "$HEALTH_TIMES" | python3 -c "
import sys
vals = [float(x) for x in sys.stdin.read().split()]
print(f'{sum(vals)/len(vals)*1000:.0f}')
")
echo "  Avg health-check latency: ${AVG_HEALTH}ms (5 samples)"

# 7. Summary
echo ""
echo "======================================================================"
echo "  Benchmark Summary"
echo "======================================================================"
echo "  Event ingestion:      $N events in ${INGEST_ELAPSED}s (${INGEST_RPS}/sec)"
echo "  Policy decisions:     10 decisions in ${DECISION_ELAPSED}s"
echo "  Trust-score latency:  avg ${AVG_SCORE}ms"
echo "  Decision latency:     avg ${AVG_DECISION}ms"
echo "  Health latency:       avg ${AVG_HEALTH}ms"
echo "  Queue: pending=$PENDING, completed=$NEW_COMPLETED (new)"
echo "======================================================================"
echo ""
echo "Thresholds (fail if exceeded):"
FAIL=0
if [ "$AVG_HEALTH" -gt 100 ]; then
  echo "  ❌ Health latency > 100ms"
  FAIL=1
else
  echo "  ✅ Health latency ≤ 100ms"
fi
if [ "$AVG_SCORE" -gt 500 ]; then
  echo "  ❌ Trust-score latency > 500ms"
  FAIL=1
else
  echo "  ✅ Trust-score latency ≤ 500ms"
fi
if [ "$AVG_DECISION" -gt 500 ]; then
  echo "  ❌ Decision latency > 500ms"
  FAIL=1
else
  echo "  ✅ Decision latency ≤ 500ms"
fi

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "⚠️  Some thresholds exceeded — investigate before deploying."
  exit 1
fi
echo ""
echo "✅ All performance thresholds met."
