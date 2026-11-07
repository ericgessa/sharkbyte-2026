from __future__ import annotations

import asyncio
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from app.schemas.events import (
    AuthMessage,
    CoachingFeedback,
    ErrorEvent,
    RepCompleted,
    RepMetricsOut,
    SessionReady,
    SessionState,
    TrackingStatus,
    UserSignal,
    incoming_adapter,
)
from app.schemas.sensors import SensorUpdate
from app.services.coaching import ACTION_RECOMMEND_REST, PAIN_ISSUE_CODE, PAIN_MESSAGE
from app.services import persistence
from app.services.ingest import pose_readings, sensor_readings
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


def _stored_metrics(live: LiveSession, r: RepMetrics) -> dict:
    """What is saved in Supabase for a rep: the metrics plus when the rep happened (server time),
    so time-series analytics can slice a session into reps. Not sent to the client."""
    return {
        **_metrics_dict(r),
        "started_at": live.clock.to_datetime("camera", r.start_ts_ms).isoformat(),
        "ended_at": live.clock.to_datetime("camera", r.end_ts_ms).isoformat(),
    }


def _record(ws: WebSocket, readings_fn) -> None:
    """Hand sensor readings to the ingestor. A bug or outage here must never touch the live workout."""
    try:
        ws.app.state.ingest.add(readings_fn())
    except Exception:
        logger.exception("sensor ingestion failed; live workout unaffected")


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
        # A pain report must survive a server restart: if one was saved, stay in "no coaching" mode.
        events = await asyncio.to_thread(store.list_coaching_events, session_id)
        if any(e.get("issue_code") == PAIN_ISSUE_CODE for e in events):
            live.pain_reported, live.coach.enabled = True, False
    return live


async def _handle_signal(ws: WebSocket, live: LiveSession, signal: str) -> None:
    """pause / resume / pain from the user's own buttons."""
    if signal == "pause":
        live.paused = True
    elif signal == "resume":
        if live.paused:
            live.counter.reset_tracking()  # don't count from the middle of a movement
        live.paused = False
    elif signal == "pain":
        first_time = not live.pain_reported
        live.pain_reported = True
        live.coach.enabled = False  # no more technique coaching this session
        reps = live.counter.rep_count
        await _send(
            ws,
            CoachingFeedback(session_id=live.session_id, rep_count=reps, issue_code=PAIN_ISSUE_CODE, message=PAIN_MESSAGE),
        )
        if first_time:  # saved once, even if the user taps the button again
            _persist(
                ws,
                live,
                (
                    persistence.COACHING_EVENT,
                    (live.session_id, uuid4(), reps or None, PAIN_ISSUE_CODE, PAIN_MESSAGE, ACTION_RECOMMEND_REST),
                ),
            )
    logger.info("user signal %s session=%s", signal, live.session_id)
    await _send(
        ws,
        SessionState(session_id=live.session_id, paused=live.paused, coaching_enabled=not live.pain_reported),
    )


@router.websocket("/ws/workouts/{session_id}")
async def workout_socket(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()
    try:
        live = await _authenticate(ws, session_id)
        if live is None:
            return
        counter = live.counter
        logger.info("ws authenticated session=%s", session_id)
        await _send(
            ws,
            SessionReady(
                session_id=session_id,
                rep_count=counter.rep_count,
                paused=live.paused,
                coaching_enabled=not live.pain_reported,
            ),
        )

        while True:
            raw = await ws.receive_text()
            if live.finished:
                await ws.close(code=CLOSE_NOT_ACTIVE)
                return
            try:
                event = incoming_adapter.validate_json(raw)
            except ValidationError as exc:
                # Don't echo user input back; just say which fields were wrong.
                fields = ",".join(".".join(map(str, e["loc"])) for e in exc.errors())
                await _send(ws, ErrorEvent(code="invalid_message", detail=f"invalid fields: {fields}"))
                continue

            # The path decides which session this is; the body must agree.
            if event.session_id != session_id:
                await _send(ws, ErrorEvent(code="session_mismatch", detail="session_id does not match socket"))
                continue

            if isinstance(event, UserSignal):
                await _handle_signal(ws, live, event.signal)
                continue
            if live.paused:
                continue  # the user paused: ignore measurements until they resume

            if isinstance(event, SensorUpdate):  # ESP32 data relayed over this same socket
                _record(
                    ws,
                    lambda: sensor_readings(
                        session_id,
                        live.clock.to_datetime(f"device:{event.device_id}", event.timestamp_ms),
                        event.device_id,
                        event.data,
                    ),
                )
                continue

            d = event.data
            _record(ws, lambda: pose_readings(session_id, live.clock.to_datetime("camera", event.timestamp_ms), d))
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
                _persist(ws, live, (persistence.REP, (session_id, r.rep_number, _stored_metrics(live, r))))

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
