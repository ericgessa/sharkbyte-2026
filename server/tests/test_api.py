import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, InMemoryStore, auth_message


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def client(store):
    # Context manager keeps one event loop alive, so background save tasks and
    # the finish endpoint share it (as they do under uvicorn).
    with TestClient(create_app(store=store, auth=FakeAuth())) as c:
        yield c


def new_session(client, headers=ALICE) -> str:
    r = client.post("/api/workouts", json={"exercise": "squat"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def authed(ws, token="alice-token"):
    ws.send_text(json.dumps(auth_message(token)))
    ready = ws.receive_json()
    assert ready["type"] == "session_ready"
    return ready


def assert_closed(ws, code):
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == code


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ REST


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}])
def test_rest_requires_a_valid_bearer_token(client, headers):
    assert client.post("/api/workouts", json={}, headers=headers).status_code == 401
    assert client.get(f"/api/workouts/{uuid4()}", headers=headers).status_code == 401


def test_create_and_get_workout(client):
    sid = new_session(client)
    body = client.get(f"/api/workouts/{sid}", headers=ALICE).json()
    assert body["status"] == "active" and body["total_reps"] == 0 and body["exercise"] == "squat"


def test_other_users_session_looks_like_it_does_not_exist(client):
    sid = new_session(client)
    assert client.get(f"/api/workouts/{sid}", headers=BOB).status_code == 404
    assert client.post(f"/api/workouts/{sid}/finish", headers=BOB).status_code == 404
    assert client.get(f"/api/workouts/{uuid4()}", headers=ALICE).status_code == 404


def test_bad_exercise_is_422(client):
    assert client.post("/api/workouts", json={"exercise": "deadlift"}, headers=ALICE).status_code == 422


# ------------------------------------------------------------------ the headline flow


def test_five_reps_over_websocket_are_saved_and_session_finishes(client, store):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        assert authed(ws)["rep_count"] == 0
        for msg in squat_messages(sid, reps=5):
            ws.send_text(json.dumps(msg))
        events = [ws.receive_json() for _ in range(5)]

    assert [e["type"] for e in events] == ["rep_completed"] * 5
    assert [e["rep_count"] for e in events] == [1, 2, 3, 4, 5]
    assert wait_for(lambda: store.count_repetitions(sid) == 5)
    assert set(store.reps[sid]) == {1, 2, 3, 4, 5}
    assert store.reps[sid][1]["min_knee_angle_deg"] == events[0]["metrics"]["min_knee_angle_deg"]

    r = client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["total_reps"] == 5 and body["ended_at"]
    # idempotent
    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()["total_reps"] == 5


SHALLOWING = [85, 85, 85, 98, 98]  # last two reps clearly shallower than the first three


def stream_shallowing(client, sid):
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for msg in squat_messages(sid, reps=5, depths=SHALLOWING):
            ws.send_text(json.dumps(msg))
        return [ws.receive_json() for _ in range(6)]  # 5 reps + 1 coaching message


def test_coaching_feedback_is_sent_once_and_saved(client, store):
    sid = new_session(client)
    events = stream_shallowing(client, sid)

    assert [e["type"] for e in events] == ["rep_completed"] * 5 + ["coaching_feedback"]
    fb = events[5]
    assert fb["issue_code"] == "reduced_depth" and fb["rep_count"] == 5 and fb["source"] == "rules"
    assert "depth" in fb["message"].lower()

    assert wait_for(lambda: len(store.events.get(sid, {})) == 1 and store.count_repetitions(sid) == 5)
    (saved,) = store.events[sid].values()
    assert saved["issue_code"] == "reduced_depth" and saved["rep_number"] == 5
    assert saved["message"] == fb["message"] and saved["action"] == "send_feedback"


def test_steady_workout_produces_no_coaching_noise(client, store):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for msg in squat_messages(sid, reps=5):
            ws.send_text(json.dumps(msg))
        assert [ws.receive_json()["type"] for _ in range(5)] == ["rep_completed"] * 5
    client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert store.events.get(sid, {}) == {}


def test_outage_queues_reps_and_coaching_events_then_finish_saves_both(client, store):
    sid = new_session(client)
    store.fail_saves = True
    events = stream_shallowing(client, sid)
    assert events[5]["type"] == "coaching_feedback"  # the user still got the cue

    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 503
    store.fail_saves = False
    r = client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert r.status_code == 200 and r.json()["total_reps"] == 5
    assert len(store.events[sid]) == 1


def test_finish_waits_for_in_flight_saves(client, store):
    """Finishing right after the last rep event must not undercount."""
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for msg in squat_messages(sid, reps=3):
            ws.send_text(json.dumps(msg))
        for _ in range(3):
            ws.receive_json()
        r = client.post(f"/api/workouts/{sid}/finish", headers=ALICE)  # no wait_for on purpose
    assert r.json()["total_reps"] == 3


def test_finished_session_refuses_new_sockets(client):
    sid = new_session(client)
    client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        assert_closed(ws, 4409)


def test_database_outage_does_not_stop_counting_and_finish_retries(client, store):
    sid = new_session(client)
    store.fail_saves = True
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for msg in squat_messages(sid, reps=2):
            ws.send_text(json.dumps(msg))
        events = [ws.receive_json() for _ in range(2)]
    assert [e["rep_count"] for e in events] == [1, 2]  # counting unaffected

    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 503
    assert client.get(f"/api/workouts/{sid}", headers=ALICE).json()["status"] == "active"

    store.fail_saves = False  # database is back
    r = client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert r.status_code == 200 and r.json()["total_reps"] == 2


def test_reconnect_after_restart_continues_rep_numbers(client, store):
    sid = new_session(client)
    store.reps[sid] = {1: {}, 2: {}}  # two reps saved before a "restart" (no live state)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        assert authed(ws)["rep_count"] == 2
        for msg in squat_messages(sid, reps=1):
            ws.send_text(json.dumps(msg))
        assert ws.receive_json()["rep_count"] == 3


# ------------------------------------------------------------------ WebSocket auth


def test_ws_bad_token_is_closed_4401(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message("wrong")))
        assert_closed(ws, 4401)


def test_ws_first_message_must_be_auth(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(next(iter(squat_messages(sid, reps=1)))))
        assert_closed(ws, 4401)


def test_ws_someone_elses_or_unknown_session_is_4404(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message("bob-token")))
        assert_closed(ws, 4404)
    with client.websocket_connect(f"/ws/workouts/{uuid4()}") as ws:
        ws.send_text(json.dumps(auth_message()))
        assert_closed(ws, 4404)


# ------------------------------------------------------------------ message handling


def test_invalid_message_gets_error_and_socket_stays_open(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        ws.send_text(json.dumps({"type": "pose_update", "schema_version": 1}))
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "invalid_message"
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "invalid_message"
        ws.send_text(json.dumps(next(iter(squat_messages(sid, reps=1)))))  # still usable


def test_out_of_range_angle_rejected(client):
    sid = new_session(client)
    msg = next(iter(squat_messages(sid, reps=1)))
    msg["data"]["left_knee_angle_deg"] = 400
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        ws.send_text(json.dumps(msg))
        assert ws.receive_json()["code"] == "invalid_message"


def test_body_session_id_must_match_socket(client):
    sid = new_session(client)
    msg = next(iter(squat_messages(uuid4(), reps=1)))
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        ws.send_text(json.dumps(msg))
        assert ws.receive_json()["code"] == "session_mismatch"


def test_low_confidence_emits_tracking_events_once(client):
    sid = new_session(client)
    msgs = list(squat_messages(sid, reps=0, rest_frames=4))
    for m in msgs[1:3]:
        m["data"]["landmark_confidence"] = 0.1
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for m in msgs:
            ws.send_text(json.dumps(m))
        assert ws.receive_json()["tracking"] == "lost"
        assert ws.receive_json()["tracking"] == "recovered"
