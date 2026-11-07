"""CROSS-LANGUAGE contract test: the messages the mobile app's TypeScript produces vs. the real backend.

Runs mobile/src/pose/emit-sample-messages.mjs with Node (type stripping, no npm install needed), then:
  * validates every message with the real Pydantic models (strict: unknown fields are rejected), and
  * streams the TypeScript-generated 5-squat workout through the REAL server over a WebSocket and requires 5 counted
    reps and a coaching cue, with timestamps and sequence numbers exactly as the TypeScript produced them.
Skipped when Node 22.6+ (needed for type stripping) or the mobile code is not available.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.events import AuthMessage, incoming_adapter
from tests.fakes import ALICE, FakeAuth, FakeTiger, InMemoryStore

MOBILE = Path(__file__).resolve().parents[2] / "mobile"
SCRIPT = MOBILE / "src" / "pose" / "emit-sample-messages.mjs"
NODE = shutil.which("node")


def _node_supports_type_stripping() -> bool:
    if not NODE:
        return False
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)\.(\d+)", out)
    return bool(m) and (int(m[1]), int(m[2])) >= (22, 6)


pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and _node_supports_type_stripping()),
    reason="needs Node 22.6+ and the mobile/ folder (the TypeScript is run directly, no npm install)",
)


def emit(session_id: str) -> dict:
    r = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", "--import", "./src/pose/test-register.mjs", "src/pose/emit-sample-messages.mjs", session_id],
        cwd=MOBILE, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def sample():
    return emit(str(uuid4()))


def test_every_typescript_message_is_valid_for_the_real_server_models(sample):
    assert len(sample["stream"]) > 400
    for m in sample["stream"]:
        incoming_adapter.validate_python(m)  # extra="forbid": a stray or misspelled field would fail here
    for m in sample["signals"]:
        parsed = incoming_adapter.validate_python(m)
        assert parsed.type == "user_signal"
    assert [m["signal"] for m in sample["signals"]] == ["pause", "resume", "pain"]
    auth = AuthMessage.model_validate(sample["auth"])
    assert auth.access_token == "a-token"
    upper = incoming_adapter.validate_python(sample["upper"])  # elbow + hip angles, measured from landmarks
    assert upper.data.left_elbow_angle_deg == pytest.approx(90, abs=0.1) and upper.data.left_hip_angle_deg == pytest.approx(180, abs=0.1)


def test_sequence_and_timestamps_are_strictly_increasing_as_the_server_requires(sample):
    seqs = [m["sequence"] for m in sample["stream"]]
    times = [m["timestamp_ms"] for m in sample["stream"]]
    assert seqs == list(range(len(seqs)))
    assert all(b > a for a, b in zip(times, times[1:]))
    # at the 30 fps cap or slower, so the server's flood limit is never in play
    assert min(b - a for a, b in zip(times, times[1:])) >= 33


def test_the_typescript_stream_drives_the_real_server_to_five_reps_and_a_coaching_cue():
    store = InMemoryStore()
    tiger = FakeTiger(configured=False)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger)) as client:
        sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]
        msgs = emit(sid)
        with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
            ws.send_text(json.dumps({**msgs["auth"], "access_token": "alice-token"}))  # swap in the fake auth's token
            assert ws.receive_json()["type"] == "session_ready"
            for m in msgs["stream"]:
                ws.send_text(json.dumps(m))
            events = [ws.receive_json() for _ in range(6)]
        types = [e["type"] for e in events]
        assert types.count("rep_completed") == 5, types
        assert [e["rep_count"] for e in events if e["type"] == "rep_completed"] == [1, 2, 3, 4, 5]
        cue = next(e for e in events if e["type"] == "coaching_feedback")
        assert cue["issue_code"] == "reduced_depth" and cue["source"] == "rules"  # depth dropped on the last two reps
        end = time.time() + 3
        while time.time() < end and len(store.reps.get(sid, {})) < 5:
            time.sleep(0.02)
        assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()["total_reps"] == 5
