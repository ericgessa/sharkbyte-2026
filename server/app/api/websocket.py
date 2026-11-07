from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from app.schemas.events import (
    AuthMessage,
    CoachingFeedback,
    ErrorEvent,
    PoseUpdate,
    RepCompleted,
    RepMetricsOut,
    SessionReady,
    TrackingStatus,
)
from app.services import persistence
from app.services.persistence import Write
from app.services.rep_counter import PoseSample, RepMetrics
from app.services.session_manager import LiveSession

logger = logging.getLogger(__name__)
router = APIRouter()

# Application-defined WebSocket close codes.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_FOUND = 4404  # also used for "exists but is not yours"
CLOSE_NOT_ACTIVE = 4409
CLOSE_SERVER_ERROR = 4500
AUTH_TIMEOUT_S = 10.0


async def _send(ws: WebSocket, event: BaseModel) -> None:
    await ws.send_json(event.model_dump(mode="json"))


def _metrics_dict(r: RepMetrics) -> dict:
    return {
        "min_knee_angle_deg": r.min_knee_angle_deg,
        "descent_ms": r.descent_ms,
        "ascent_ms": r.ascent_ms,
        "duration_ms": r.duration_ms,
        "max_asymmetry_deg": r.max_asymmetry_deg,
    }


SAVE_TIMEOUT_S = 15.0


async def _run_write(store: object, live: LiveSession, write: Write) -> None:
    """Run one database write. A failure never interrupts the workout: it is queued for retry."""
    try:
        await asyncio.wait_for(asyncio.to_thread(persistence.perform, store, write), SAVE_TIMEOUT_S)
    except Exception:
        logger.exception("write %s failed for session %s; queued for retry", write[0], live.session_id)
        live.pending_writes.append(write)


def _persist(ws: WebSocket, live: LiveSession, write: Write) -> None:
    """Fire-and-track: counting continues while the write runs in the background."""
    task = asyncio.create_task(_run_write(ws.app.state.store, live, write))
    live.save_tasks.add(task)
    task.add_done_callback(live.save_tasks.discard)


async def _authenticate(ws: WebSocket, session_id: UUID) -> LiveSession | None:
    """First message must be ``auth``. Returns the live session, or closes the socket."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        token = AuthMessage.model_validate_json(raw).access_token
    except (asyncio.TimeoutError, ValidationError):
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None

    store, auth = ws.app.state.store, ws.app.state.auth
    user_id = await asyncio.to_thread(auth.verify, token)
    if user_id is None:
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None

    row = await asyncio.to_thread(store.get_session, session_id, user_id)
    if row is None:
        await ws.close(code=CLOSE_NOT_FOUND)
        return None
    if row["status"] != "active":
        await ws.close(code=CLOSE_NOT_ACTIVE)
        return None

    live = ws.app.state.live.get(session_id)
    if live is None:
        saved = await asyncio.to_thread(store.count_repetitions, session_id)
        live = ws.app.state.live.get_or_create(session_id, user_id, saved_reps=saved)
    return live


@router.websocket("/ws/workouts/{session_id}")
async def workout_socket(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()
    try:
        live = await _authenticate(ws, session_id)
        if live is None:
            return
        counter = live.counter
        logger.info("ws authenticated session=%s", session_id)
        await _send(ws, SessionReady(session_id=session_id, rep_count=counter.rep_count))

        while True:
            raw = await ws.receive_text()
            if live.finished:
                await ws.close(code=CLOSE_NOT_ACTIVE)
                return
            try:
                event = PoseUpdate.model_validate_json(raw)
            except ValidationError as exc:
                # Don't echo user input back; just say which fields were wrong.
                fields = ",".join(".".join(map(str, e["loc"])) for e in exc.errors())
                await _send(ws, ErrorEvent(code="invalid_message", detail=f"invalid fields: {fields}"))
                continue

            # The path decides which session this is; the body must agree.
            if event.session_id != session_id:
                await _send(ws, ErrorEvent(code="session_mismatch", detail="session_id does not match socket"))
                continue

            d = event.data
            result = counter.update(
                PoseSample(
                    sequence=event.sequence,
                    timestamp_ms=event.timestamp_ms,
                    confidence=d.landmark_confidence,
                    left_knee_deg=d.left_knee_angle_deg,
                    right_knee_deg=d.right_knee_angle_deg,
                )
            )

            if result.tracking_changed is not None:
                await _send(
                    ws,
                    TrackingStatus(
                        session_id=session_id,
                        tracking="lost" if result.tracking_changed else "recovered",
                    ),
                )
            if result.rep is not None:
                r = result.rep
                # Tell the client first (low latency), then save.
                await _send(
                    ws,
                    RepCompleted(session_id=session_id, rep_count=r.rep_number, metrics=RepMetricsOut(**_metrics_dict(r))),
                )
                _persist(ws, live, (persistence.REP, (session_id, r.rep_number, _metrics_dict(r))))

                decision = live.coach.observe(r)
                if decision is not None:
                    await _send(
                        ws,
                        CoachingFeedback(
                            session_id=session_id,
                            rep_count=decision.rep_number,
                            issue_code=decision.issue_code,
                            message=decision.message,
                        ),
                    )
                    _persist(
                        ws,
                        live,
                        (
                            persistence.COACHING_EVENT,
                            (session_id, decision.event_id, decision.rep_number, decision.issue_code, decision.message, decision.action),
                        ),
                    )
    except WebSocketDisconnect:
        logger.info("ws disconnected session=%s", session_id)
