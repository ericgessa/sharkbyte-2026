"""Wire-format models. Source of truth for humans: docs/event-contracts.md."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ incoming


class PoseData(_Strict):
    left_knee_angle_deg: float | None = Field(default=None, ge=0, le=180)
    right_knee_angle_deg: float | None = Field(default=None, ge=0, le=180)
    landmark_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _at_least_one_knee(self) -> "PoseData":
        if self.left_knee_angle_deg is None and self.right_knee_angle_deg is None:
            raise ValueError("at least one knee angle is required")
        return self


class PoseUpdate(_Strict):
    schema_version: Literal[1]
    type: Literal["pose_update"]
    session_id: UUID
    sequence: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)
    data: PoseData


class AuthMessage(_Strict):
    """Must be the first message on a WebSocket. Keeps the token out of the URL."""

    schema_version: Literal[1]
    type: Literal["auth"]
    access_token: str = Field(min_length=1, max_length=8192)


class UserSignal(_Strict):
    """Something the user did in the app (not a measurement)."""

    schema_version: Literal[1]
    type: Literal["user_signal"]
    session_id: UUID
    signal: Literal["pain", "pause", "resume"]


# Phase 4: add SensorUpdate to this union.
IncomingEvent = Annotated[Union[PoseUpdate, UserSignal], Field(discriminator="type")]
incoming_adapter: TypeAdapter[PoseUpdate | UserSignal] = TypeAdapter(IncomingEvent)


# ------------------------------------------------------------------ outgoing


class SessionReady(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["session_ready"] = "session_ready"
    session_id: UUID
    rep_count: int
    paused: bool = False
    coaching_enabled: bool = True  # False once the user has reported pain in this session


class SessionState(_Strict):
    """Sent after every user_signal so the UI can show the current state."""

    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["session_state"] = "session_state"
    session_id: UUID
    paused: bool
    coaching_enabled: bool


class RepMetricsOut(_Strict):
    min_knee_angle_deg: float
    descent_ms: int
    ascent_ms: int
    duration_ms: int
    max_asymmetry_deg: float | None


class RepCompleted(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["rep_completed"] = "rep_completed"
    session_id: UUID
    rep_count: int
    metrics: RepMetricsOut


class CoachingFeedback(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["coaching_feedback"] = "coaching_feedback"
    session_id: UUID
    rep_count: int
    issue_code: str
    message: str
    source: Literal["rules", "agent"] = "rules"


class TrackingStatus(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["tracking_status"] = "tracking_status"
    session_id: UUID
    tracking: Literal["lost", "recovered"]


class ErrorEvent(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["error"] = "error"
    code: Literal["invalid_message", "session_mismatch"]
    detail: str


# ------------------------------------------------------------------ REST


class CreateWorkoutRequest(_Strict):
    exercise: Literal["squat"] = "squat"


class RepOut(_Strict):
    rep_number: int
    metrics: dict[str, Any]
    form_score: float | None = None
    created_at: datetime


class CoachingEventOut(_Strict):
    rep_number: int | None
    issue_code: str | None
    message: str
    action: str | None
    created_at: datetime


class StatsOut(_Strict):
    """Knee angle: smaller = deeper. depth_change_deg > 0 means later reps were shallower."""

    rep_count: int
    avg_min_knee_angle_deg: float | None
    deepest_knee_angle_deg: float | None
    shallowest_knee_angle_deg: float | None
    depth_change_deg: float | None
    avg_rep_duration_ms: int | None
    max_asymmetry_deg: float | None


class WorkoutOut(_Strict):
    session_id: UUID
    exercise: str
    status: Literal["active", "completed"]
    started_at: datetime
    ended_at: datetime | None = None
    total_reps: int
    dropped_frames: int = 0  # live counters; 0 when the session is not streaming
    aborted_attempts: int = 0


class WorkoutSummaryOut(_Strict):
    workout: WorkoutOut
    stats: StatsOut
    reps: list[RepOut]
    coaching_events: list[CoachingEventOut]
