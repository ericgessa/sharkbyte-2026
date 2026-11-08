"""Response model for GET /api/stats. Source of truth for humans: docs/event-contracts.md."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class TotalsOut(BaseModel):
    workouts: int
    reps: int
    active_seconds: int
    first_workout_at: datetime | None = None
    last_workout_at: datetime | None = None


class ExerciseStatsOut(BaseModel):
    exercise: str
    workouts: int
    total_reps: int
    best_workout_reps: int
    best_workout_id: UUID | None = None
    last_workout_at: datetime | None = None


class WeekOut(BaseModel):
    week_start: date  # the Monday of that week, in the caller's time zone
    workouts: int
    reps: int


class StreakOut(BaseModel):
    current_days: int
    longest_days: int


class BestWorkoutOut(BaseModel):
    exercise: str
    reps: int
    workout_id: UUID | None = None
    at: datetime | None = None


class RecordsOut(BaseModel):
    most_reps_in_a_workout: BestWorkoutOut | None = None


class FastestRaceOut(BaseModel):
    exercise: str
    target_reps: int
    duration_ms: int
    mode: str
    achieved_at: datetime | None = None


class RaceStatsOut(BaseModel):
    races: int
    finished: int
    fastest: list[FastestRaceOut]


class ProgressOut(BaseModel):
    totals: TotalsOut
    per_exercise: list[ExerciseStatsOut]
    weekly: list[WeekOut]
    streak: StreakOut
    records: RecordsOut
    # Rep Racer. null when the GRIT Games database tables are not set up (the rest of the stats still work).
    games: RaceStatsOut | None = None
    # How many of the newest workouts were looked at (older ones are ignored).
    workouts_considered: int
