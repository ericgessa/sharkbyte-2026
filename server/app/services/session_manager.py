"""Live, in-memory state for workouts that are currently streaming.

The database row (Supabase) is the permanent record; this holds only what the
real-time loop needs: the rep counter and any reps that failed to save. If the
server restarts, a reconnecting client gets a fresh counter seeded from the
number of reps already saved, so rep numbers keep counting up.

Two housekeeping rules keep it healthy on real phones:
  * one socket per session: a reconnecting app takes the session over from a stale (half-open) socket;
  * abandoned sessions are evicted after a period with no socket, once their unsaved writes are flushed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.services import persistence
from app.services.coaching import RuleCoach
from app.services.ingest import ClockAnchors
from app.services.persistence import Write
from app.services.posture import PostureCoach
from app.services.spine_form import SpineFormCoach
from app.services.exercises import DEFAULT_EXERCISE, Exercise, find

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TTL_S = 30 * 60  # evict this long after the last socket went away
HARD_TTL_S = 6 * 60 * 60  # ...and give up on unsaved writes after this long
DEFAULT_SWEEP_INTERVAL_S = 60.0


@dataclass
class LiveSession:
    session_id: UUID
    user_id: str
    counter: Any  # SquatRepCounter / PushUpRepCounter, or NoopRepCounter for exercises without analysis
    exercise: Exercise | None = None
    coach: RuleCoach = field(default_factory=RuleCoach)
    # Upper-back IMU posture coaches, one per ESP32 device_id (created on the first sensor_update; see websocket.py).
    posture: dict[str, PostureCoach] = field(default_factory=dict)
    # Back shape per counted rep, from the wearable's section angles (created on the first spine sample).
    spine: SpineFormCoach | None = None
    last_cue_at: float | None = None  # monotonic time of the last coaching cue sent (any source), to space them out
    clock: ClockAnchors = field(default_factory=ClockAnchors)  # device/camera clocks -> server time
    # Writes that failed and must be retried when finishing (see services/persistence.py).
    pending_writes: list[Write] = field(default_factory=list)
    # Background saves in flight; finishing waits for these so no rep is missed.
    save_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    finished: bool = False
    paused: bool = False  # frames are ignored while paused
    pain_reported: bool = False
    socket: Any = None  # the one WebSocket currently driving this session, if any
    sensor_socket: Any = None  # the laptop bridge streaming the wearable for this session, if any
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class LiveSessions:
    def __init__(self) -> None:
        self._live: dict[UUID, LiveSession] = {}

    def get(self, session_id: UUID) -> LiveSession | None:
        return self._live.get(session_id)

    def get_or_create(
        self, session_id: UUID, user_id: str, saved_reps: int = 0, exercise: Exercise | None = None
    ) -> LiveSession:
        live = self._live.get(session_id)
        if live is None:
            exercise = exercise or find(DEFAULT_EXERCISE)
            live = LiveSession(
                session_id, user_id, exercise.make_counter(saved_reps), exercise=exercise, coach=RuleCoach(joint=exercise.joint)
            )
            self._live[session_id] = live
        return live

    def remove(self, session_id: UUID) -> None:
        self._live.pop(session_id, None)

    def for_user(self, user_id: str) -> list[LiveSession]:
        return [s for s in self._live.values() if s.user_id == user_id]

    def __len__(self) -> int:
        return len(self._live)

    def idle(self, ttl_s: float, now: float | None = None) -> list[LiveSession]:
        """Sessions with no socket (app or sensor bridge), nothing in flight, and no activity for ``ttl_s`` seconds."""
        now = time.monotonic() if now is None else now
        return [
            s for s in self._live.values()
            if s.socket is None and s.sensor_socket is None and not s.save_tasks and now - s.last_activity >= ttl_s
        ]


async def sweep_once(
    live: LiveSessions,
    store: Any,
    ttl_s: float = DEFAULT_IDLE_TTL_S,
    hard_ttl_s: float = HARD_TTL_S,
    now: float | None = None,
) -> int:
    """Evict abandoned sessions. A session with writes that still fail to save is kept (and retried
    next sweep) until ``hard_ttl_s``, so a database blip does not silently lose reps. Returns the number evicted."""
    now = time.monotonic() if now is None else now
    evicted = 0
    for s in live.idle(ttl_s, now):
        remaining: list[Write] = []
        for write in s.pending_writes:
            try:
                await asyncio.to_thread(persistence.perform, store, write)
            except Exception:
                remaining.append(write)
        s.pending_writes = remaining
        if remaining and now - s.last_activity < hard_ttl_s:
            continue  # keep it; try again next sweep
        if remaining:
            logger.warning("evicting session %s after %.0f s with %d unsaved writes", s.session_id, hard_ttl_s, len(remaining))
        live.remove(s.session_id)
        evicted += 1
        logger.info("evicted idle live session %s", s.session_id)
    return evicted


async def sweep_loop(live: LiveSessions, store: Any, ttl_s: float, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await sweep_once(live, store, ttl_s)
        except Exception:  # housekeeping must never take the server down
            logger.exception("live-session sweep failed")
