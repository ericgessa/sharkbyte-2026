"""Live 1v1 Rep Racer rooms: an in-memory state machine, no sockets, no database, no sleeping.

The server is authoritative for everything that decides a race: who is in the room, when the countdown ends, when the
race starts and stops, how many reps each player has, and who won. Each player's browser runs MediaPipe locally and
sends only joint ANGLES; the server feeds them to the same rep counter a normal workout uses, so a client cannot claim
a rep that its angles did not produce (there is no "I did a rep" message to forge or replay).

The room never talks to anyone itself. Every method that changes something appends events to ``outbox`` (as
``(user_id or None for everyone, event)``) and the WebSocket layer sends them, so the whole race can be tested with a
fake clock. A driver calls ``tick()`` a few times a second to move the countdown/timer/forfeit rules forward.

    LOBBY --(2 players connected + both ready)--> COUNTDOWN --(3 s)--> RACING --(target | time | forfeit)--> FINISHED
      ^                                              |                                                         |
      +--------------(a player drops)----------------+------------------------(both ask for a rematch)--------+
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.schemas.games import SCHEMA_VERSION, RoomPose
from app.services.coaching import RuleCoach
from app.services.exercises import Exercise
from app.services.rep_counter import PoseSample

logger = logging.getLogger(__name__)

LOBBY, COUNTDOWN, RACING, FINISHED = "lobby", "countdown", "racing", "finished"
MAX_PLAYERS = 2
COUNTDOWN_S = 3.0
RECONNECT_GRACE_S = 15.0  # how long a dropped player has to come back during a race before they forfeit
IDLE_TTL_S = 30 * 60  # a room nobody touched for this long is evicted
FINISHED_TTL_S = 10 * 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easy to read out loud
CODE_LENGTH = 6
CODE_RE = re.compile(r"^[A-Z0-9]{4,8}$")
MAX_ROOMS = 500


class RoomError(Exception):
    """A request the room refuses. ``status`` is the HTTP status the REST layer answers with."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def normalize_code(value: str) -> str:
    code = (value or "").strip().upper()
    if not CODE_RE.match(code):
        raise RoomError(404, "room_not_found", "no such room")
    return code


@dataclass(eq=False)
class Player:
    user_id: str
    name: str
    ready: bool = False
    connected: bool = False
    socket: Any = None
    counter: Any = None
    coach: RuleCoach | None = None
    reps: int = 0
    rep_ms: list[int] = field(default_factory=list)  # ms from the start signal to each counted rep
    rep_metrics: list[dict[str, Any]] = field(default_factory=list)
    rematch: bool = False
    forfeited: bool = False
    disconnected_at: float | None = None
    tracking: str = "ok"
    frames: int = 0

    @property
    def last_rep_ms(self) -> int | None:
        return self.rep_ms[-1] if self.rep_ms else None


class Room:
    def __init__(
        self,
        code: str,
        host_id: str,
        exercise: Exercise,
        target_reps: int,
        time_limit_s: int,
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.code, self.host_id, self.exercise = code, host_id, exercise
        self.target_reps, self.time_limit_s = target_reps, time_limit_s
        self._now, self._wall = now, wall
        self.players: dict[str, Player] = {}
        self.status = LOBBY
        self.race_id = 0  # bumps on every rematch; lets clients and the database tell races apart
        self.countdown_ends: float | None = None
        self.race_started: float | None = None
        self.race_started_wall: datetime | None = None
        self.winner_id: str | None = None
        self.end_reason: str | None = None
        self.results: list[dict[str, Any]] = []
        self.persist_pending = False  # set when a race ends; the WebSocket layer saves it once and clears this
        self.outbox: list[tuple[str | None, dict[str, Any]]] = []
        self.last_activity = now()
        self.finished_at: float | None = None
        self.closed = False

    # ------------------------------------------------------------------ helpers

    def touch(self) -> None:
        self.last_activity = self._now()

    def _emit(self, event: dict[str, Any], to: str | None = None) -> None:
        self.outbox.append((to, {"schema_version": SCHEMA_VERSION, "room": self.code, **event}))

    def drain(self) -> list[tuple[str | None, dict[str, Any]]]:
        out, self.outbox = self.outbox, []
        return out

    def _other(self, user_id: str) -> Player | None:
        return next((p for p in self.players.values() if p.user_id != user_id), None)

    def elapsed_ms(self) -> int:
        return max(0, round((self._now() - self.race_started) * 1000)) if self.race_started is not None else 0

    def starts_in_ms(self) -> int | None:
        if self.status != COUNTDOWN or self.countdown_ends is None:
            return None
        return max(0, round((self.countdown_ends - self._now()) * 1000))

    def snapshot(self) -> dict[str, Any]:
        """Everything a client needs to draw the room. Times are RELATIVE (ms from now / since the start) so the
        two laptops' clocks never have to agree."""
        remaining = None
        if self.status == RACING:
            remaining = max(0, self.time_limit_s * 1000 - self.elapsed_ms())
        return {
            "type": "room_state",
            "race_id": self.race_id,
            "exercise": self.exercise.slug,
            "target_reps": self.target_reps,
            "time_limit_s": self.time_limit_s,
            "status": self.status,
            "host_id": self.host_id,
            "starts_in_ms": self.starts_in_ms(),
            "elapsed_ms": self.elapsed_ms() if self.status in (RACING, FINISHED) else 0,
            "remaining_ms": remaining,
            "players": [
                {
                    "user_id": p.user_id,
                    "name": p.name,
                    "ready": p.ready,
                    "connected": p.connected,
                    "reps": p.reps,
                    "tracking": p.tracking,
                    "rematch": p.rematch,
                }
                for p in self.players.values()
            ],
            "winner_id": self.winner_id,
            "end_reason": self.end_reason,
            "results": self.results,
        }

    def _state(self) -> None:
        self._emit(self.snapshot())

    # ------------------------------------------------------------------ membership

    def join(self, user_id: str, name: str) -> Player:
        existing = self.players.get(user_id)
        if existing is not None:
            return existing  # joining again (a page reload) is not an error
        if self.status != LOBBY:
            raise RoomError(409, "race_in_progress", "this race has already started")
        if len(self.players) >= MAX_PLAYERS:
            raise RoomError(409, "room_full", "this room already has two players")
        player = Player(user_id=user_id, name=name or "Player")
        self.players[user_id] = player
        self.touch()
        self._state()
        return player

    def connect(self, user_id: str, socket: Any) -> Any:
        """Attach a live socket to ``user_id``'s seat (a returning player simply takes it back). Returns the
        previous socket, if any, so the caller can close it."""
        player = self.players.get(user_id)
        if player is None:
            raise RoomError(404, "not_in_room", "join the room first")
        old = player.socket
        player.socket, player.connected, player.disconnected_at = socket, True, None
        self.touch()
        self._state()
        return old

    def disconnect(self, user_id: str, socket: Any) -> None:
        player = self.players.get(user_id)
        if player is None or player.socket is not socket:  # already replaced by a newer connection
            return
        player.socket, player.connected, player.disconnected_at = None, False, self._now()
        self.touch()
        if self.status == LOBBY:
            player.ready = False
        elif self.status == COUNTDOWN:
            self._back_to_lobby()  # nobody races an opponent who is not there
        self._state()

    def leave(self, user_id: str) -> None:
        """The player chose to leave (button) or was kicked out of a stale room."""
        player = self.players.get(user_id)
        if player is None:
            return
        self.touch()
        if self.status == RACING:
            self._finish("forfeit", winner=self._other(user_id), forfeiter=player)
            return
        del self.players[user_id]
        if self.status == COUNTDOWN:
            self._back_to_lobby()
        if not self.players:
            self.closed = True
            return
        if self.host_id == user_id:
            self.host_id = next(iter(self.players))
        if self.status == FINISHED:
            for p in self.players.values():
                p.rematch = False
        self._state()

    # ------------------------------------------------------------------ lobby and countdown

    def set_ready(self, user_id: str, ready: bool) -> None:
        player = self.players.get(user_id)
        if player is None:
            raise RoomError(404, "not_in_room", "join the room first")
        if self.status != LOBBY:
            return  # a late "ready" after the countdown began changes nothing
        player.ready = ready
        self.touch()
        everyone_ready = len(self.players) == MAX_PLAYERS and all(p.ready and p.connected for p in self.players.values())
        if everyone_ready:
            self.status = COUNTDOWN
            self.countdown_ends = self._now() + COUNTDOWN_S
            logger.info("room %s countdown started", self.code)
        self._state()

    def _back_to_lobby(self) -> None:
        self.status = LOBBY
        self.countdown_ends = None
        for p in self.players.values():
            p.ready = False

    def _start_race(self) -> None:
        self.status = RACING
        self.race_started = self._now()
        self.race_started_wall = self._wall()
        self.countdown_ends = None
        for p in self.players.values():  # a fresh counter each race: it must see the player standing before it counts
            p.counter = self.exercise.make_counter(0)
            p.coach = RuleCoach(joint=self.exercise.joint)
            p.reps, p.rep_ms, p.rep_metrics, p.tracking, p.frames = 0, [], [], "ok", 0
            p.forfeited = False
        self._emit({"type": "race_started", "race_id": self.race_id, "time_limit_s": self.time_limit_s, "target_reps": self.target_reps})
        self._state()
        logger.info("room %s race %d started (%s, target %d)", self.code, self.race_id, self.exercise.slug, self.target_reps)

    # ------------------------------------------------------------------ the race

    def tick(self) -> bool:
        """Advance time-based rules. Returns True if the state changed. Safe to call as often as you like."""
        now = self._now()
        if self.status == COUNTDOWN and self.countdown_ends is not None and now >= self.countdown_ends:
            self._start_race()
            return True
        if self.status == RACING:
            if self.elapsed_ms() >= self.time_limit_s * 1000:
                self._finish("time_up")
                return True
            gone = [p for p in self.players.values() if not p.connected and p.disconnected_at is not None and now - p.disconnected_at >= RECONNECT_GRACE_S]
            if gone:
                connected = [p for p in self.players.values() if p.connected]
                self._finish("forfeit", winner=connected[0] if connected else None, forfeiter=gone[0])
                return True
        return False

    def on_pose(self, user_id: str, msg: RoomPose) -> None:
        """One frame of angles from a player. Counts only while racing."""
        player = self.players.get(user_id)
        if player is None or self.status != RACING or player.counter is None:
            return
        self.touch()
        d = msg.data
        player.frames += 1
        result = player.counter.update(
            PoseSample(
                sequence=msg.sequence,
                timestamp_ms=msg.timestamp_ms,
                confidence=d.landmark_confidence,
                left_knee_deg=d.left_knee_angle_deg,
                right_knee_deg=d.right_knee_angle_deg,
                left_elbow_deg=d.left_elbow_angle_deg,
                right_elbow_deg=d.right_elbow_angle_deg,
            )
        )
        if result.tracking_changed is not None:
            player.tracking = "lost" if result.tracking_changed else "ok"
            self._state()
        rep = result.rep
        if rep is None:
            return
        elapsed = self.elapsed_ms()
        player.reps += 1  # the counter numbers reps itself; a replayed frame can never produce a second one
        player.rep_ms.append(elapsed)
        assert self.race_started_wall is not None
        player.rep_metrics.append(
            {
                "rep_number": player.reps,
                "min_angle_deg": rep.min_angle_deg,
                "joint": rep.joint,
                "descent_ms": rep.descent_ms,
                "ascent_ms": rep.ascent_ms,
                "duration_ms": rep.duration_ms,
                "max_asymmetry_deg": rep.max_asymmetry_deg,
                "ended_at": (self.race_started_wall + timedelta(milliseconds=elapsed)).isoformat(),
            }
        )
        self._emit({"type": "rep_counted", "user_id": user_id, "reps": player.reps, "elapsed_ms": elapsed})
        decision = player.coach.observe(rep) if player.coach is not None else None
        if decision is not None:
            self._emit(
                {"type": "coaching_feedback", "rep_count": decision.rep_number, "issue_code": decision.issue_code, "message": decision.message, "source": decision.source},
                to=user_id,
            )
        if player.reps >= self.target_reps:
            self._finish("target_reached", winner=player)
        else:
            self._state()

    def _finish(self, reason: str, winner: Player | None = None, forfeiter: Player | None = None) -> None:
        if self.status != RACING:
            return
        players = list(self.players.values())
        if reason == "time_up":
            ranked = sorted(players, key=lambda p: (-p.reps, p.last_rep_ms if p.last_rep_ms is not None else 10**9))
            tied = len(ranked) == 2 and ranked[0].reps == ranked[1].reps and ranked[0].last_rep_ms == ranked[1].last_rep_ms
            winner = None if tied or not ranked else ranked[0]
        if forfeiter is not None:
            forfeiter.forfeited = True
        self.status, self.end_reason = FINISHED, reason
        self.winner_id = winner.user_id if winner else None
        self.finished_at = self._now()
        self.results = []
        for p in sorted(players, key=lambda p: (p is not winner, -p.reps)):
            if winner is None:
                outcome = "draw"
            elif p is winner:
                outcome = "win"
            else:
                outcome = "forfeit" if p.forfeited else "loss"
            self.results.append(
                {"user_id": p.user_id, "name": p.name, "reps": p.reps, "last_rep_ms": p.last_rep_ms, "placement": 0, "result": outcome}
            )
        for place, r in enumerate(self.results, start=1):
            r["placement"] = 1 if r["result"] in ("win", "draw") else 2
        for p in players:
            p.rematch = False
        self.persist_pending = True
        self._emit(
            {
                "type": "race_finished",
                "race_id": self.race_id,
                "winner_id": self.winner_id,
                "end_reason": reason,
                "elapsed_ms": self.elapsed_ms(),
                "results": self.results,
            }
        )
        self._state()
        logger.info("room %s race %d finished: %s winner=%s", self.code, self.race_id, reason, self.winner_id)

    # ------------------------------------------------------------------ rematch

    def request_rematch(self, user_id: str) -> None:
        player = self.players.get(user_id)
        if player is None:
            raise RoomError(404, "not_in_room", "join the room first")
        if self.status != FINISHED:
            return
        player.rematch = True
        self.touch()
        if len(self.players) == MAX_PLAYERS and all(p.rematch and p.connected for p in self.players.values()):
            self.race_id += 1
            self.status, self.winner_id, self.end_reason, self.results = LOBBY, None, None, []
            self.race_started = self.race_started_wall = self.finished_at = None
            for p in self.players.values():
                p.ready = p.rematch = False
                p.reps, p.rep_ms, p.rep_metrics, p.counter, p.coach, p.tracking = 0, [], [], None, None, "ok"
        self._state()

    # ------------------------------------------------------------------ persistence payload

    def finished_payload(self) -> dict[str, Any]:
        """What to save once a race is over (see ``api/games.py``)."""
        assert self.status == FINISHED and self.race_started_wall is not None
        return {
            "code": self.code,
            "host_id": self.host_id,
            "exercise": self.exercise.slug,
            "target_reps": self.target_reps,
            "time_limit_s": self.time_limit_s,
            "winner_id": self.winner_id,
            "end_reason": self.end_reason,
            "started_at": self.race_started_wall,
            "players": [
                {
                    **r,
                    "rep_ms": list(self.players[r["user_id"]].rep_ms) if r["user_id"] in self.players else [],
                    "rep_metrics": list(self.players[r["user_id"]].rep_metrics) if r["user_id"] in self.players else [],
                }
                for r in self.results
            ],
            "duration_ms": self.elapsed_ms() if self.finished_at is None else round((self.finished_at - (self.race_started or 0)) * 1000),
        }


class GameRooms:
    """All live rooms. Small and in-memory; a server restart ends the races in progress (nothing is lost that was
    already saved, and players simply create a new room)."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._rooms: dict[str, Room] = {}
        self._now = now

    def __len__(self) -> int:
        return len(self._rooms)

    def get(self, code: str) -> Room | None:
        return self._rooms.get(code)

    def for_user(self, user_id: str) -> list[Room]:
        return [r for r in self._rooms.values() if user_id in r.players]

    def create(self, host_id: str, host_name: str, exercise: Exercise, target_reps: int, time_limit_s: int) -> Room:
        for old in self.for_user(host_id):
            if old.status in (RACING, COUNTDOWN):
                raise RoomError(409, "already_in_a_race", "finish or leave your current race first")
            old.leave(host_id)
        self.sweep()
        if len(self._rooms) >= MAX_ROOMS:
            raise RoomError(503, "too_many_rooms", "the server is busy; try again in a minute")
        for _ in range(50):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._rooms:
                break
        else:  # astronomically unlikely
            raise RoomError(503, "too_many_rooms", "could not make a room code; try again")
        room = Room(code, host_id, exercise, target_reps, time_limit_s, now=self._now)
        room.join(host_id, host_name)
        self._rooms[code] = room
        logger.info("room %s created by %s (%s, target %d, %ds)", code, host_id, exercise.slug, target_reps, time_limit_s)
        return room

    def join(self, code: str, user_id: str, name: str) -> Room:
        room = self._rooms.get(normalize_code(code))
        if room is None or room.closed:
            raise RoomError(404, "room_not_found", "no such room")
        for other in self.for_user(user_id):
            if other is not room:
                if other.status in (RACING, COUNTDOWN):
                    raise RoomError(409, "already_in_a_race", "finish or leave your current race first")
                other.leave(user_id)
        room.join(user_id, name)
        return room

    def remove(self, code: str) -> None:
        self._rooms.pop(code, None)

    def sweep(self) -> int:
        """Drop closed, abandoned and long-finished rooms. Returns how many were removed."""
        now = self._now()
        stale = [
            code
            for code, r in self._rooms.items()
            if r.closed
            or now - r.last_activity > IDLE_TTL_S
            or (r.status == FINISHED and r.finished_at is not None and now - r.finished_at > FINISHED_TTL_S and not any(p.connected for p in r.players.values()))
        ]
        for code in stale:
            del self._rooms[code]
        return len(stale)
