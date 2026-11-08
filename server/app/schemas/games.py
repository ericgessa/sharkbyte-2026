"""Wire-format models for GRIT Games (Rep Racer). Source of truth for humans: docs/grit-games.md."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.events import PoseData
from app.services.exercises import UnknownExerciseError
from app.services.exercises import resolve as resolve_exercise

SCHEMA_VERSION = 1

GAME_EXERCISES = ("squat", "push-up")  # the exercises the server can count reps for
Game = Literal["rep_racer"]
Mode = Literal["solo", "personal_best", "multiplayer"]
# Over REST a player can only save their own solo or personal-best round. A multiplayer result is written by the
# server itself when a race finishes; a client can never claim one.
PostedMode = Literal["solo", "personal_best"]

MAX_TARGET_REPS = 100
MIN_TIME_LIMIT_S, MAX_TIME_LIMIT_S = 10, 600


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def game_exercise(value: object) -> str:
    """Any spelling of squat / push-up -> the canonical slug. Anything else is rejected (no counter for it)."""
    try:
        slug = resolve_exercise(value).slug
    except UnknownExerciseError as exc:
        raise ValueError(str(exc)) from None
    if slug not in GAME_EXERCISES:
        raise ValueError(f"GRIT Games supports {' and '.join(GAME_EXERCISES)} for now")
    return slug


# ------------------------------------------------------------------ results (REST)


class GameResultIn(_Strict):
    """A finished camera-tracked round. Rep counts, rep times and "did you reach the target" are NOT taken from
    here: the server derives them from the workout (``workout_session_id``) that its own rep counter scored."""

    game: Game
    mode: PostedMode
    exercise: str
    workout_session_id: UUID
    duration_ms: int = Field(ge=0, le=3_600_000)
    target_reps: int | None = Field(default=None, ge=1, le=MAX_TARGET_REPS)
    time_limit_s: int | None = Field(default=None, ge=MIN_TIME_LIMIT_S, le=MAX_TIME_LIMIT_S)
    form_feedback: dict[str, int] = Field(default_factory=dict, max_length=20)

    @field_validator("exercise")
    @classmethod
    def _exercise(cls, v: str) -> str:
        return game_exercise(v)

    @field_validator("form_feedback")
    @classmethod
    def _feedback(cls, v: dict[str, int]) -> dict[str, int]:
        for key, count in v.items():
            if not 0 < len(key) <= 80 or not 0 <= count <= 100_000:
                raise ValueError("form_feedback keys must be 1-80 characters and counts 0-100000")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.target_reps is None or self.time_limit_s is None:
            raise ValueError("Rep Racer results need target_reps and time_limit_s")


class PersonalBestComparison(BaseModel):
    """How this round compares with the player's previous best (Rep Racer). All values come from saved results."""

    has_previous: bool
    previous_duration_ms: int | None = None
    previous_reps: int | None = None
    is_new_best: bool = False
    # negative = faster than before. None when there is nothing to compare or this round did not finish.
    delta_ms: int | None = None
    reps_delta: int | None = None


class GameResultOut(BaseModel):
    id: UUID
    game: Game
    mode: Mode
    exercise: str
    valid_reps: int
    score: int
    duration_ms: int
    target_reps: int | None = None
    time_limit_s: int | None = None
    completed: bool
    form_feedback: dict[str, int] = Field(default_factory=dict)
    rep_times_ms: list[int] = Field(default_factory=list)
    workout_session_id: UUID | None = None
    room_id: UUID | None = None
    created_at: datetime
    personal_best: PersonalBestComparison | None = None


class BestOut(BaseModel):
    """The ghost source for Personal Best mode. ``best`` is null on a first attempt."""

    best: GameResultOut | None
    attempts: int


# ------------------------------------------------------------------ rooms (REST)


class CreateRoomRequest(_Strict):
    exercise: str = "squat"
    target_reps: int = Field(default=10, ge=1, le=MAX_TARGET_REPS)
    time_limit_s: int = Field(default=60, ge=MIN_TIME_LIMIT_S, le=MAX_TIME_LIMIT_S)
    display_name: str | None = Field(default=None, max_length=40)

    @field_validator("exercise")
    @classmethod
    def _exercise(cls, v: str) -> str:
        return game_exercise(v)


class JoinRoomRequest(_Strict):
    display_name: str | None = Field(default=None, max_length=40)


class RoomOut(BaseModel):
    code: str
    exercise: str
    target_reps: int
    time_limit_s: int
    status: str
    host_id: str
    players: int


# ------------------------------------------------------------------ room WebSocket (client -> server)


class RoomPose(_Strict):
    """Joint angles from this player's camera. Same measurements as a workout's ``pose_update``, but the room (not
    a session id) says whose they are. No video ever reaches the server."""

    schema_version: Literal[1]
    type: Literal["pose_update"]
    sequence: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)
    data: PoseData


class RoomReady(_Strict):
    schema_version: Literal[1]
    type: Literal["ready"]
    ready: bool


class RoomRematch(_Strict):
    schema_version: Literal[1]
    type: Literal["rematch"]


class RoomLeave(_Strict):
    schema_version: Literal[1]
    type: Literal["leave"]
