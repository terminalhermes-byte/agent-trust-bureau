from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings


_windows: dict[str, deque[float]] = defaultdict(deque)


def check_score_rate_limit(agent_id: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    limit = settings.score_rate_limit_per_minute
    if limit <= 0:
        return True

    now = time.monotonic()
    window = _windows[agent_id]

    # Purge entries older than 60s
    while window and window[0] <= now - 60:
        window.popleft()

    if len(window) >= limit:
        return False

    window.append(now)
    return True


def reset_rate_limits() -> None:
    """Clear all windows. Useful for testing."""
    _windows.clear()
