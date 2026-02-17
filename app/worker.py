"""Webhook delivery worker process.

Polls the ``webhook_jobs`` table for pending jobs, claims them, and delivers
the webhook with HMAC signing + delivery logging.  Designed to run as a
separate process alongside the web service (e.g. a Render background worker
or a second Fly machine).

Usage::

    python -m app.worker          # run the worker loop
    python -m app.worker --once   # process one batch then exit (for testing)

Environment variables:
    WEBHOOK_WORKER_POLL_SECONDS  — poll interval (default 2.0)
    DATABASE_URL                 — same as the web service
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from app.config import settings
from app.db import get_session_factory
from app.services.webhook import (
    claim_pending_job,
    process_webhook_job,
    recover_stale_jobs,
    requeue_failed_jobs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.worker")

_shutdown = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _shutdown = True


def run_once(*, use_sqlite_claim: bool = False) -> int:
    """Process all available pending jobs in one pass.

    Returns the number of jobs processed.
    """
    from app.services.webhook import claim_pending_job_sqlite

    # Auto-select the SQLite claim path when running against SQLite.
    if settings.database_url.startswith("sqlite"):
        use_sqlite_claim = True

    session_factory = get_session_factory()
    processed = 0

    db = session_factory()
    try:
        # Maintenance: recover stale jobs + requeue failed
        recovered = recover_stale_jobs(db)
        if recovered:
            logger.info("Recovered %d stale job(s)", recovered)
        requeued = requeue_failed_jobs(db)
        if requeued:
            logger.info("Requeued %d failed job(s)", requeued)
    finally:
        db.close()

    while True:
        db = session_factory()
        try:
            if use_sqlite_claim:
                job = claim_pending_job_sqlite(db)
            else:
                job = claim_pending_job(db)

            if job is None:
                break

            logger.info("Processing job %d (tenant=%d, agent=%s)", job.id, job.tenant_id, job.agent_id)
            process_webhook_job(db, job)
            processed += 1
        except Exception:
            logger.exception("Error processing webhook job")
        finally:
            db.close()

    return processed


def run_loop() -> None:
    """Main worker loop. Runs until SIGINT/SIGTERM."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    poll_interval = settings.webhook_worker_poll_seconds
    logger.info(
        "Webhook worker started (poll_interval=%.1fs, pid=%d)",
        poll_interval, __import__("os").getpid(),
    )

    iteration = 0
    while not _shutdown:
        iteration += 1
        try:
            count = run_once()
            if count:
                logger.info("Processed %d job(s) in iteration %d", count, iteration)
        except Exception:
            logger.exception("Worker iteration %d failed", iteration)

        if not _shutdown:
            time.sleep(poll_interval)

    logger.info("Webhook worker stopped.")


def main() -> None:
    if "--once" in sys.argv:
        count = run_once()
        logger.info("Processed %d job(s) in single pass.", count)
    else:
        run_loop()


if __name__ == "__main__":
    main()
