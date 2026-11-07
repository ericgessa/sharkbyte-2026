from __future__ import annotations

import asyncio
import logging
import time
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
from app.services.agent import CoachAgent, build_cue_context
from app.services.coaching import ACTION_RECOMMEND_REST, PAIN_ISSUE_CODE, PAIN_MESSAGE, CoachingDecision
from app.services.exercises import DEFAULT_EXERCISE, for_stored_name
from app.services import persistence
from app.services.ingest import pose_readings, sensor_readings
from app.services.persistence import Write
from app.services.ratelimit import TokenBucket
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
CLOSE_REPLACED = 4410  # a newer connection took this session over
CLOSE_TOO_BIG = 1009  # standard "message too big"
MAX_MESSAGE_BYTES = 16_384  # pose/sensor messages are ~300 B; the largest legit one is the auth token
RATE_PER_S = 300.0  # sustained measurements/second per connection (camera ~30 + a fast IMU is well under this)
BURST = 600  # lets a reconnecting phone flush ~2 s of buffered frames
# user_signal (pause / resume / PAIN) has its own small limit so a flood of measurements can never starve it.
SIGNAL_RATE_PER_S = 5.0
SIGNAL_BURST = 20
RATE_WARN_EVERY_S = 5.0
# The token is only checked when the socket connects. Closing long connections makes the app reconnect, which
# re-checks the (refreshed) token, so a revoked or deleted user can't keep streaming for hours.
MAX_CONNECTION_S = 3 * 3600


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
        if len(raw) > MAX_MESSAGE_BYTES:
            await ws.close(code=CLOSE_TOO_BIG)
            return None
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
        stored = (row.get("exercises") or {}).get("name", DEFAULT_EXERCISE)
        live = ws.app.state.live.get_or_create(session_id, user_id, saved_reps=saved, exercise=for_stored_name(stored))
        # A pain report must survive a server restart: if one was saved, stay in "no coaching" mode.
        events = await asyncio.to_thread(store.list_coaching_events, session_id)
        if any(e.get("issue_code") == PAIN_ISSUE_CODE for e in events):
            live.pain_reported, live.coach.enabled = True, False
    return live


async def _deliver_coaching(ws: WebSocket, live: LiveSession, decision: CoachingDecision) -> None:
    """Send one coaching cue to the app and save it (the same record whether rules or the LLM worded it)."""
    await _send(
        ws,
        CoachingFeedback(
            session_id=live.session_id,
            rep_count=decision.rep_number,
            issue_code=decision.issue_code,
            message=decision.message,
            source=decision.source,  # "rules" or "agent"
        ),
    )
    _persist(
        ws,
        live,
        (
            persistence.COACHING_EVENT,
            (live.session_id, decision.event_id, decision.rep_number, decision.issue_code, decision.message, decision.action),
        ),
    )


def _spawn_agent_delivery(ws: WebSocket, live: LiveSession, decision: CoachingDecision, agent: CoachAgent) -> None:
    """Let the LLM word (or suppress) a cue the rules already decided on, without blocking the live loop.

    Whatever goes wrong inside, the user still gets the rule message (``agent.refine`` falls back). Because the
    model takes a moment, the situation is re-checked before sending: never coach after a pain report or a finish.
    """

    async def run() -> None:
        try:
            context = build_cue_context(live.exercise.slug, live.coach.reps, decision, live.coach.history, live.coach.config)
            final = await agent.refine(decision, context)
            target = live.socket  # the app may have reconnected while we waited: deliver to the current connection
            if final is None or target is None or live.pain_reported or live.finished:
                return
            await _deliver_coaching(target, live, final)
        except Exception:
            logger.exception("coaching delivery failed; the rep count is unaffected")

    task = asyncio.create_task(run())
    live.agent_tasks.add(task)
    task.add_done_callback(live.agent_tasks.discard)


async def _take_over(ws: WebSocket, live: LiveSession) -> None:
    """One socket per session. A reconnecting phone must not have to wait for the old, possibly
    half-open connection to time out: the newest authenticated connection wins and the old one is closed."""
    old = live.socket
    live.socket = ws
    live.touch()
    if old is not None and old is not ws:
        logger.info("session %s taken over by a new connection", live.session_id)
        try:
            await old.close(code=CLOSE_REPLACED)
        except Exception:  # already closed or broken: nothing to do
            pass


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
    live: LiveSession | None = None
    try:
        live = await _authenticate(ws, session_id)
        if live is None:
            return
        await _take_over(ws, live)
        counter = live.counter
        bucket = TokenBucket(RATE_PER_S, BURST)
        signal_bucket = TokenBucket(SIGNAL_RATE_PER_S, SIGNAL_BURST)
        last_rate_warning = float("-inf")
        connected_at = time.monotonic()
        logger.info("ws authenticated session=%s", session_id)
        await _send(
            ws,
            SessionReady(
                session_id=session_id,
                rep_count=counter.rep_count,
                exercise=live.exercise.slug,
                analysis_supported=live.exercise.analysis_supported,
                paused=live.paused,
                coaching_enabled=not live.pain_reported,
            ),
        )

        while True:
            raw = await ws.receive_text()
            if live.socket is not ws:  # a newer connection took over; this one is stale
                return
            live.touch()
            if time.monotonic() - connected_at > MAX_CONNECTION_S:
                await ws.close(code=CLOSE_UNAUTHENTICATED)  # reconnect with a fresh token; the workout continues
                return
            if len(raw) > MAX_MESSAGE_BYTES:
                await ws.close(code=CLOSE_TOO_BIG)
                return
            # A cheap pre-parse check routes anything claiming to be a user_signal to its own bucket; it is
            # still fully validated afterwards, so spoofing the text only gets you the (small) signal limit.
            limiter = signal_bucket if '"user_signal"' in raw else bucket
            if not limiter.allow():  # a flood: drop it, and say so (at most once per few seconds)
                now = time.monotonic()
                if now - last_rate_warning >= RATE_WARN_EVERY_S:
                    last_rate_warning = now
                    await _send(ws, ErrorEvent(code="rate_limited", detail="too many messages; slow down"))
                continue
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
                    agent = ws.app.state.agent
                    if agent.enabled:
                        _spawn_agent_delivery(ws, live, decision, agent)  # background: counting never waits on the LLM
                    else:
                        await _deliver_coaching(ws, live, decision)
    except WebSocketDisconnect:
        logger.info("ws disconnected session=%s", session_id)
    finally:
        if live is not None and live.socket is ws:  # not already replaced by a newer connection
            live.socket = None
            live.touch()
