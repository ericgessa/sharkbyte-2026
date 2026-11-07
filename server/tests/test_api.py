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


# ------------------------------------------------------------------ CORS (Expo web)


def test_cors_preflight_allows_expo_web_with_auth_header(client):
    r = client.options(
        "/api/workouts",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers  # bearer tokens, never cookies


def test_cors_can_be_restricted_by_env(monkeypatch, store):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    with TestClient(create_app(store=store, auth=FakeAuth())) as c:
        ok = c.get("/health", headers={"Origin": "https://app.example.com"})
        other = c.get("/health", headers={"Origin": "https://evil.example.com"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-origin" not in other.headers


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


def test_unknown_exercise_is_422(client):
    assert client.post("/api/workouts", json={"exercise": "burpee"}, headers=ALICE).status_code == 422


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


def test_summary_has_reps_coaching_and_stats(client, store):
    sid = new_session(client)
    stream_shallowing(client, sid)
    client.post(f"/api/workouts/{sid}/finish", headers=ALICE)

    r = client.get(f"/api/workouts/{sid}/summary", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert body["workout"]["status"] == "completed" and body["workout"]["total_reps"] == 5
    assert [x["rep_number"] for x in body["reps"]] == [1, 2, 3, 4, 5]
    assert "min_knee_angle_deg" in body["reps"][0]["metrics"]
    assert [e["issue_code"] for e in body["coaching_events"]] == ["reduced_depth"]
    stats = body["stats"]
    assert stats["rep_count"] == 5 and stats["depth_change_deg"] > 5  # later reps shallower
    assert stats["deepest_knee_angle_deg"] < stats["shallowest_knee_angle_deg"]


def test_summary_of_a_workout_with_no_reps_is_valid(client):
    sid = new_session(client)
    body = client.get(f"/api/workouts/{sid}/summary", headers=ALICE).json()
    assert body["reps"] == [] and body["coaching_events"] == []
    assert body["stats"]["rep_count"] == 0 and body["stats"]["avg_min_knee_angle_deg"] is None


def test_summary_is_private_and_authenticated(client):
    sid = new_session(client)
    assert client.get(f"/api/workouts/{sid}/summary", headers=BOB).status_code == 404
    assert client.get(f"/api/workouts/{sid}/summary").status_code == 401
    assert client.get(f"/api/workouts/{uuid4()}/summary", headers=ALICE).status_code == 404


def test_list_workouts_returns_only_my_sessions_newest_first(client):
    first, second = new_session(client), new_session(client)
    bobs = new_session(client, BOB)
    ids = [w["session_id"] for w in client.get("/api/workouts", headers=ALICE).json()]
    assert ids == [second, first] and bobs not in ids
    assert [w["session_id"] for w in client.get("/api/workouts", headers=BOB).json()] == [bobs]


def test_list_workouts_limit_and_auth(client):
    for _ in range(3):
        new_session(client)
    assert len(client.get("/api/workouts?limit=2", headers=ALICE).json()) == 2
    assert client.get("/api/workouts?limit=0", headers=ALICE).status_code == 422
    assert client.get("/api/workouts?limit=101", headers=ALICE).status_code == 422
    assert client.get("/api/workouts").status_code == 401


def test_list_shows_live_rep_count_for_a_streaming_workout(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        for msg in squat_messages(sid, reps=2):
            ws.send_text(json.dumps(msg))
        [ws.receive_json() for _ in range(2)]
        listed = client.get("/api/workouts", headers=ALICE).json()
    assert listed[0]["total_reps"] == 2  # not 0: DB total is only written at finish


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


# ------------------------------------------------------------------ user signals: pain / pause / resume


def signal(ws, sid, name):
    ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": name}))


def test_pain_stops_technique_coaching_and_recommends_rest(client, store):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        signal(ws, sid, "pain")
        fb, state = ws.receive_json(), ws.receive_json()
        assert fb["type"] == "coaching_feedback" and fb["issue_code"] == "pain_reported"
        assert "stop" in fb["message"].lower() and "health professional" in fb["message"].lower()
        assert state == {**state, "type": "session_state", "coaching_enabled": False, "paused": False}

        # The shallowing workout would normally trigger a depth message. Now: reps only.
        for msg in squat_messages(sid, reps=5, depths=SHALLOWING):
            ws.send_text(json.dumps(msg))
        assert [ws.receive_json()["type"] for _ in range(5)] == ["rep_completed"] * 5

        signal(ws, sid, "pain")  # pressing again re-shows the message but must not duplicate the record
        assert ws.receive_json()["issue_code"] == "pain_reported"
        assert ws.receive_json()["type"] == "session_state"

    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 200
    saved = list(store.events[sid].values())
    assert [e["issue_code"] for e in saved] == ["pain_reported"]  # no depth message, one pain record
    assert saved[0]["action"] == "recommend_rest" and saved[0]["rep_number"] is None


def test_pain_report_survives_a_server_restart(store):
    sid = None
    with TestClient(create_app(store=store, auth=FakeAuth())) as first:
        sid = new_session(first)
        with first.websocket_connect(f"/ws/workouts/{sid}") as ws:
            authed(ws)
            signal(ws, sid, "pain")
            ws.receive_json(), ws.receive_json()
            assert wait_for(lambda: len(store.events.get(sid, {})) == 1)

    # "Restart": brand-new app and empty live state, same database.
    with TestClient(create_app(store=store, auth=FakeAuth())) as second:
        with second.websocket_connect(f"/ws/workouts/{sid}") as ws:
            assert authed(ws)["coaching_enabled"] is False
            for msg in squat_messages(sid, reps=5, depths=SHALLOWING):
                ws.send_text(json.dumps(msg))
            assert [ws.receive_json()["type"] for _ in range(5)] == ["rep_completed"] * 5  # still no coaching


def test_pause_ignores_measurements_until_resume(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        signal(ws, sid, "pause")
        assert ws.receive_json()["paused"] is True
        for msg in squat_messages(sid, reps=3):  # would be 3 reps if counted
            ws.send_text(json.dumps(msg))
        signal(ws, sid, "resume")
        assert ws.receive_json()["paused"] is False
        for msg in squat_messages(sid, reps=1, start_sequence=10_000, start_ts=1_794_100_000_000):
            ws.send_text(json.dumps(msg))
        ev = ws.receive_json()
    assert ev["type"] == "rep_completed" and ev["rep_count"] == 1  # the paused reps never counted


def test_unknown_signal_is_rejected_and_other_sessions_signal_is_mismatch(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        authed(ws)
        signal(ws, sid, "explode")
        assert ws.receive_json()["code"] == "invalid_message"
        signal(ws, str(uuid4()), "pain")
        assert ws.receive_json()["code"] == "session_mismatch"


# ------------------------------------------------------------------ helpful errors for new frontend devs


def test_401_messages_tell_you_how_to_get_a_token(client):
    missing = client.post("/api/workouts", json={})
    assert missing.status_code == 401 and "Bearer" in missing.json()["detail"] and "frontend-integration" in missing.json()["detail"]
    bad = client.post("/api/workouts", json={}, headers={"Authorization": "Bearer sb_publishable_abc"})
    assert bad.status_code == 401 and "access_token" in bad.json()["detail"] and "publishable" in bad.json()["detail"]


def test_root_explains_the_service_instead_of_a_bare_404(client):
    r = client.get("/")
    assert r.status_code == 200 and r.json()["service"] == "SharkByte API" and "Bearer" in r.json()["auth"]


# ------------------------------------------------------------------ readiness


def test_readiness_reports_both_databases_and_is_ok_when_they_are_up(store):
    from tests.fakes import FakeTiger

    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=FakeTiger())) as c:
        r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "supabase": "ok", "tiger_data": "ok", "sensor_writes": "healthy"}


def test_tiger_data_being_down_or_absent_never_makes_the_instance_unready(store):
    from tests.fakes import FakeTiger

    down = FakeTiger()
    down.fail = True
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=down)) as c:
        r = c.get("/health/ready")
    assert r.status_code == 200 and r.json()["tiger_data"] == "unavailable" and r.json()["status"] == "ok"
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=FakeTiger(configured=False))) as c:
        assert c.get("/health/ready").json()["tiger_data"] == "not_configured"


def test_supabase_being_down_makes_the_instance_unready_with_503(store):
    store.ping_ok = False
    with TestClient(create_app(store=store, auth=FakeAuth())) as c:
        r = c.get("/health/ready")
    assert r.status_code == 503 and r.json()["supabase"] == "unavailable" and r.json()["status"] == "unavailable"


def test_readiness_is_cached_so_it_cannot_be_used_to_hammer_the_databases(store):
    with TestClient(create_app(store=store, auth=FakeAuth())) as c:
        for _ in range(25):
            assert c.get("/health/ready").status_code == 200
        assert store.ping_calls == 1  # 25 requests, one database check
        c.app.state.ready_ttl_s = 0  # cache lifetime over
        c.get("/health/ready")
        assert store.ping_calls == 2


def test_readiness_leaks_no_secrets_or_details(client):
    body = json.dumps(client.get("/health/ready").json())
    for forbidden in ("postgres", "supabase.co", "sb_", "password", "eyJ", "@"):
        assert forbidden not in body
    assert client.get("/health").json() == {"status": "ok"}  # liveness stays dependency-free
