"""SIMULATED pose data for tests and the no-hardware demo.

Nothing here is real camera or sensor output. Angles follow a smooth cosine
curve between a standing angle and a chosen depth.

Stream to a running server (needs a real Supabase user access token):
    python -m app.simulation --reps 5 --token <access_token> [--base-url http://127.0.0.1:8000]
Or run the whole thing in one command against Supabase (throwaway user):
    python -m scripts.e2e_live
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator, Sequence
from typing import Any
from uuid import UUID

FRAME_MS = 33  # ~30 fps


def squat_messages(
    session_id: UUID | str,
    reps: int = 5,
    *,
    depth_deg: float = 85.0,
    depths: Sequence[float] | None = None,  # per-rep depth; overrides depth_deg
    standing_deg: float = 170.0,
    rep_ms: int = 2400,
    rest_frames: int = 10,
    confidence: float = 0.95,
    asymmetry_deg: float = 2.0,
    start_sequence: int = 0,
    start_ts: int = 1_794_000_000_000,
    joint: str = "knee",  # "elbow" simulates a push-up (see pushup_messages)
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
                f"left_{joint}_angle_deg": round(angle - asymmetry_deg / 2, 2),
                f"right_{joint}_angle_deg": round(angle + asymmetry_deg / 2, 2),
                "landmark_confidence": confidence,
            },
        }
        seq += 1
        ts += FRAME_MS
        return msg

    for _ in range(rest_frames):
        yield frame(standing_deg)
    for rep_index in range(reps):
        rep_depth = depths[rep_index] if depths is not None else depth_deg
        steps = rep_ms // FRAME_MS
        for i in range(1, steps + 1):
            phase = i / steps
            yield frame(standing_deg - (standing_deg - rep_depth) * 0.5 * (1 - math.cos(2 * math.pi * phase)))
        for _ in range(rest_frames):
            yield frame(standing_deg)


def pushup_messages(session_id: UUID | str, reps: int = 5, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Simulated push-ups: the ELBOW angle goes from arms extended down to about 80 degrees and back."""
    kwargs.setdefault("depth_deg", 80.0)
    kwargs.setdefault("standing_deg", 165.0)
    kwargs.setdefault("rep_ms", 1800)
    yield from squat_messages(session_id, reps, joint="elbow", **kwargs)


def _demo(base_url: str, reps: int, token: str) -> None:  # pragma: no cover - manual tool
    import json
    import urllib.request

    from websockets.sync.client import connect

    req = urllib.request.Request(
        f"{base_url}/api/workouts",
        data=json.dumps({"exercise": "squat"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        session_id = json.load(resp)["session_id"]
    print(f"session {session_id} (SIMULATED pose data)")

    ws_url = base_url.replace("http", "ws", 1) + f"/ws/workouts/{session_id}"
    with connect(ws_url) as ws:
        ws.send(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
        print(ws.recv())  # session_ready
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
    p.add_argument("--token", default=os.environ.get("ACCESS_TOKEN"), help="Supabase access token (or set ACCESS_TOKEN)")
    args = p.parse_args()
    if not args.token:
        p.error("an access token is required (--token or ACCESS_TOKEN). For a one-command check use: python -m scripts.e2e_live")
    _demo(args.base_url, args.reps, args.token)
