#!/usr/bin/env bash
# ============================================================================
# Agent Trust Bureau — Webhook Worker
#
# Runs alembic migrations (idempotent) then starts the webhook delivery
# worker loop.  Designed to run as a background worker on Render / Fly.
# ============================================================================
set -euo pipefail

echo "Running database migrations..."
alembic upgrade head

echo "Starting webhook worker..."
exec python -m app.worker
