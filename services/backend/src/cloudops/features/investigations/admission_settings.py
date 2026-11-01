"""E04 deferred-investigation bound. Shared job capacity (queue_limit) stays in shared config."""

import os
from functools import lru_cache


@lru_cache
def deferred_run_limit() -> int:
    raw = os.environ.get("DEFERRED_RUN_LIMIT", "100")
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= 1000:
        raise RuntimeError("DEFERRED_RUN_LIMIT must be an integer from 1 to 1000.")
    return value
