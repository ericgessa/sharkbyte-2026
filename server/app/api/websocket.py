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
    DeviceCommand,
    DeviceCommandOut,
    ErrorEvent,
    RepCompleted,
    RepRejected,
    RepMetricsOut,
    SensorAuth,
    SensorLink,
    SensorLive,
    SensorNote,
    SensorReady,
    SessionReady,
    SessionState,
    TrackingStatus,
    UserSignal,
    incoming_adapter,
    sensor_bridge_adapter,
)
from app.schemas.sensors import SensorUpdate
from app.services.coaching import ACTION_RECOMMEND_REST, PAIN_ISSUE_CODE, PAIN_MESSAGE, CoachingDecision
from app.services.exercises import DEFAULT_EXERCISE, for_stored_name
from app.services import persistence
from app.services.ingest import pose_readings, sensor_readings
from app.services.persistence import Write
from app.services.posture import PostureCoach
from app.services.spine_form import SpineFormCoach
from app.services.ratelimit import TokenBucket
from app.services.rep_counter import PoseSample, RejectedRep, RejectReason, RepMetrics
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
# Posture cues from the upper-back IMU (services/posture.py). Its thresholds were chosen for squats only.
POSTURE_EXERCISES = frozenset({"squat"})
MAX_POSTURE_DEVICES = 2  # per workout; more distinct device_ids are stored but not coached
# A posture cue is skipped if any cue went out this recently, so the user never gets two at once. Camera cues
# are never held back by this (their own rule-coach rate limit is unchanged).
MIN_CUE_GAP_S = 6.0


async def _send(ws: WebSocket, event: BaseModel) -> None:
    await ws.send_json(event.model_dump(mode="json"))


def _metrics_dict(r: RepMetrics) -> dict:
    return {
        "min_knee_angle_deg": r.min_angle_deg if r.joint == "knee" else None,
        "min_angle_deg": r.min_angle_deg,
        "joint": r.joint,
        "descent_ms": r.descent_ms,
        "ascent_ms": r.ascent_ms,
        "duration_ms": r.duration_ms,
        "max_asymmetry_deg": r.max_asymmetry_deg,
    }


def _stored_metrics(live: LiveSession, r: RepMetrics, spine: dict | None = None) -> dict:
    """What is saved in Supabase for a rep: the metrics plus when the rep happened (server time),
    so time-series analytics can slice a session into reps, and the wearable's back-shape record for the rep
    (``spine``, when the wearable sent data). Not sent to the client."""
    out = {
        **_metrics_dict(r),
        "started_at": live.clock.to_datetime("camera", r.start_ts_ms).isoformat(),
        "ended_at": live.clock.to_datetime("camera", r.end_ts_ms).isoformat(),
    }
    if spine is not None:
        out["spine"] = spine
    return out


# Form IQ: what the user is told when an attempt is not counted (rep_counter.RejectReason).
REJECT_MESSAGES = {
    RejectReason.PARTIAL_RANGE: "That one didn't count: go a little lower so you reach the bottom.",
    RejectReason.TOO_FAST: "That one didn't count: it was too quick to track. Slow down a little.",
}
ACTION_REP_REJECTED = "rep_rejected"


def _camera_ms(live: LiveSession, ts_ms: int) -> float:
    return live.clock.to_datetime("camera", ts_ms).timestamp() * 1000


def _record_spine(live: LiveSession, event: SensorUpdate) -> None:
    """Buffer the wearable's section angles (server time) so each counted rep can be judged. Exercises without spine
    rules are skipped. The fields only exist once the wearable's schema (Hardware_branch) is merged: until then this
    reads nothing and does nothing."""
    d = event.data
    upper, mid, lumbar = (getattr(d, f, None) for f in ("spine_upper_deg", "spine_mid_deg", "spine_lumbar_deg"))
    if upper is None and mid is None and lumbar is None:
        return
    if live.spine is None:
        if live.exercise is None:
            return
        live.spine = SpineFormCoach(live.exercise.slug)
        live.spine.enabled = not live.pain_reported
    if live.spine.supported:
        server_ms = live.clock.to_datetime(f"device:{event.device_id}", event.timestamp_ms).timestamp() * 1000
        live.spine.add(server_ms, upper, mid, lumbar)


async def _report_rejected(ws: WebSocket, live: LiveSession, rejected: RejectedRep) -> None:
    """Tell the app an attempt didn't count and why, and save it so the end-of-workout report can mention it."""
    message = REJECT_MESSAGES[rejected.reason]
    reps = live.counter.rep_count
    await _send(
        ws,
        RepRejected(
            session_id=live.session_id,
            rep_count=reps,
            reason=rejected.reason.value,
            message=message,
            min_angle_deg=rejected.min_angle_deg,
            joint=rejected.joint,
            duration_ms=rejected.duration_ms,
        ),
    )
    _persist(
        ws,
        live,
        (persistence.COACHING_EVENT, (live.session_id, uuid4(), reps or None, rejected.reason.value, message, ACTION_REP_REJECTED)),
    )


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

    user_id = await asyncio.to_thread(ws.app.state.auth.verify, token)
    if user_id is None:
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None
    return await _load_live(ws, session_id, user_id)


async def _load_live(ws: WebSocket, session_id: UUID, user_id: str) -> LiveSession | None:
    """The user's active workout as a live session (created after a server restart). Closes the socket if it is
    missing, someone else's, or finished."""
    store = ws.app.state.store
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
    live.last_cue_at = time.monotonic()
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


async def _coach_posture(ws: WebSocket, live: LiveSession, event: SensorUpdate) -> None:
    """Feed one ESP32 sample to that device's posture coach; deliver a cue if the posture rules decided on one.

    Deterministic, like camera cues: the rule message is sent as-is (Gemini only writes the end-of-workout recap).
    A bug here is logged and never touches rep counting or sensor storage.
    """
    exercise = live.exercise
    if exercise is None or exercise.slug not in POSTURE_EXERCISES or event.data.imu_pitch_deg is None:
        return
    coach = live.posture.get(event.device_id)
    if coach is None:
        if len(live.posture) >= MAX_POSTURE_DEVICES:
            return
        coach = live.posture[event.device_id] = PostureCoach()
        coach.enabled = not live.pain_reported  # also after a server restart (pain is restored on connect)
    try:
        found = coach.update(event.timestamp_ms, event.data.imu_pitch_deg, event.data.imu_roll_deg)
    except Exception:
        logger.exception("posture analysis failed; the workout is unaffected")
        return
    if found is None:
        return
    now = time.monotonic()
    if live.last_cue_at is not None and now - live.last_cue_at < MIN_CUE_GAP_S:
        logger.info("posture cue %s skipped: another cue went out %.1f s ago", found.issue_code, now - live.last_cue_at)
        return
    # rep_count is the camera's count the app is showing; with the camera off (sensor only) it is the IMU's rep.
    decision = CoachingDecision(
        rep_number=live.counter.rep_count or found.rep_number,
        issue_code=found.issue_code,
        message=found.message,
        event_id=found.event_id,
    )
    logger.info("posture cue %s session=%s device=%s", found.issue_code, live.session_id, event.device_id)
    await _deliver_coaching(ws, live, decision)


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
        for posture in live.posture.values():
            posture.enabled = False
        if live.spine is not None:
            live.spine.enabled = False
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
                sensor_connected=live.sensor_socket is not None,
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
            if isinstance(event, DeviceCommand):  # e.g. Recalibrate, for the wearable on the laptop bridge
                bridge = live.sensor_socket
                if bridge is None:
                    await _send(ws, ErrorEvent(code="sensor_not_connected", detail="no sensor bridge is connected"))
                    continue
                try:
                    await _send(bridge, DeviceCommandOut(session_id=session_id, command=event.command))
                except Exception:  # the bridge just went away; its own handler tells the app
                    logger.info("device command for session %s not delivered", session_id)
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
                _record_spine(live, event)
                await _coach_posture(ws, live, event)
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
                    left_elbow_deg=d.left_elbow_angle_deg,
                    right_elbow_deg=d.right_elbow_angle_deg,
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
            if result.rejected is not None:
                await _report_rejected(ws, live, result.rejected)
            if result.rep is not None:
                r = result.rep
                # Tell the client first (low latency), then save.
                await _send(
                    ws,
                    RepCompleted(session_id=session_id, rep_count=r.rep_number, metrics=RepMetricsOut(**_metrics_dict(r))),
                )
                spine_form, spine_cue = None, None
                if live.spine is not None and live.spine.supported:
                    spine_form, spine_cue = live.spine.judge_rep(
                        r.rep_number, _camera_ms(live, r.start_ts_ms), _camera_ms(live, r.end_ts_ms)
                    )
                spine = spine_form.as_metrics() if spine_form is not None else None
                _persist(ws, live, (persistence.REP, (session_id, r.rep_number, _stored_metrics(live, r, spine))))

                decision = live.coach.observe(r)
                if decision is not None:  # the rule message, as-is: live cues never wait on an LLM
                    await _deliver_coaching(ws, live, decision)
                elif spine_cue is not None and (
                    live.last_cue_at is None or time.monotonic() - live.last_cue_at >= MIN_CUE_GAP_S
                ):
                    await _deliver_coaching(ws, live, spine_cue)  # one cue per rep at most; the camera's comes first
    except WebSocketDisconnect:
        logger.info("ws disconnected session=%s", session_id)
    finally:
        if live is not None and live.socket is ws:  # not already replaced by a newer connection
            live.socket = None
            live.touch()


# ------------------------------------------------------------------ laptop sensor bridge

SENSOR_FORWARD_HZ = 10.0  # live wearable readings forwarded to the app, for display (all of them are stored)


async def _tell_app(live: LiveSession, event: BaseModel) -> None:
    """Best effort: the app may not be connected, or may be reconnecting; it gets the state in session_ready."""
    target = live.socket
    if target is None:
        return
    try:
        await _send(target, event)
    except Exception:
        pass  # the app's own handler notices its socket is gone


async def _authenticate_bridge(ws: WebSocket) -> tuple[LiveSession, str] | None:
    """First message must be ``sensor_auth`` with a pairing code or a bridge token. Returns (live, token)."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        if len(raw) > MAX_MESSAGE_BYTES:
            await ws.close(code=CLOSE_TOO_BIG)
            return None
        msg = SensorAuth.model_validate_json(raw)
    except (asyncio.TimeoutError, ValidationError):
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None

    pairing = ws.app.state.sensor_pairing
    if msg.pair_code is not None:
        redeemed = pairing.redeem(msg.pair_code, client=ws.client.host if ws.client else "")
        pair, token = (redeemed[0], redeemed[1]) if redeemed else (None, "")
    else:
        pair, token = pairing.check_token(msg.bridge_token), msg.bridge_token or ""
    if pair is None:
        ended = msg.bridge_token is not None and pairing.is_revoked(msg.bridge_token)
        await ws.close(code=CLOSE_NOT_ACTIVE if ended else CLOSE_UNAUTHENTICATED)  # 'workout ended' vs 'unknown pairing'
        return None
    live = await _load_live(ws, pair.session_id, pair.user_id)  # still active, still this user's
    if live is None:
        pairing.revoke_session(pair.session_id)
        return None
    return live, token


@router.websocket("/ws/sensors")
async def sensor_bridge_socket(ws: WebSocket) -> None:
    """The laptop bridge (hardware/laptop/spine_bridge.py) streams the wearable here. Readings are stored like
    ones relayed by the app, and forwarded to the app's workout socket (throttled) so a phone can show them."""
    await ws.accept()
    live: LiveSession | None = None
    try:
        authed = await _authenticate_bridge(ws)
        if authed is None:
            return
        live, token = authed
        session_id = live.session_id
        old = live.sensor_socket
        live.sensor_socket = ws
        live.touch()
        if old is not None and old is not ws:  # one bridge per workout: the newest wins
            try:
                await old.close(code=CLOSE_REPLACED)
            except Exception:
                pass
        logger.info("sensor bridge connected session=%s", session_id)
        await _send(ws, SensorReady(session_id=session_id, bridge_token=token, exercise=live.exercise.slug))
        await _tell_app(live, SensorLink(session_id=session_id, connected=True))

        bucket = TokenBucket(RATE_PER_S, BURST)
        last_rate_warning = float("-inf")
        last_forward = float("-inf")
        connected_at = time.monotonic()
        while True:
            raw = await ws.receive_text()
            if live.sensor_socket is not ws:
                return
            live.touch()
            if live.finished:
                await ws.close(code=CLOSE_NOT_ACTIVE)
                return
            if time.monotonic() - connected_at > MAX_CONNECTION_S:
                await ws.close(code=CLOSE_UNAUTHENTICATED)  # reconnect with the token: re-checks the workout
                return
            if len(raw) > MAX_MESSAGE_BYTES:
                await ws.close(code=CLOSE_TOO_BIG)
                return
            if not bucket.allow():
                now = time.monotonic()
                if now - last_rate_warning >= RATE_WARN_EVERY_S:
                    last_rate_warning = now
                    await _send(ws, ErrorEvent(code="rate_limited", detail="too many messages; slow down"))
                continue
            try:
                event = sensor_bridge_adapter.validate_json(raw)
            except ValidationError as exc:
                fields = ",".join(".".join(map(str, e["loc"])) for e in exc.errors())
                await _send(ws, ErrorEvent(code="invalid_message", detail=f"invalid fields: {fields}"))
                continue
            if event.session_id != session_id:
                await _send(ws, ErrorEvent(code="session_mismatch", detail="session_id does not match this bridge"))
                continue

            if isinstance(event, SensorNote):
                await _tell_app(live, SensorLink(session_id=session_id, connected=True, note=event.note))
                continue

            if not live.paused:  # same rules as data relayed by the app
                _record(
                    ws,
                    lambda: sensor_readings(
                        session_id,
                        live.clock.to_datetime(f"device:{event.device_id}", event.timestamp_ms),
                        event.device_id,
                        event.data,
                    ),
                )
                # Per-rep back-shape analysis (saved with each rep in Supabase) and posture cues, exactly as for data
                # relayed by the app. Cues go to the APP's socket; with no app connected there is no one to tell.
                _record_spine(live, event)
                app_ws = live.socket
                if app_ws is not None:
                    try:
                        await _coach_posture(app_ws, live, event)
                    except Exception:
                        logger.exception("posture cue from bridge data failed; the workout is unaffected")
            now = time.monotonic()
            if now - last_forward >= 1.0 / SENSOR_FORWARD_HZ:  # the display keeps updating while paused
                last_forward = now
                await _tell_app(live, SensorLive(session_id=session_id, device_id=event.device_id, data=event.data))
    except WebSocketDisconnect:
        logger.info("sensor bridge disconnected session=%s", live.session_id if live else None)
    finally:
        if live is not None and live.sensor_socket is ws:
            live.sensor_socket = None
            live.touch()
            if not live.finished:
                await _tell_app(live, SensorLink(session_id=live.session_id, connected=False))
