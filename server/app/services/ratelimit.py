"""A tiny token bucket: ``allow()`` is True while the sender stays within the sustained rate (plus a burst)."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, now: Callable[[], float] = time.monotonic) -> None:
        self._rate, self._burst, self._now = rate_per_s, float(burst), now
        self._tokens, self._last = float(burst), now()

    def allow(self) -> bool:
        t = self._now()
        self._tokens = min(self._burst, self._tokens + (t - self._last) * self._rate)
        self._last = t
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False
