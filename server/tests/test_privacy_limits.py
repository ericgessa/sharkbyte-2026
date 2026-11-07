"""Deleting a workout from both databases, per-user request limits, maximum socket age."""

import json
import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import websocket as wsmod
from app.main import create_app
from app.services.ingest import SensorIngestor
from app.services.ratelimit import KeyedLimiter
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryStore, auth_message


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def tiger():
    return FakeTiger()


@pytest.fixture
def client(store, tiger):
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger, ingest=SensorIngestor(tiger, flush_interval_s=0.05))) as c:
        yield c


def new_session(client, headers=ALICE, exercise="squat"):
    return client.post("/api/workouts", json={"exercise": exercise}, headers=headers).json()["session_id"]


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def stream(client, sid, reps=2, wait_rows=True, tiger=None):
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=reps):
            ws.send_text(json.dumps(m))
        [ws.receive_json() for _ in range(reps)]
        if wait_rows and tiger is not None:
            assert wait_for(lambda: any(str(r.session_id) == sid for r in tiger.rows))


# ---- deleting a workout -----------------------------------------------------


def test_deleting_removes_the_workout_from_both_databases(client, store, tiger):
    keep, gone = new_session(client), new_session(client)
    stream(client, gone, tiger=tiger)
    stream(client, keep, tiger=tiger)
    client.post(f"/api/workouts/{gone}/finish", headers=ALICE)
    assert wait_for(lambda: len(store.reps.get(gone, {})) == 2)

    r = client.delete(f"/api/workouts/{gone}", headers=ALICE)
    assert r.status_code == 204 and r.content == b""

    # Supabase side: session, reps and coaching events are gone
    assert gone not in store.sessions and gone not in store.reps and gone not in store.events
    assert client.get(f"/api/workouts/{gone}", headers=ALICE).status_code == 404
    assert [w["session_id"] for w in client.get("/api/workouts", headers=ALICE).json()] == [keep]
    # Tiger Data side: that workout's readings are gone, the other workout's are untouched
    assert not any(str(x.session_id) == gone for x in tiger.rows)
    assert any(str(x.session_id) == keep for x in tiger.rows)
    assert keep in store.sessions and len(store.reps[keep]) == 2


def test_only_the_owner_can_delete_and_a_stranger_changes_nothing(client, store, tiger):
    sid = new_session(client)
    stream(client, sid, tiger=tiger)
    rows_before = len(tiger.rows)
    assert client.delete(f"/api/workouts/{sid}", headers=BOB).status_code == 404  # same as "does not exist"
    assert client.delete(f"/api/workouts/{sid}").status_code == 401
    assert sid in store.sessions and len(tiger.rows) == rows_before


def test_deleting_twice_or_an_unknown_workout_is_404(client):
    sid = new_session(client)
    assert client.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 204
    assert client.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 404
    assert client.delete("/api/workouts/00000000-0000-0000-0000-000000000000", headers=ALICE).status_code == 404


def test_deleting_a_streaming_workout_closes_the_socket_and_leaves_no_sensor_data(client, store, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=1):
            ws.send_text(json.dumps(m))
        ws.receive_json()  # rep_completed; some readings are still buffered, none may survive the delete
        assert client.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4409
    time.sleep(0.3)  # give any straggler a chance to (wrongly) land
    assert not any(str(r.session_id) == sid for r in tiger.rows)
    assert sid not in store.sessions and client.app.state.live.get(UUID(sid)) is None


def test_if_tiger_data_is_down_nothing_is_deleted_and_the_workout_stays_usable(client, store, tiger):
    sid = new_session(client)
    stream(client, sid, tiger=tiger)
    tiger.fail = True  # the workout is still active, as in a real mid-workout delete
    r = client.delete(f"/api/workouts/{sid}", headers=ALICE)
    assert r.status_code == 503 and "nothing was deleted" in r.json()["detail"]
    assert sid in store.sessions and len(store.reps[sid]) == 2  # Supabase untouched: no half-deleted workout
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:  # and it still works: a message gets a real reply
        ws.send_text(json.dumps(auth_message()))
        assert ws.receive_json()["type"] == "session_ready"
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
        assert ws.receive_json()["type"] == "session_state"  # not a 4409 close from a stale "deleting" flag
    tiger.fail = False  # Tiger Data is back: the retry succeeds
    assert client.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 204
    assert sid not in store.sessions and not any(str(x.session_id) == sid for x in tiger.rows)


def test_delete_works_when_tiger_data_is_not_configured(store):
    off = FakeTiger(configured=False)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=off)) as c:
        sid = new_session(c)
        assert c.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 204
    assert sid not in store.sessions


def test_cors_preflight_allows_delete_for_expo_web(client):
    r = client.options("/api/workouts/x", headers={"Origin": "http://localhost:8081", "Access-Control-Request-Method": "DELETE",
                                                    "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and "DELETE" in r.headers["access-control-allow-methods"]


# ---- per-user request limits ------------------------------------------------


def test_keyed_limiter_isolates_users_and_bounds_its_memory():
    t = [0.0]
    lim = KeyedLimiter(rate_per_s=1, burst=2, max_keys=3, now=lambda: t[0])
    assert [lim.allow("a") for _ in range(3)] == [True, True, False]
    assert lim.allow("b") is True  # another user is unaffected
    t[0] += 2
    assert lim.allow("a") is True  # refilled
    for k in ("c", "d", "e", "f"):
        lim.allow(k)
    assert len(lim._buckets) == 3  # old keys are forgotten, never unbounded
    assert lim.retry_after_s == 1 and KeyedLimiter(0.1, 1).retry_after_s == 10


def test_a_user_hammering_the_api_gets_429_with_retry_after_while_others_are_fine(client):
    client.app.state.rest_limiter = KeyedLimiter(rate_per_s=0.0001, burst=3)
    codes = [client.get("/api/workouts", headers=ALICE).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = client.get("/api/workouts", headers=ALICE)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "slow down" in r.json()["detail"]
    assert client.get("/api/workouts", headers=BOB).status_code == 200  # a different user is not affected
    assert client.get("/health").status_code == 200 and client.get("/api/exercises").status_code == 200  # public routes unlimited


def test_starting_workouts_has_its_own_tighter_limit(client):
    client.app.state.create_limiter = KeyedLimiter(rate_per_s=0.0001, burst=2)
    codes = [client.post("/api/workouts", json={}, headers=ALICE).status_code for _ in range(4)]
    assert codes == [201, 201, 429, 429]
    assert client.get("/api/workouts", headers=ALICE).status_code == 200  # reading is not limited by that


def test_default_limits_do_not_get_in_the_way_of_normal_use(client):
    for _ in range(40):  # far more than a real app does in a few seconds
        assert client.get("/api/workouts", headers=ALICE).status_code == 200
    for _ in range(10):
        assert client.post("/api/workouts", json={}, headers=ALICE).status_code == 201


# ---- maximum WebSocket age --------------------------------------------------


def test_a_connection_older_than_the_limit_is_closed_so_the_app_reconnects_and_re_authenticates(client, monkeypatch):
    monkeypatch.setattr(wsmod, "MAX_CONNECTION_S", 0.1)
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        time.sleep(0.2)
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401  # the same code as "refresh your token and reconnect"
    monkeypatch.setattr(wsmod, "MAX_CONNECTION_S", 3600)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:  # reconnecting is seamless
        ws.send_text(json.dumps(auth_message()))
        assert ws.receive_json()["type"] == "session_ready"


# ---- deleting a whole account ------------------------------------------------

CONFIRM = {"confirm": "delete my account"}


def delete_account(client, body=CONFIRM, headers=ALICE):
    return client.post("/api/account/delete", json=body, headers=headers)


def test_account_deletion_requires_the_exact_confirmation_phrase(client, store):
    new_session(client)
    for body in ({}, {"confirm": "yes"}, {"confirm": "Delete My Account"}, {"confirm": "delete my account", "x": 1}, None):
        r = client.post("/api/account/delete", json=body, headers=ALICE) if body is not None else client.post("/api/account/delete", headers=ALICE)
        assert r.status_code == 422, body
    assert delete_account(client, headers={}).status_code == 401
    assert store.list_all_session_ids("user-alice") and not getattr(store, "deleted_users", [])  # nothing happened


def test_deleting_an_account_removes_everything_of_that_user_and_only_that_user(client, store, tiger):
    a1, a2 = new_session(client), new_session(client)
    b1 = new_session(client, BOB)
    stream(client, a1, tiger=tiger)
    stream(client, a2, tiger=tiger)
    with client.websocket_connect(f"/ws/workouts/{b1}") as ws:  # Bob streams too, with his own token
        ws.send_text(json.dumps(auth_message("bob-token")))
        ws.receive_json()
        for m in squat_messages(b1, reps=1):
            ws.send_text(json.dumps(m))
        ws.receive_json()
        assert wait_for(lambda: any(str(r.session_id) == b1 for r in tiger.rows))
    assert wait_for(lambda: len(store.reps.get(a1, {})) == 2 and len(store.reps.get(a2, {})) == 2)

    r = delete_account(client)
    assert r.status_code == 204 and r.content == b""

    assert store.deleted_users == ["user-alice"]
    assert store.list_all_session_ids("user-alice") == [] and a1 not in store.sessions and a2 not in store.reps
    assert not any(str(x.session_id) in (a1, a2) for x in tiger.rows)  # Alice's sensor data is gone too
    assert b1 in store.sessions and any(str(x.session_id) == b1 for x in tiger.rows)  # Bob is untouched
    assert client.get("/api/workouts", headers=BOB).status_code == 200


def test_the_deleted_users_token_stops_working_immediately(client):
    new_session(client)
    assert client.get("/api/workouts", headers=ALICE).status_code == 200
    assert delete_account(client).status_code == 204
    assert client.app.state.auth.invalidated == ["user-alice"]
    assert client.get("/api/workouts", headers=ALICE).status_code == 401  # not "after the cache expires"


def test_deleting_an_account_closes_a_workout_that_is_streaming(client, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=1):
            ws.send_text(json.dumps(m))
        ws.receive_json()
        assert delete_account(client).status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4409
    time.sleep(0.3)
    assert not any(str(r.session_id) == sid for r in tiger.rows)
    assert client.app.state.live.get(UUID(sid)) is None


def test_if_tiger_data_is_down_the_account_is_not_deleted_and_can_be_retried(client, store, tiger):
    sid = new_session(client)
    stream(client, sid, tiger=tiger)
    tiger.fail = True
    r = delete_account(client)
    assert r.status_code == 503 and "nothing was deleted" in r.json()["detail"]
    assert sid in store.sessions and not getattr(store, "deleted_users", [])  # account and workouts intact
    assert client.get("/api/workouts", headers=ALICE).status_code == 200  # still logged in
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:  # and the workout still works
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
        assert ws.receive_json()["type"] == "session_state"
    tiger.fail = False
    assert delete_account(client).status_code == 204
    assert sid not in store.sessions and store.deleted_users == ["user-alice"]


def test_if_the_supabase_step_fails_after_sensor_data_is_deleted_the_user_is_told_and_can_retry(client, store, tiger):
    sid = new_session(client)
    stream(client, sid, tiger=tiger)
    store.fail_account_delete = True
    r = delete_account(client)
    assert r.status_code == 503 and "sensor data was deleted but the account could not be removed" in r.json()["detail"]
    assert not any(str(x.session_id) == sid for x in tiger.rows)  # no orphaned sensor data in either outcome
    store.fail_account_delete = False
    assert delete_account(client).status_code == 204 and store.deleted_users == ["user-alice"]


def test_account_deletion_works_without_tiger_data_and_for_a_user_with_no_workouts(store):
    off = FakeTiger(configured=False)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=off)) as c:
        new_session(c)
        assert delete_account(c).status_code == 204
        assert delete_account(c, headers=BOB).status_code == 204  # Bob never made a workout
    assert store.deleted_users == ["user-alice", "user-bob"]
