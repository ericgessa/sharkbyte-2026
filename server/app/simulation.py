"""SIMULATED pose data for tests and the no-hardware demo.

Nothing here is real camera or sensor output. Angles follow a smooth cosine
curve between a standing angle and a chosen depth.

Run a demo against a live server:
    python -m app.simulation --reps 5 [--base-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any
from uuid import UUID

FRAME_MS = 33  # ~30 fps


def squat_messages(
    session_id: UUID | str,
    reps: int = 5,
    *,
    depth_deg: float = 85.0,
    standing_deg: float = 170.0,
    rep_ms: int = 2400,
    rest_frames: int = 10,
    confidence: float = 0.95,
    asymmetry_deg: float = 2.0,
    start_sequence: int = 0,
    start_ts: int = 1_794_000_000_000,
) -> Iterator[dict[str, Any]]:
    """Yield ``pose_update`` messages for ``reps`` squats, standing before and after each."""
    seq, ts = start_sequence, start_ts

    def frame(angle: float) -> dict[str, Any]:
        nonlocal seq, ts
        msg = {
            "schema_version": 1,
            "type": "pose_update",
            "session_id": str(session_id),
            "sequence": seq,
            "timestamp_ms": ts,
            "data": {
                "left_knee_angle_deg": round(angle - asymmetry_deg / 2, 2),
                "right_knee_angle_deg": round(angle + asymmetry_deg / 2, 2),
                "landmark_confidence": confidence,
            },
        }
        seq += 1
        ts += FRAME_MS
        return msg

    for _ in range(rest_frames):
        yield frame(standing_deg)
    for _ in range(reps):
        steps = rep_ms // FRAME_MS
        for i in range(1, steps + 1):
            phase = i / steps
            yield frame(standing_deg - (standing_deg - depth_deg) * 0.5 * (1 - math.cos(2 * math.pi * phase)))
        for _ in range(rest_frames):
            yield frame(standing_deg)


def _demo(base_url: str, reps: int) -> None:  # pragma: no cover - manual tool
    import argparse  # noqa: F401  (kept local; only the CLI needs it)
    import json
    import urllib.request

    from websockets.sync.client import connect

    req = urllib.request.Request(
        f"{base_url}/api/workouts",
        data=json.dumps({"exercise": "squat"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        session_id = json.load(resp)["session_id"]
    print(f"session {session_id} (SIMULATED pose data)")

    ws_url = base_url.replace("http", "ws", 1) + f"/ws/workouts/{session_id}"
    with connect(ws_url) as ws:
        for msg in squat_messages(session_id, reps):
            ws.send(json.dumps(msg))
            try:
                while True:
                    print(ws.recv(timeout=0.001))
            except TimeoutError:
                pass


if __name__ == "__main__":  # pragma: no cover
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = p.parse_args()
    _demo(args.base_url, args.reps)
