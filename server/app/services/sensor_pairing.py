"""Pairing a laptop sensor bridge with a workout, without the user's password ever reaching the laptop.

1. The app (signed in) asks for a pairing code for its active workout: ``POST /api/workouts/{id}/sensor-code``.
2. The user types the code into the bridge, which redeems it on ``WS /ws/sensors``.
3. The server answers with a bridge token for that one workout. The bridge reconnects with the token after a
   network blip, so it doesn't need a new code. The code itself works once.

Codes expire after a few minutes and live in memory. Tokens are SIGNED (HMAC) instead of remembered, so a paired
bridge keeps working across a server restart (``--reload``, a crash, a redeploy) as long as the server's secret is
the same: it is derived from ``SENSOR_BRIDGE_SECRET``, else from ``SUPABASE_SECRET_KEY`` (see ``bridge_secret``).
Whether the workout is still active and still the user's is checked against the database on every connection,
so a token stops working the moment its workout finishes or is deleted, restart or not.

Wrong codes are limited PER CLIENT (one mistyping laptop, or one attacker, can't lock everyone else out), with a
much higher shared ceiling as a backstop against a distributed guessing flood.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

CODE_TTL_S = 10 * 60
TOKEN_TTL_S = 24 * 3600  # a token outlives any workout; the database check is what really ends it
# No 0/O, 1/I/L: the code is read off a phone and typed on a laptop. 31^8 ~ 8.5e11 codes.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8
FAILURE_WINDOW_S = 60.0
MAX_FAILURES_PER_CLIENT = 10  # a real user who mistypes a few times is unaffected
MAX_FAILURES_PER_WINDOW = 300  # all clients together: brute force at this rate against 8.5e11 codes is hopeless
MAX_TRACKED_CLIENTS = 5000
TOKEN_VERSION = "v1"


def normalize_code(raw: str) -> str:
    """'abcd-2345 ' -> 'ABCD2345'."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def bridge_secret() -> bytes | None:
    """A stable signing key, so bridge tokens survive a restart. None = a random key for this process only."""
    explicit = os.environ.get("SENSOR_BRIDGE_SECRET", "").strip()
    if explicit:
        return hashlib.sha256(b"sharkbyte-bridge|" + explicit.encode()).digest()
    supabase = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if supabase:  # derived, never the key itself: a leaked token reveals nothing about the Supabase key
        return hmac.new(supabase.encode(), b"sharkbyte sensor bridge tokens v1", hashlib.sha256).digest()
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Pairing:
    session_id: UUID
    user_id: str


class SensorPairing:
    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        self._wall = wall  # token expiry must mean the same thing after a restart: wall-clock time
        self._secret = secret or secrets.token_bytes(32)
        self._codes: dict[str, tuple[Pairing, float]] = {}  # code -> (pairing, expires_at)
        self._revoked: set[UUID] = set()  # sessions ended in THIS run (after a restart, the database check covers it)
        self._failures: dict[str, list[float]] = {}  # client -> times of its recent wrong codes
        self._all_failures: list[float] = []

    # ---- codes

    def issue_code(self, session_id: UUID, user_id: str) -> str:
        """A new code for this workout. Any earlier unused code for it stops working."""
        self._expire()
        for code, (p, _) in list(self._codes.items()):
            if p.session_id == session_id:
                del self._codes[code]
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._codes[code] = (Pairing(session_id, user_id), self._now() + CODE_TTL_S)
        return code

    def redeem(self, raw_code: str, client: str = "") -> tuple[Pairing, str] | None:
        """Code -> (pairing, bridge token). None for a wrong, used or expired code, or while this client (or everyone,
        in a flood) is locked out for guessing; a correct code is never burned by a lockout and works afterwards."""
        self._expire()
        now = self._now()
        mine = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        self._all_failures = [t for t in self._all_failures if now - t < FAILURE_WINDOW_S]
        if len(mine) >= MAX_FAILURES_PER_CLIENT or len(self._all_failures) >= MAX_FAILURES_PER_WINDOW:
            self._failures[client] = mine
            return None
        entry = self._codes.pop(normalize_code(raw_code), None)
        if entry is None:
            mine.append(now)
            self._all_failures.append(now)
            self._failures[client] = mine
            self._prune(now)
            return None
        self._failures.pop(client, None)  # a success clears this client's slate
        return entry[0], self.make_token(entry[0])

    # ---- tokens

    def make_token(self, pairing: Pairing) -> str:
        expires = int(self._wall() + TOKEN_TTL_S)
        payload = _b64(f"{pairing.session_id}|{expires}|{pairing.user_id}".encode())
        return f"{TOKEN_VERSION}.{payload}.{self._sign(payload)}"

    def check_token(self, token: str | None) -> Pairing | None:
        """The pairing a token stands for, or None if it is forged, expired, malformed or its session was revoked."""
        pairing = self._verify(token)
        if pairing is None or pairing.session_id in self._revoked:
            return None
        return pairing

    def is_revoked(self, token: str | None) -> bool:
        """A genuine token whose workout has ended: the bridge is told 'workout ended', not 'unknown pairing'."""
        pairing = self._verify(token)
        return pairing is not None and pairing.session_id in self._revoked

    def _verify(self, token: str | None) -> Pairing | None:
        try:
            version, payload, sig = (token or "").split(".")
        except ValueError:
            return None
        if version != TOKEN_VERSION or not hmac.compare_digest(sig, self._sign(payload)):
            return None
        try:
            session_text, expires_text, user_id = _unb64(payload).decode().split("|", 2)
            pairing = Pairing(UUID(session_text), user_id)
            expires = int(expires_text)
        except (ValueError, UnicodeDecodeError):
            return None
        if expires < self._wall() or not user_id:
            return None
        return pairing

    def revoke_session(self, session_id: UUID) -> None:
        """The workout ended or was deleted: its codes and tokens stop working."""
        self._codes = {c: e for c, e in self._codes.items() if e[0].session_id != session_id}
        self._revoked.add(session_id)

    # ---- internals

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()[:24])

    def _expire(self) -> None:
        now = self._now()
        self._codes = {c: e for c, e in self._codes.items() if e[1] > now}

    def _prune(self, now: float) -> None:
        if len(self._failures) > MAX_TRACKED_CLIENTS:
            self._failures = {c: ts for c, ts in self._failures.items() if ts and now - ts[-1] < FAILURE_WINDOW_S}
