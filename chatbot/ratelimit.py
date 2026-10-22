"""Fixed-window rate limiting backed by the Django cache.

Use a shared cache (``DJANGO_CACHE_URL``, Redis) in production so limits apply
across all worker processes.
"""

import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)


def check_rate_limit(key: str, limit: int, window: int = 60) -> tuple[bool, int]:
    """Return ``(allowed, retry_after_seconds)``."""
    if limit <= 0:
        return True, 0
    now = time.time()
    bucket = int(now // window)
    cache_key = f"t2s:rl:{key}:{bucket}"
    try:
        cache.add(cache_key, 0, window + 5)
        count = cache.incr(cache_key)
    except Exception as e:
        # Fail open: a cache outage should not take the whole app down.
        logger.warning("Rate limit cache unavailable: %s", e)
        return True, 0
    if count > limit:
        return False, max(1, int(window - (now % window)))
    return True, 0
