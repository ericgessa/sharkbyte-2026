"""GRIT Games endpoints: saved results, personal bests, and live 1v1 Rep Racer rooms.

REST (all need ``Authorization: Bearer <Supabase access token>``, same as the workout API):
    POST /api/games/results                 save a finished camera-tracked round (scored from its workout)
    GET  /api/games/results                 your history
    GET  /api/games/results/{id}            one of your results
    GET  /api/games/best                    your personal best / high score (the ghost for Personal Best mode)
    POST /api/games/rooms                   create a 1v1 room -> code
    POST /api/games/rooms/{code}/join       take the second seat
    GET  /api/games/rooms/{code}            the room's current state (members only)
    POST /api/games/rooms/{code}/leave
    GET  /api/games/races/{room_id}         a finished race you played in
WebSocket:
    WS   /ws/games/rooms/{code}             first message {"type":"auth","access_token":...}; see docs/grit-games.md

Rules that keep it fair: the server counts reps itself (from joint angles), decides the start, the timer and the
winner, and scores saved results from the workout its own counter scored. Other people's data is never readable or
writable: a result, a workout or a race that is not yours answers 404, exactly like one that does not exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import TypeAdapter, ValidationError

from app.api.routes import UserId, _owned_session
from app.api.websocket import (
    AUTH_TIMEOUT_S,
    CLOSE_NOT_FOUND,
    CLOSE_REPLACED,
    CLOSE_TOO_BIG,
    CLOSE_UNAUTHENTICATED,
    MAX_MESSAGE_BYTES,
)
from app.schemas.events import AuthMessage
from app.schemas.games import (
    BestOut,
    CreateRoomRequest,
    GameResultIn,
    GameResultOut,
    JoinRoomRequest,
    RoomLeave,
    RoomOut,
    RoomPose,
    RoomReady,
    RoomRematch,
    game_exercise,
)
from app.services import game_results
from app.services.exercises import resolve as resolve_exercise
from app.services.game_rooms import FINISHED, Room, RoomError, normalize_code
from app.services.ratelimit import TokenBucket

logger = logging.getLogger(__name__)
router = APIRouter()

POSE_RATE_PER_S, POSE_BURST = 120.0, 240  # a camera sends ~30 frames a second
CONTROL_RATE_PER_S, CONTROL_BURST = 5.0, 20  # ready / rematch / leave
TICK_S = 0.1
MAX_CONNECTION_S = 3 * 3600

_client_message: TypeAdapter[RoomPose | RoomReady | RoomRematch | RoomLeave] = TypeAdapter(RoomPose | RoomReady | RoomRematch | RoomLeave)


# ------------------------------------------------------------------ helpers


def _exercise_param(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return game_exercise(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _result_out(row: dict[str, Any], comparison: Any = None) -> GameResultOut:
    return GameResultOut(
        id=row["id"],
        game=row["game"],
        mode=row["mode"],
        exercise=row["exercise"],
        valid_reps=row["valid_reps"],
        score=row["score"],
        duration_ms=row["duration_ms"],
        target_reps=row.get("target_reps"),
        time_limit_s=row.get("time_limit_s"),
        completed=row["completed"],
        form_feedback=row.get("form_feedback") or {},
        rep_times_ms=row.get("rep_times_ms") or [],
        workout_session_id=row.get("workout_session_id"),
        room_id=row.get("room_id"),
        created_at=row["created_at"],
        personal_best=comparison,
    )


def _display_name(requested: str | None, user_id: str) -> str:
    name = (requested or "").strip()
    return name[:40] if name else f"Player {user_id[:4].upper()}"


def _room_out(room: Room) -> RoomOut:
    return RoomOut(
        code=room.code,
        exercise=room.exercise.slug,
        target_reps=room.target_reps,
        time_limit_s=room.time_limit_s,
        status=room.status,
        host_id=room.host_id,
        players=len(room.players),
    )


def _room_for_member(request: Request, code: str, user_id: str) -> Room:
    try:
        room = request.app.state.rooms.get(normalize_code(code))
    except RoomError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    if room is None or room.closed or user_id not in room.players:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such room")  # same answer for "missing" and "not yours"
    return room


def _room_error(exc: RoomError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.detail})


# ------------------------------------------------------------------ results


@router.post("/api/games/results", response_model=GameResultOut)
async def save_result(body: GameResultIn, request: Request, response: Response, user_id: UserId) -> GameResultOut:
    """Save a finished camera round. Finish its workout first (``POST /api/workouts/{id}/finish``): the saved
    repetitions of that workout are what the result is scored from, so the client cannot claim reps it did not do.
    Sending the same workout again returns the first result (200) instead of saving a duplicate."""
    state = request.app.state
    session = await asyncio.to_thread(_owned_session, request, body.workout_session_id, user_id)
    if session["status"] != "completed":
        raise HTTPException(status.HTTP_409_CONFLICT, "finish the workout first (POST /api/workouts/{id}/finish), then save the result")
    stored_exercise = (session.get("exercises") or {}).get("name", "")
    if resolve_exercise(stored_exercise).slug != body.exercise:
        raise HTTPException(status.HTTP_409_CONFLICT, "the workout was for a different exercise than this result")

    existing = await asyncio.to_thread(state.games.result_for_workout, body.workout_session_id, user_id)
    if existing is not None:
        return _result_out(existing)

    reps = await asyncio.to_thread(state.store.list_repetitions, body.workout_session_id)
    row = game_results.build_result_row(user_id, body, session, reps)
    prior = await asyncio.to_thread(state.games.list_results, user_id, "rep_racer", body.exercise, 200)
    ghost = game_results.choose_ghost(prior, body.target_reps)
    try:
        saved = await asyncio.to_thread(state.games.create_result, row)
    except Exception:
        existing = await asyncio.to_thread(state.games.result_for_workout, body.workout_session_id, user_id)
        if existing is None:  # not a duplicate-save race: a real failure
            raise
        return _result_out(existing)
    response.status_code = status.HTTP_201_CREATED
    logger.info("game result saved id=%s game=%s mode=%s reps=%d user=%s", saved["id"], saved["game"], saved["mode"], saved["valid_reps"], user_id)
    return _result_out(saved, game_results.compare(saved, ghost))


@router.get("/api/games/results", response_model=list[GameResultOut])
def list_results(
    request: Request,
    user_id: UserId,
    game: Annotated[str | None, Query(pattern="^rep_racer$")] = None,
    exercise: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[GameResultOut]:
    """Your camera-tracked results, newest first. Keyboard/mouse practice rounds are never stored."""
    rows = request.app.state.games.list_results(user_id, game, _exercise_param(exercise), limit)
    return [_result_out(r) for r in rows]


@router.get("/api/games/best", response_model=BestOut)
def personal_best(
    request: Request,
    user_id: UserId,
    game: Annotated[str, Query(pattern="^rep_racer$")],
    exercise: str,
    target_reps: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> BestOut:
    """Rep Racer: the attempt to race as a ghost (your fastest finished race with this target, else your attempt
    with the most reps). ``best`` is null on a first attempt: nothing is invented."""
    slug = _exercise_param(exercise)
    prior = request.app.state.games.list_results(user_id, game, slug, 200)
    best = game_results.choose_ghost(prior, target_reps)
    return BestOut(best=_result_out(best) if best else None, attempts=len(prior))


@router.get("/api/games/results/{result_id}", response_model=GameResultOut)
def get_result(result_id: UUID, request: Request, user_id: UserId) -> GameResultOut:
    row = request.app.state.games.get_result(result_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "result not found")
    return _result_out(row)


# ------------------------------------------------------------------ rooms (REST)


@router.post("/api/games/rooms", response_model=RoomOut, status_code=status.HTTP_201_CREATED)
def create_room(body: CreateRoomRequest, request: Request, user_id: UserId) -> RoomOut:
    limiter = getattr(request.app.state, "create_limiter", None)
    if limiter is not None and not limiter.allow(user_id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many rooms created; slow down", {"Retry-After": str(limiter.retry_after_s)})
    try:
        room = request.app.state.rooms.create(user_id, _display_name(body.display_name, user_id), resolve_exercise(body.exercise), body.target_reps, body.time_limit_s)
    except RoomError as exc:
        raise _room_error(exc) from None
    return _room_out(room)


@router.post("/api/games/rooms/{code}/join", response_model=RoomOut)
def join_room(code: str, body: JoinRoomRequest, request: Request, user_id: UserId) -> RoomOut:
    try:
        room = request.app.state.rooms.join(code, user_id, _display_name(body.display_name, user_id))
    except RoomError as exc:
        raise _room_error(exc) from None
    return _room_out(room)


@router.get("/api/games/rooms/{code}")
def get_room(code: str, request: Request, user_id: UserId) -> dict[str, Any]:
    room = _room_for_member(request, code, user_id)
    return {**_room_out(room).model_dump(), "state": room.snapshot()}


@router.post("/api/games/rooms/{code}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_room(code: str, request: Request, user_id: UserId) -> Response:
    room = _room_for_member(request, code, user_id)
    room.leave(user_id)
    await _flush(room)
    await _persist_if_finished(request.app, room)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/games/races/{room_id}")
def get_race(room_id: UUID, request: Request, user_id: UserId) -> dict[str, Any]:
    race = request.app.state.games.get_race(room_id, user_id)
    if race is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return race


# ------------------------------------------------------------------ rooms (WebSocket)


async def _send(ws: WebSocket, event: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):  # a socket that just died is cleaned up by its own receive loop
        await ws.send_json(event)


async def _flush(room: Room) -> None:
    """Deliver everything the room queued. ``room_state`` is personalised with ``you``."""
    for to, event in room.drain():
        for player in list(room.players.values()):
            if player.socket is None or (to is not None and player.user_id != to):
                continue
            await _send(player.socket, {**event, "you": player.user_id} if event["type"] == "room_state" else event)


def _persist_race_sync(state: Any, payload: dict[str, Any]) -> str:
    """Save a finished race: each player's workout (so history and the AI recap work for races too), their game
    result, and the race itself. Runs in a worker thread."""
    results: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    target = payload["target_reps"]
    for p in payload["players"]:
        session = state.store.create_session(p["user_id"], payload["exercise"])
        for metrics in p["rep_metrics"]:
            state.store.save_repetition(session["id"], metrics["rep_number"], metrics)
        state.store.finish_session(session, len(p["rep_metrics"]))
        reached = p["reps"] >= target
        times = p["rep_ms"][:target]
        results.append(
            {
                "user_id": p["user_id"],
                "game": "rep_racer",
                "mode": "multiplayer",
                "exercise": payload["exercise"],
                "input_source": "camera",
                "valid_reps": min(p["reps"], target),
                "score": min(p["reps"], target),
                "duration_ms": times[target - 1] if reached else payload["duration_ms"],
                "target_reps": target,
                "time_limit_s": payload["time_limit_s"],
                "completed": reached,
                "form_feedback": {},
                "rep_times_ms": times,
                "workout_session_id": str(session["id"]),
            }
        )
        players.append(
            {
                "user_id": p["user_id"],
                "display_name": p["name"][:40] or "Player",
                "valid_reps": p["reps"],
                "last_rep_ms": p["last_rep_ms"],
                "placement": p["placement"],
                "result": p["result"],
            }
        )
    room_row = {
        "code": payload["code"],
        "host_id": payload["host_id"],
        "exercise": payload["exercise"],
        "target_reps": target,
        "time_limit_s": payload["time_limit_s"],
        "winner_id": payload["winner_id"],
        "end_reason": payload["end_reason"],
        "started_at": payload["started_at"].isoformat(),
    }
    return state.games.save_race(room_row, players, results)


async def _persist_if_finished(app: Any, room: Room) -> None:
    """Save the race exactly once. A failure is logged and never reaches the players: the result screen they are
    looking at came from the live room, not from the database."""
    if not room.persist_pending or room.status != FINISHED:
        return
    room.persist_pending = False
    try:
        payload = room.finished_payload()
        room_id = await asyncio.wait_for(asyncio.to_thread(_persist_race_sync, app.state, payload), 30.0)
        logger.info("race saved room=%s db_id=%s", room.code, room_id)
    except Exception:
        logger.exception("could not save the finished race in room %s (the players still saw the result)", room.code)


async def _drive(app: Any, room: Room) -> None:
    """Moves a room's clock: countdown -> race -> timer / forfeit, and saves the race when it ends."""
    try:
        while not room.closed and app.state.rooms.get(room.code) is room:
            await asyncio.sleep(TICK_S)
            room.tick()
            await _flush(room)
            await _persist_if_finished(app, room)
            if room.status == FINISHED and not any(p.connected for p in room.players.values()):
                break  # nobody is watching any more; the sweeper removes the room later
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("room driver for %s crashed", room.code)
    finally:
        drivers = app.state.room_drivers
        if drivers.get(room.code) is asyncio.current_task():
            drivers.pop(room.code, None)


def _ensure_driver(app: Any, room: Room) -> None:
    drivers = app.state.room_drivers
    task = drivers.get(room.code)
    if task is None or task.done():
        drivers[room.code] = asyncio.create_task(_drive(app, room), name=f"room-{room.code}")


async def _authenticate(ws: WebSocket, code: str) -> tuple[Room, str] | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        if len(raw) > MAX_MESSAGE_BYTES:
            await ws.close(code=CLOSE_TOO_BIG)
            return None
        token = AuthMessage.model_validate_json(raw).access_token
    except (asyncio.TimeoutError, ValidationError):
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None
    user_id = await asyncio.to_thread(ws.app.state.auth.verify, token)
    if user_id is None:
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None
    try:
        room = ws.app.state.rooms.get(normalize_code(code))
    except RoomError:
        room = None
    if room is None or room.closed or user_id not in room.players:  # same close code for "missing" and "not yours"
        await ws.close(code=CLOSE_NOT_FOUND)
        return None
    return room, user_id


@router.websocket("/ws/games/rooms/{code}")
async def room_socket(ws: WebSocket, code: str) -> None:
    await ws.accept()
    room: Room | None = None
    user_id = ""
    try:
        authed = await _authenticate(ws, code)
        if authed is None:
            return
        room, user_id = authed
        old = room.connect(user_id, ws)
        if old is not None and old is not ws:  # a reconnect (or a second tab) takes the seat over
            with contextlib.suppress(Exception):
                await old.close(code=CLOSE_REPLACED)
        _ensure_driver(ws.app, room)
        await _flush(room)
        pose_bucket = TokenBucket(POSE_RATE_PER_S, POSE_BURST)
        control_bucket = TokenBucket(CONTROL_RATE_PER_S, CONTROL_BURST)
        connected_at = time.monotonic()
        last_warning = float("-inf")
        logger.info("room ws authenticated room=%s user=%s", room.code, user_id)

        while True:
            raw = await ws.receive_text()
            player = room.players.get(user_id)
            if player is None or player.socket is not ws:  # replaced by a newer connection, or left the room
                return
            if time.monotonic() - connected_at > MAX_CONNECTION_S:
                await ws.close(code=CLOSE_UNAUTHENTICATED)
                return
            if len(raw) > MAX_MESSAGE_BYTES:
                await ws.close(code=CLOSE_TOO_BIG)
                return
            bucket = pose_bucket if '"pose_update"' in raw else control_bucket
            if not bucket.allow():
                now = time.monotonic()
                if now - last_warning >= 5.0:
                    last_warning = now
                    await _send(ws, {"schema_version": 1, "room": room.code, "type": "error", "code": "rate_limited", "detail": "too many messages; slow down"})
                continue
            try:
                msg = _client_message.validate_json(raw)
            except ValidationError as exc:
                fields = ",".join(".".join(map(str, e["loc"])) for e in exc.errors())
                await _send(ws, {"schema_version": 1, "room": room.code, "type": "error", "code": "invalid_message", "detail": f"invalid fields: {fields}"})
                continue
            try:
                if isinstance(msg, RoomPose):
                    room.on_pose(user_id, msg)
                elif isinstance(msg, RoomReady):
                    room.set_ready(user_id, msg.ready)
                elif isinstance(msg, RoomRematch):
                    room.request_rematch(user_id)
                elif isinstance(msg, RoomLeave):
                    room.leave(user_id)
                    await _flush(room)
                    await _persist_if_finished(ws.app, room)
                    await ws.close()
                    return
            except RoomError as exc:
                await _send(ws, {"schema_version": 1, "room": room.code, "type": "error", "code": exc.code, "detail": exc.detail})
            await _flush(room)
            await _persist_if_finished(ws.app, room)
    except WebSocketDisconnect:
        logger.info("room ws disconnected room=%s user=%s", getattr(room, "code", "?"), user_id)
    finally:
        if room is not None:
            room.disconnect(user_id, ws)
            await _flush(room)
