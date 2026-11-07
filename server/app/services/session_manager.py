"""Live, in-memory state for workouts that are currently streaming.

The database row (Supabase) is the permanent record; this holds only what the
real-time loop needs: the rep counter and any reps that failed to save. If the
server restarts, a reconnecting client gets a fresh counter seeded from the
number of reps already saved, so rep numbers keep counting up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.services.coaching import RuleCoach
from app.services.persistence import Write
from app.services.rep_counter import SquatRepCounter


@dataclass
class LiveSession:
    session_id: UUID
    user_id: str
    counter: SquatRepCounter
    coach: RuleCoach = field(default_factory=RuleCoach)
    # Writes that failed and must be retried when finishing (see services/persistence.py).
    pending_writes: list[Write] = field(default_factory=list)
    # Background saves in flight; finishing waits for these so no rep is missed.
    save_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    finished: bool = False
    paused: bool = False  # frames are ignored while paused
    pain_reported: bool = False


class LiveSessions:
    def __init__(self) -> None:
        self._live: dict[UUID, LiveSession] = {}

    def get(self, session_id: UUID) -> LiveSession | None:
        return self._live.get(session_id)

    def get_or_create(self, session_id: UUID, user_id: str, saved_reps: int = 0) -> LiveSession:
        live = self._live.get(session_id)
        if live is None:
            live = LiveSession(session_id, user_id, SquatRepCounter(rep_count=saved_reps))
            self._live[session_id] = live
        return live

    def remove(self, session_id: UUID) -> None:
        self._live.pop(session_id, None)
