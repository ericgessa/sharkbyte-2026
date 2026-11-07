"""Turn a Supabase access token (JWT) into a user id, or None.

Verification is delegated to Supabase Auth (``auth.get_user(jwt)``), which also catches revoked sessions and works
with whichever signing keys the project uses. That is a network call, so successful results are cached briefly:
a busy app would otherwise make one extra round trip to Supabase on EVERY API call.

Cache rules (security trade-offs, deliberately conservative):
  * only successes are cached; an invalid token is re-checked every time;
  * entries live at most ``ttl_s`` (default 60 s) and never past the token's own ``exp``;
  * keys are SHA-256 hashes: the token itself is never kept in the cache;
  * so a revoked session or deleted user may still pass for up to ``ttl_s`` seconds (account deletion
    calls ``invalidate_user`` to close that window immediately for the deleted user).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable

from supabase import Client

from app.services.database import get_client

logger = logging.getLogger(__name__)


def _token_expiry(token: str) -> float | None:
    """The ``exp`` claim (epoch seconds) read WITHOUT verifying the signature. Used only to make the cache
    lifetime shorter, never to trust the token: Supabase has already verified it by the time this matters."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except (IndexError, ValueError, TypeError):
        return None


class SupabaseAuth:
    def __init__(
        self,
        client: Client | None = None,
        ttl_s: float = 60.0,
        max_entries: int = 1000,
        now: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._ttl_s, self._max_entries, self._now, self._wall = ttl_s, max_entries, now, wall_clock
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()  # sha256(token) -> (user_id, expires_at)

    def verify(self, access_token: str) -> str | None:
        key = hashlib.sha256(access_token.encode()).hexdigest()
        hit = self._cache.get(key)
        if hit is not None:
            if self._now() < hit[1]:
                self._cache.move_to_end(key)
                return hit[0]
            del self._cache[key]

        client = self._client or get_client()
        try:
            res = client.auth.get_user(access_token)
        except Exception as exc:  # invalid/expired token, network error...
            logger.info("token rejected: %s", type(exc).__name__)
            return None
        user = getattr(res, "user", None)
        if user is None:
            return None
        user_id = str(user.id)
        self._remember(key, user_id, access_token)
        return user_id

    def invalidate_user(self, user_id: str) -> None:
        """Forget every cached token of this user (e.g. right after their account is deleted)."""
        for key in [k for k, (uid, _) in self._cache.items() if uid == user_id]:
            del self._cache[key]

    def _remember(self, key: str, user_id: str, token: str) -> None:
        ttl = self._ttl_s
        exp = _token_expiry(token)
        if exp is not None:
            ttl = min(ttl, exp - self._wall() - 5.0)  # stay clear of the very end of the token's life
        if ttl <= 0:
            return
        self._cache[key] = (user_id, self._now() + ttl)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
