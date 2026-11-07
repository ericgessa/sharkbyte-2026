"""A tiny token bucket: ``allow()`` is True while the sender stays within the sustained rate (plus a burst)."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
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


class KeyedLimiter:
    """One token bucket per key (e.g. per user id), with bounded memory: the least recently seen keys are
    forgotten first, so a flood of distinct keys cannot grow it without limit."""

    def __init__(self, rate_per_s: float, burst: int, max_keys: int = 10_000, now: Callable[[], float] = time.monotonic) -> None:
        self._rate, self._burst, self._max_keys, self._now = rate_per_s, burst, max_keys, now
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def allow(self, key: str) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(self._rate, self._burst, self._now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return bucket.allow()

    @property
    def retry_after_s(self) -> int:
        """Whole seconds after which a rejected caller will have a token again (for the Retry-After header)."""
        return max(1, math.ceil(1.0 / self._rate)) if self._rate > 0 else 60
