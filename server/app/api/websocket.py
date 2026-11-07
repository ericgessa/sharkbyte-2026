from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from app.schemas.events import (
    ErrorEvent,
    PoseUpdate,
    RepCompleted,
    RepMetricsOut,
    TrackingStatus,
)
from app.services.rep_counter import PoseSample

logger = logging.getLogger(__name__)
router = APIRouter()

WS_CLOSE_UNKNOWN_SESSION = 4404


async def _send(ws: WebSocket, event: BaseModel) -> None:
    await ws.send_json(event.model_dump(mode="json"))


@router.websocket("/ws/workouts/{session_id}")
async def workout_socket(ws: WebSocket, session_id: UUID) -> None:
    # Phase 1: no auth. Phase 2 must authenticate the user and check they own
    # this session *before* accept().
    session = ws.app.state.sessions.get(session_id)
    if session is None:
        await ws.close(code=WS_CLOSE_UNKNOWN_SESSION)
        return

    await ws.accept()
    counter = session.counter
    logger.info("ws connected session=%s", session_id)

    try:
        while True:
            raw = await ws.receive_text()
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
                await _send(
                    ws,
                    RepCompleted(
                        session_id=session_id,
                        rep_count=r.rep_number,
                        metrics=RepMetricsOut(
                            min_knee_angle_deg=r.min_knee_angle_deg,
                            descent_ms=r.descent_ms,
                            ascent_ms=r.ascent_ms,
                            duration_ms=r.duration_ms,
                            max_asymmetry_deg=r.max_asymmetry_deg,
                        ),
                    ),
                )
    except WebSocketDisconnect:
        logger.info("ws disconnected session=%s reps=%d", session_id, counter.rep_count)
