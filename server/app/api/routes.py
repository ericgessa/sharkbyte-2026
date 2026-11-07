from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Annotated, Any
from uuid import UUID

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from app.schemas.events import CreateWorkoutRequest, DeleteAccountRequest, StatsOut, WorkoutOut, WorkoutSummaryOut
from app.services import persistence
from app.services.exercises import DEFAULT_EXERCISE, all_exercises, for_stored_name
from app.services.agent import rules_summary
from app.services.analytics import readings_in_window
from app.services.coach_context import build_coach_context
from app.services.summary import compute_stats
from app.services.timeseries import TigerDataError
from app.services.session_manager import LiveSession

logger = logging.getLogger(__name__)
router = APIRouter()


def current_user_id(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """Validate ``Authorization: Bearer <Supabase access token>``; return the user id.

    Plain ``def`` so FastAPI runs it (and the blocking Supabase call) in the threadpool.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token: sign in with Supabase and send 'Authorization: Bearer <access_token>' "
            "(see docs/frontend-integration.md)",
            {"WWW-Authenticate": "Bearer"},
        )
    user_id = request.app.state.auth.verify(token.strip())
    if user_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired token: use the user's Supabase access_token (not the publishable key); "
            "re-read it with supabase.auth.getSession() because it expires after about an hour",
            {"WWW-Authenticate": "Bearer"},
        )
    limiter = getattr(request.app.state, "rest_limiter", None)
    if limiter is not None and not limiter.allow(user_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many requests; slow down", {"Retry-After": str(limiter.retry_after_s)}
        )
    return user_id


UserId = Annotated[str, Depends(current_user_id)]


def to_out(row: dict[str, Any], live: LiveSession | None = None) -> WorkoutOut:
    exercise = (row.get("exercises") or {}).get("name", DEFAULT_EXERCISE)
    return WorkoutOut(
        session_id=row["id"],
        exercise=exercise,
        analysis_supported=for_stored_name(exercise).analysis_supported,
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


@router.get("/")
def root() -> dict[str, str]:
    """What someone sees when they open the API's address in a browser."""
    return {
        "service": "SharkByte API",
        "health": "/health (no login needed)",
        "docs": "/docs (interactive REST docs)",
        "auth": "all /api/* calls need 'Authorization: Bearer <Supabase access_token>'; see docs/frontend-integration.md",
    }


@router.get("/api/exercises")
def list_exercises() -> list[dict[str, Any]]:
    """The exercises the app can offer. No login needed (nothing user-specific). Use ``slug`` when creating a
    workout; any spelling of the name also works. ``analysis_supported: false`` means the workout is logged
    and its sensor data recorded, but the server does not count reps or coach that exercise yet."""
    return [
        {
            "slug": e.slug,
            "display_name": e.display_name,
            "aliases": list(e.aliases),
            "analysis_supported": e.analysis_supported,
            "primary_angles": list(e.primary_angles),
        }
        for e in all_exercises()
    ]


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Cheap, no dependencies. Use this for a platform's restart check."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    """Readiness: can this instance do its job? Supabase is required (503 if unreachable); Tiger Data is optional
    (reported, but never makes the instance unready, since workouts work without it). Public and unauthenticated,
    so it returns only booleans/labels, and the result is cached for a few seconds so it can't be used to hammer
    the databases."""
    state = request.app.state
    ttl = getattr(state, "ready_ttl_s", 10.0)
    cached = getattr(state, "_ready_cache", None)
    now = time.monotonic()
    if cached is None or now - cached[0] >= ttl:
        supabase_ok = bool(await asyncio.to_thread(state.store.ping))
        if not state.tiger.configured:
            tiger = "not_configured"
        else:
            try:
                await asyncio.to_thread(state.tiger.query, "select 1 as ok")
                tiger = "ok"
            except Exception:
                tiger = "unavailable"
        body = {
            "status": "ok" if supabase_ok else "unavailable",
            "supabase": "ok" if supabase_ok else "unavailable",
            "tiger_data": tiger,
            "sensor_writes": "healthy" if state.ingest.stats.healthy else "failing",
        }
        cached = state._ready_cache = (now, body)
    if cached[1]["supabase"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return cached[1]


@router.post("/api/workouts", response_model=WorkoutOut, status_code=status.HTTP_201_CREATED)
def create_workout(body: CreateWorkoutRequest, request: Request, user_id: UserId) -> WorkoutOut:
    limiter = getattr(request.app.state, "create_limiter", None)
    if limiter is not None and not limiter.allow(user_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many workouts started; slow down", {"Retry-After": str(limiter.retry_after_s)}
        )
    row = request.app.state.store.create_session(user_id, body.exercise)
    logger.info("session created id=%s user=%s", row["id"], user_id)
    return to_out(row)


@router.get("/api/workouts", response_model=list[WorkoutOut])
def list_workouts(request: Request, user_id: UserId, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[WorkoutOut]:
    """The caller's workouts, newest first."""
    live = request.app.state.live
    rows = request.app.state.store.list_sessions(user_id, limit)
    return [to_out(r, live.get(UUID(str(r["id"])))) for r in rows]


@router.delete("/api/workouts/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workout(session_id: UUID, request: Request, user_id: UserId) -> Response:
    """Permanently delete one of your workouts from BOTH databases.

    Supabase: the session, its reps and its coaching events. Tiger Data: its sensor readings (a separate
    database that does not cascade, so it is deleted explicitly, FIRST). If Tiger Data is configured but
    cannot be reached, nothing is deleted and you get a 503 to retry, so sensor data is never left behind
    with its workout gone. 404 if the workout is not yours (same as if it does not exist). 204 on success.
    """
    state = request.app.state
    await asyncio.to_thread(_owned_session, request, session_id, user_id)

    live = state.live.get(session_id)
    if live is not None:
        live.finished = True  # a still-open socket stops adding data to a workout being deleted
        if live.socket is not None:
            with contextlib.suppress(Exception):
                await live.socket.close(code=4409)
        if live.save_tasks:
            await asyncio.gather(*live.save_tasks, return_exceptions=True)

    if state.tiger.configured:
        try:
            if not await state.ingest.flush(timeout_s=5.0):  # nothing may land in Tiger Data after we delete
                raise RuntimeError("buffered readings could not be flushed")
            await asyncio.to_thread(state.tiger.delete_session_readings, session_id)
        except Exception:
            if live is not None:
                live.finished = False  # nothing was deleted: the workout stays usable
            logger.warning("delete of workout %s aborted: Tiger Data unavailable", session_id)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "could not delete the sensor data right now; nothing was deleted, try again"
            ) from None

    await asyncio.to_thread(state.store.delete_session, session_id, user_id)  # cascades to reps + coaching events
    state.live.remove(session_id)
    logger.info("workout deleted id=%s user=%s", session_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/account/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(body: DeleteAccountRequest, request: Request, user_id: UserId) -> Response:
    """Permanently delete YOUR account and everything in it, from both databases.

    Body must be exactly ``{"confirm": "delete my account"}``. This removes the login, the profile, every workout
    with its reps and coaching events (Supabase) and all of your sensor readings (Tiger Data). There is no undo.
    Tiger Data goes first; if it is configured but unreachable nothing is deleted (503, safe to retry). Any
    workout that is streaming right now is closed. 204 on success; the token stops working immediately.
    """
    state = request.app.state
    live_sessions = state.live.for_user(user_id)
    for live in live_sessions:
        live.finished = True  # stop any open socket from adding data
        if live.socket is not None:
            with contextlib.suppress(Exception):
                await live.socket.close(code=4409)
        if live.save_tasks:
            await asyncio.gather(*live.save_tasks, return_exceptions=True)

    session_ids = await asyncio.to_thread(state.store.list_all_session_ids, user_id)
    if state.tiger.configured:
        try:
            if not await state.ingest.flush(timeout_s=5.0):
                raise RuntimeError("buffered readings could not be flushed")
            await asyncio.to_thread(state.tiger.delete_sessions_readings, session_ids)
        except Exception:
            for live in live_sessions:
                live.finished = False  # nothing was deleted: workouts stay usable
            logger.warning("account deletion aborted for user %s: Tiger Data unavailable", user_id)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "could not delete your sensor data right now; nothing was deleted, try again"
            ) from None

    try:
        await asyncio.to_thread(state.store.delete_account, user_id)
    except Exception:
        logger.exception("account deletion: Supabase step failed for user %s", user_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "your sensor data was deleted but the account could not be removed right now; please try again",
        ) from None

    for live in live_sessions:
        state.live.remove(live.session_id)
    invalidate = getattr(state.auth, "invalidate_user", None)
    if invalidate:
        invalidate(user_id)  # the deleted user's token must stop working at once, not after the cache expires
    logger.info("account deleted user=%s workouts=%d", user_id, len(session_ids))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/workouts/{session_id}/analytics")
def workout_analytics(session_id: UUID, request: Request, user_id: UserId) -> dict[str, Any]:
    """Compact analytics for one workout: Supabase facts plus Tiger Data time-series summaries.

    ``timeseries.available`` is false (with a reason) when Tiger Data is not configured or unreachable;
    the Supabase part is always present.
    """
    state = request.app.state
    _owned_session(request, session_id, user_id)
    return build_coach_context(state.store, state.tiger, session_id)


@router.get("/api/workouts/{session_id}/coach-summary")
async def coach_summary(session_id: UUID, request: Request, user_id: UserId) -> dict[str, Any]:
    """A short plain-language recap of a workout, from both databases.

    ``source`` is ``"agent"`` when the LLM coach wrote it and ``"rules"`` for the deterministic recap (the LLM is
    off, unavailable, failed a safety check, or the user reported pain: then it is always the rules text).
    Recaps of finished workouts are cached. Limited to a few per minute per user because each can cost an LLM call.
    """
    state = request.app.state
    if not state.summary_limiter.allow(user_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many recap requests; slow down", {"Retry-After": str(state.summary_limiter.retry_after_s)}
        )
    row = await asyncio.to_thread(_owned_session, request, session_id, user_id)
    key = str(session_id)
    if key in state.summary_cache:
        text, source = state.summary_cache[key]
        return {"summary": text, "source": source}

    context = await asyncio.to_thread(build_coach_context, state.store, state.tiger, session_id)
    exercise = for_stored_name((row.get("exercises") or {}).get("name", DEFAULT_EXERCISE))
    fallback = rules_summary(context, exercise.analysis_supported, exercise.display_name.lower(), exercise.joint)
    if exercise.llm_supported:
        text, source = await state.agent.summarize(context, fallback)
    else:  # the LLM layer only knows knee wording so far: other exercises get the deterministic recap
        text, source = fallback, "rules"
    if row["status"] == "completed":  # an active workout's recap would go stale
        state.summary_cache[key] = (text, source)
        while len(state.summary_cache) > 200:
            state.summary_cache.popitem(last=False)
    return {"summary": text, "source": source}


@router.get("/api/workouts/{session_id}/readings")
def workout_readings(
    session_id: UUID,
    request: Request,
    user_id: UserId,
    start: datetime,
    end: datetime,
    metric: Annotated[list[str] | None, Query(max_length=16)] = None,
    bucket_seconds: Annotated[float | None, Query(gt=0, le=3600)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> dict[str, Any]:
    """Sensor measurements in [start, end) from Tiger Data. Use ``bucket_seconds`` to downsample in SQL."""
    state = request.app.state
    _owned_session(request, session_id, user_id)
    if not state.tiger.configured:
        return {"available": False, "reason": "not_configured", "readings": []}
    start, end = (t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in (start, end))  # naive = UTC
    if end <= start:
        raise HTTPException(422, "end must be after start")
    try:
        rows = readings_in_window(state.tiger, session_id, start, end, metric, bucket_seconds, limit)
    except TigerDataError:
        return {"available": False, "reason": "unavailable", "readings": []}
    return {"available": True, "readings": rows}


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

    # Best effort: write buffered sensor readings so analytics are complete. Never blocks finishing.
    ingest = request.app.state.ingest
    await ingest.flush(timeout_s=5.0 if ingest.stats.healthy else 1.0)  # known outage: don't make the user wait

    # The saved repetitions are the source of truth for the total.
    total = await asyncio.to_thread(store.count_repetitions, session_id)
    done = await asyncio.to_thread(store.finish_session, row, total)
    if done is None:  # raced with another finish
        done = await asyncio.to_thread(_owned_session, request, session_id, user_id)
    request.app.state.live.remove(session_id)
    logger.info("session finished id=%s reps=%d", session_id, total)
    return to_out(done)
