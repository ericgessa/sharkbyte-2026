"""Wire-format models. Source of truth for humans: docs/event-contracts.md."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.schemas.sensors import SensorUpdate
from app.services.exercises import DEFAULT_EXERCISE, UnknownExerciseError
from app.services.exercises import resolve as resolve_exercise

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# PoseData field -> stored metric name (see schemas/sensors.py for the table's naming rules)
ANGLE_FIELDS: dict[str, str] = {
    "left_knee_angle_deg": "left_knee_angle",
    "right_knee_angle_deg": "right_knee_angle",
    "left_elbow_angle_deg": "left_elbow_angle",
    "right_elbow_angle_deg": "right_elbow_angle",
    "left_hip_angle_deg": "left_hip_angle",
    "right_hip_angle_deg": "right_hip_angle",
}


# ------------------------------------------------------------------ incoming


class PoseData(_Strict):
    """Joint angles from the camera, degrees 0-180 (180 = straight). Send only joints that are visible.

    Knee angles drive squat rep counting. Elbow and hip angles are recorded (Tiger Data) for the other
    exercises but NOT analyzed yet: only squats are counted and coached.
    """

    left_knee_angle_deg: float | None = Field(default=None, ge=0, le=180)
    right_knee_angle_deg: float | None = Field(default=None, ge=0, le=180)
    left_elbow_angle_deg: float | None = Field(default=None, ge=0, le=180)
    right_elbow_angle_deg: float | None = Field(default=None, ge=0, le=180)
    left_hip_angle_deg: float | None = Field(default=None, ge=0, le=180)
    right_hip_angle_deg: float | None = Field(default=None, ge=0, le=180)
    landmark_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _at_least_one_angle(self) -> "PoseData":
        if all(getattr(self, f) is None for f in ANGLE_FIELDS):
            raise ValueError("at least one joint angle is required")
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


IncomingEvent = Annotated[Union[PoseUpdate, SensorUpdate, UserSignal], Field(discriminator="type")]
incoming_adapter: TypeAdapter[PoseUpdate | SensorUpdate | UserSignal] = TypeAdapter(IncomingEvent)


# ------------------------------------------------------------------ outgoing


class SessionReady(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    type: Literal["session_ready"] = "session_ready"
    session_id: UUID
    rep_count: int
    exercise: str = "squat"
    # False: this exercise is logged and its data recorded, but reps are NOT counted and there is no coaching.
    analysis_supported: bool = True
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
    code: Literal["invalid_message", "session_mismatch", "rate_limited"]
    detail: str


# ------------------------------------------------------------------ REST


class CreateWorkoutRequest(_Strict):
    """``exercise`` accepts any spelling of a registered exercise ("Push Up", "pushup", "push-up") and is
    normalized to its canonical name. Unknown names get a 422 listing what is supported."""

    exercise: str = DEFAULT_EXERCISE

    @field_validator("exercise", mode="before")
    @classmethod
    def _normalize_exercise(cls, v: object) -> str:
        try:
            return resolve_exercise(v).slug
        except UnknownExerciseError as exc:
            raise ValueError(str(exc)) from None


class DeleteAccountRequest(_Strict):
    """Deleting an account is irreversible, so the caller must type the confirmation phrase exactly."""

    confirm: Literal["delete my account"]


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
    # False: logged only. The server does not count reps or coach this exercise yet.
    analysis_supported: bool = False
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
