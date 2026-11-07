from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas.events import CreateWorkoutRequest, WorkoutOut
from app.services.session_manager import WorkoutSession

router = APIRouter()


def _to_out(s: WorkoutSession) -> WorkoutOut:
    return WorkoutOut(
        session_id=s.id,
        exercise=s.exercise,
        status=s.status,  # type: ignore[arg-type]
        started_at=s.started_at,
        total_reps=s.counter.rep_count,
        dropped_frames=s.counter.dropped_frames,
        aborted_attempts=s.counter.aborted_attempts,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Phase 1: no auth. Phase 2 adds Supabase JWT validation and ownership checks here.
@router.post("/api/workouts", response_model=WorkoutOut, status_code=status.HTTP_201_CREATED)
def create_workout(body: CreateWorkoutRequest, request: Request) -> WorkoutOut:
    return _to_out(request.app.state.sessions.create(body.exercise))


@router.get("/api/workouts/{session_id}", response_model=WorkoutOut)
def get_workout(session_id: UUID, request: Request) -> WorkoutOut:
    session = request.app.state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return _to_out(session)
