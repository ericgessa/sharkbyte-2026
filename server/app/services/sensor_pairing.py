"""Pairing a laptop sensor bridge with a workout, without the user's password ever reaching the laptop.

1. The app (signed in) asks for a pairing code for its active workout: ``POST /api/workouts/{id}/sensor-code``.
2. The user types the code into the bridge, which redeems it on ``WS /ws/sensors``.
3. The server answers with a bridge token for that one workout. The bridge reconnects with the token after a
   network blip, so it doesn't need a new code. The code itself works once.

Codes expire after a few minutes. Tokens live in memory: they end when the workout ends or the server restarts
(then the bridge asks for a new code). Whether the workout is still active and still the user's is checked
against the database on every connection, not here.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

CODE_TTL_S = 10 * 60
# No 0/O, 1/I/L: the code is read off a phone and typed on a laptop. 31^8 ~ 8.5e11 codes.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8
# Wrong codes, across all callers. Brute force at this rate against 8.5e11 codes is hopeless; a real user
# who mistypes a few times is unaffected.
FAILURE_WINDOW_S, MAX_FAILURES_PER_WINDOW = 60.0, 30


def normalize_code(raw: str) -> str:
    """'abcd-2345 ' -> 'ABCD2345'."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


@dataclass(frozen=True)
class Pairing:
    session_id: UUID
    user_id: str


class SensorPairing:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._codes: dict[str, tuple[Pairing, float]] = {}  # code -> (pairing, expires_at)
        self._tokens: dict[str, Pairing] = {}
        self._failures: list[float] = []  # times of recent wrong codes

    def issue_code(self, session_id: UUID, user_id: str) -> str:
        """A new code for this workout. Any earlier unused code for it stops working."""
        self._expire()
        for code, (p, _) in list(self._codes.items()):
            if p.session_id == session_id:
                del self._codes[code]
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._codes[code] = (Pairing(session_id, user_id), self._now() + CODE_TTL_S)
        return code

    def redeem(self, raw_code: str) -> tuple[Pairing, str] | None:
        """Code -> (pairing, bridge token). None for a wrong, used or expired code, or during a guessing flood
        (then even a correct code is refused for a moment, and stays usable afterwards)."""
        self._expire()
        now = self._now()
        self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW_S]
        if len(self._failures) >= MAX_FAILURES_PER_WINDOW:
            return None
        entry = self._codes.pop(normalize_code(raw_code), None)
        if entry is None:
            self._failures.append(now)
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = entry[0]
        return entry[0], token

    def check_token(self, token: str) -> Pairing | None:
        return self._tokens.get(token)

    def revoke_session(self, session_id: UUID) -> None:
        """The workout ended or was deleted: its codes and tokens stop working."""
        self._codes = {c: e for c, e in self._codes.items() if e[0].session_id != session_id}
        self._tokens = {t: p for t, p in self._tokens.items() if p.session_id != session_id}

    def _expire(self) -> None:
        now = self._now()
        self._codes = {c: e for c, e in self._codes.items() if e[1] > now}
