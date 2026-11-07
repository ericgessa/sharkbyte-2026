"""In-memory workout sessions (Phase 1).

Phase 2 replaces creation/lookup with Supabase-backed, ownership-checked
sessions; the live ``SquatRepCounter`` stays in memory either way, because the
real-time loop must not wait on the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.services.rep_counter import SquatRepCounter

logger = logging.getLogger(__name__)


@dataclass
class WorkoutSession:
    exercise: str
    id: UUID = field(default_factory=uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"
    counter: SquatRepCounter = field(default_factory=SquatRepCounter)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[UUID, WorkoutSession] = {}

    def create(self, exercise: str) -> WorkoutSession:
        session = WorkoutSession(exercise=exercise)
        self._sessions[session.id] = session
        logger.info("session created id=%s exercise=%s", session.id, exercise)
        return session

    def get(self, session_id: UUID) -> WorkoutSession | None:
        return self._sessions.get(session_id)
