import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.simulation import squat_messages


@pytest.fixture
def client():
    return TestClient(create_app())


def new_session(client) -> str:
    r = client.post("/api/workouts", json={"exercise": "squat"})
    assert r.status_code == 201
    return r.json()["session_id"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_workout(client):
    sid = new_session(client)
    body = client.get(f"/api/workouts/{sid}").json()
    assert body["status"] == "active" and body["total_reps"] == 0


def test_unknown_workout_404_and_bad_exercise_422(client):
    assert client.get(f"/api/workouts/{uuid4()}").status_code == 404
    assert client.post("/api/workouts", json={"exercise": "deadlift"}).status_code == 422


def test_five_rep_demo_over_websocket(client):
    sid = new_session(client)
    received = []
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        for msg in squat_messages(sid, reps=5):
            ws.send_text(json.dumps(msg))
        # All sends are processed in order; ask the counter via REST afterwards,
        # and drain the 5 rep events that were queued.
        for _ in range(5):
            received.append(ws.receive_json())

    assert [e["type"] for e in received] == ["rep_completed"] * 5
    assert [e["rep_count"] for e in received] == [1, 2, 3, 4, 5]
    assert received[0]["session_id"] == sid
    assert client.get(f"/api/workouts/{sid}").json()["total_reps"] == 5


def test_invalid_message_gets_error_and_socket_stays_open(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps({"type": "pose_update", "schema_version": 1}))
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "invalid_message"
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "invalid_message"
        # still usable
        first = next(iter(squat_messages(sid, reps=1)))
        ws.send_text(json.dumps(first))


def test_out_of_range_angle_rejected(client):
    sid = new_session(client)
    msg = next(iter(squat_messages(sid, reps=1)))
    msg["data"]["left_knee_angle_deg"] = 400
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(msg))
        assert ws.receive_json()["code"] == "invalid_message"


def test_body_session_id_must_match_socket(client):
    sid = new_session(client)
    msg = next(iter(squat_messages(uuid4(), reps=1)))
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(msg))
        assert ws.receive_json()["code"] == "session_mismatch"


def test_unknown_session_socket_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/workouts/{uuid4()}"):
            pass
    assert exc.value.code == 4404


def test_low_confidence_emits_tracking_events_once(client):
    sid = new_session(client)
    msgs = list(squat_messages(sid, reps=0, rest_frames=4))
    for m in msgs[1:3]:
        m["data"]["landmark_confidence"] = 0.1
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        for m in msgs:
            ws.send_text(json.dumps(m))
        assert ws.receive_json()["tracking"] == "lost"
        assert ws.receive_json()["tracking"] == "recovered"
