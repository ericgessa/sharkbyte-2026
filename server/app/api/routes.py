from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.schemas.events import CreateWorkoutRequest, StatsOut, WorkoutOut, WorkoutSummaryOut
from app.services import persistence
from app.services.summary import compute_stats
from app.services.session_manager import LiveSession

logger = logging.getLogger(__name__)
router = APIRouter()


def current_user_id(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """Validate ``Authorization: Bearer <Supabase access token>``; return the user id.

    Plain ``def`` so FastAPI runs it (and the blocking Supabase call) in the threadpool.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token", {"WWW-Authenticate": "Bearer"})
    user_id = request.app.state.auth.verify(token.strip())
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token", {"WWW-Authenticate": "Bearer"})
    return user_id


UserId = Annotated[str, Depends(current_user_id)]


def to_out(row: dict[str, Any], live: LiveSession | None = None) -> WorkoutOut:
    exercise = (row.get("exercises") or {}).get("name", "squat")
    return WorkoutOut(
        session_id=row["id"],
        exercise=exercise,
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row.get("ended_at"),
        total_reps=live.counter.rep_count if live else row["total_reps"],
        dropped_frames=live.counter.dropped_frames if live else 0,
        aborted_attempts=live.counter.aborted_attempts if live else 0,
    )


def _owned_session(request: Request, session_id: UUID, user_id: str) -> dict[str, Any]:
    row = request.app.state.store.get_session(session_id, user_id)
    if row is None:  # same answer for "missing" and "someone else's"
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return row


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/workouts", response_model=WorkoutOut, status_code=status.HTTP_201_CREATED)
def create_workout(body: CreateWorkoutRequest, request: Request, user_id: UserId) -> WorkoutOut:
    row = request.app.state.store.create_session(user_id, body.exercise)
    logger.info("session created id=%s user=%s", row["id"], user_id)
    return to_out(row)


@router.get("/api/workouts", response_model=list[WorkoutOut])
def list_workouts(request: Request, user_id: UserId, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[WorkoutOut]:
    """The caller's workouts, newest first."""
    live = request.app.state.live
    rows = request.app.state.store.list_sessions(user_id, limit)
    return [to_out(r, live.get(UUID(str(r["id"])))) for r in rows]


@router.get("/api/workouts/{session_id}/summary", response_model=WorkoutSummaryOut)
def workout_summary(session_id: UUID, request: Request, user_id: UserId) -> WorkoutSummaryOut:
    """Everything the end-of-workout screen needs: reps, coaching messages, stats.

    Works for active sessions too (reps saved so far).
    """
    store = request.app.state.store
    row = _owned_session(request, session_id, user_id)
    reps = store.list_repetitions(session_id)
    events = store.list_coaching_events(session_id)
    return WorkoutSummaryOut(
        workout=to_out(row, request.app.state.live.get(session_id)),
        stats=StatsOut(**compute_stats(reps)),
        reps=reps,
        coaching_events=events,
    )


@router.get("/api/workouts/{session_id}", response_model=WorkoutOut)
def get_workout(session_id: UUID, request: Request, user_id: UserId) -> WorkoutOut:
    row = _owned_session(request, session_id, user_id)
    return to_out(row, request.app.state.live.get(session_id))


@router.post("/api/workouts/{session_id}/finish", response_model=WorkoutOut)
async def finish_workout(session_id: UUID, request: Request, user_id: UserId) -> WorkoutOut:
    store = request.app.state.store
    row = await asyncio.to_thread(_owned_session, request, session_id, user_id)
    if row["status"] == "completed":  # idempotent
        return to_out(row)

    live = request.app.state.live.get(session_id)
    if live is not None:
        live.finished = True  # stops any still-open socket from adding reps
        # Let in-flight saves land, then retry any that failed earlier.
        if live.save_tasks:
            await asyncio.gather(*live.save_tasks, return_exceptions=True)
        remaining = []
        for write in live.pending_writes:
            try:
                await asyncio.to_thread(persistence.perform, store, write)
            except Exception:
                logger.exception("retry failed: session=%s write=%s", session_id, write[0])
                remaining.append(write)
        live.pending_writes = remaining
        if remaining:
            live.finished = False  # let the workout continue; the client can retry finishing
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not save all data yet; retry")

    # The saved repetitions are the source of truth for the total.
    total = await asyncio.to_thread(store.count_repetitions, session_id)
    done = await asyncio.to_thread(store.finish_session, row, total)
    if done is None:  # raced with another finish
        done = await asyncio.to_thread(_owned_session, request, session_id, user_id)
    request.app.state.live.remove(session_id)
    logger.info("session finished id=%s reps=%d", session_id, total)
    return to_out(done)
