"""The live workout pipeline with Tiger Data attached: association, batching, ESP32 relay, outages."""

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.ingest import SensorIngestor
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
    ingest = SensorIngestor(tiger, flush_interval_s=0.05)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger, ingest=ingest)) as c:
        c.ingest = ingest
        yield c


def new_session(client, headers=ALICE):
    return client.post("/api/workouts", json={"exercise": "squat"}, headers=headers).json()["session_id"]


def open_ready(client, sid):
    ws = client.websocket_connect(f"/ws/workouts/{sid}")
    sock = ws.__enter__()
    sock.send_text(json.dumps(auth_message()))
    assert sock.receive_json()["type"] == "session_ready"
    return ws, sock


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def sensor_msg(sid, seq, ts, **data):
    return {"schema_version": 1, "type": "sensor_update", "session_id": sid, "device_id": "esp32-01",
            "sequence": seq, "timestamp_ms": ts, "data": data}


# ---- association with the Supabase session UUID ------------------------------


def test_camera_measurements_are_stored_under_the_workout_session_uuid(client, tiger):
    sid = new_session(client)
    frames = list(squat_messages(sid, reps=1))
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in frames:
            ws.send_text(json.dumps(m))
        ws.receive_json()  # rep_completed
        assert wait_for(lambda: len(tiger.rows) >= 2 * (len(frames) - 10))
    assert tiger.rows
    assert {str(r.session_id) for r in tiger.rows} == {sid}  # the same UUID Supabase uses
    assert {r.metric_name for r in tiger.rows} == {"left_knee_angle", "right_knee_angle"}
    assert all(r.source == "camera" and r.time.tzinfo is not None for r in tiger.rows)
    first = frames[0]["data"]
    assert tiger.rows[0].metric_value in (first["left_knee_angle_deg"], first["right_knee_angle_deg"])
    assert tiger.rows[0].metadata == {"confidence": first["landmark_confidence"]}


def test_measurements_are_written_in_batches_not_one_at_a_time(client, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=1):
            ws.send_text(json.dumps(m))
        ws.receive_json()
        assert wait_for(lambda: len(tiger.rows) > 100)
    assert len(tiger.batches) < len(tiger.rows) / 5  # many readings per database round trip
    assert max(tiger.batches) > 1


def test_only_summaries_go_to_supabase_never_the_raw_stream(client, store, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=2):
            ws.send_text(json.dumps(m))
        [ws.receive_json() for _ in range(2)]
        assert wait_for(lambda: len(store.reps.get(sid, {})) == 2)
        assert wait_for(lambda: len(tiger.rows) > 100)  # the buffer flushes on a timer, so wait for it
    assert len(store.reps[sid]) == 2  # Supabase got 2 rep rows
    assert len(tiger.rows) > 100  # ...while Tiger Data got the high-frequency readings


def test_saved_reps_carry_server_time_windows_for_time_series_slicing(client, store):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=1):
            ws.send_text(json.dumps(m))
        ws.receive_json()
        assert wait_for(lambda: store.reps.get(sid))
    m = store.reps[sid][1]
    start, end = (datetime.fromisoformat(m[k]) for k in ("started_at", "ended_at"))
    assert start.tzinfo is not None and 0 < (end - start).total_seconds() < 5
    assert abs((datetime.now(timezone.utc) - end).total_seconds()) < 60  # anchored to server time


# ---- ESP32 data over the SAME socket ----------------------------------------


def test_esp32_sensor_updates_arrive_over_the_existing_socket(client, tiger):
    sid = new_session(client)
    ws_cm, ws = open_ready(client, sid)
    try:
        ws.send_text(json.dumps(sensor_msg(sid, 1, 5000, flex_raw=1780, imu_pitch_deg=12.4, accel_z_g=0.98)))
        ws.send_text(json.dumps(sensor_msg(sid, 2, 5020, flex_raw=1790)))
        assert wait_for(lambda: len(tiger.rows) == 4)
    finally:
        ws_cm.__exit__(None, None, None)
    by_metric = {}
    for r in tiger.rows:
        by_metric.setdefault(r.metric_name, []).append(r)
    assert set(by_metric) == {"flex_value", "imu_pitch", "accel_z"}
    assert all(str(r.session_id) == sid and r.sensor_id == "esp32-01" for r in tiger.rows)
    assert by_metric["flex_value"][0].source == "flex_sensor" and by_metric["imu_pitch"][0].source == "imu"
    a, b = by_metric["flex_value"]
    assert (b.time - a.time).total_seconds() == pytest.approx(0.02, abs=1e-6)  # spacing preserved


def test_invalid_sensor_update_gets_an_error_and_the_socket_stays_usable(client, tiger):
    sid = new_session(client)
    ws_cm, ws = open_ready(client, sid)
    try:
        ws.send_text(json.dumps(sensor_msg(sid, 1, 1, imu_pitch_deg=9999)))
        assert ws.receive_json()["code"] == "invalid_message"
        ws.send_text(json.dumps(sensor_msg(str(uuid4()), 2, 2, flex_raw=1)))  # another session's id
        assert ws.receive_json()["code"] == "session_mismatch"
        ws.send_text(json.dumps(sensor_msg(sid, 3, 3, flex_raw=1)))
        assert wait_for(lambda: len(tiger.rows) == 1)
    finally:
        ws_cm.__exit__(None, None, None)


def test_nothing_is_recorded_while_the_user_is_paused(client, tiger):
    sid = new_session(client)
    ws_cm, ws = open_ready(client, sid)
    try:
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
        assert ws.receive_json()["paused"] is True
        ws.send_text(json.dumps(sensor_msg(sid, 1, 1, flex_raw=5)))
        for m in list(squat_messages(sid, reps=1))[:20]:
            ws.send_text(json.dumps(m))
        time.sleep(0.3)
        assert tiger.rows == []
    finally:
        ws_cm.__exit__(None, None, None)


# ---- Tiger Data outages must not affect the live workout ---------------------


def test_tiger_outage_does_not_interrupt_counting_coaching_or_finishing(client, store, tiger):
    tiger.fail = True
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=5, depths=[85, 85, 85, 98, 98]):
            ws.send_text(json.dumps(m))
        events = [ws.receive_json() for _ in range(6)]
    assert [e["type"] for e in events] == ["rep_completed"] * 5 + ["coaching_feedback"]  # live feedback intact

    r = client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert r.status_code == 200 and r.json()["total_reps"] == 5  # Supabase side complete
    assert len(store.reps[sid]) == 5 and len(store.events[sid]) == 1
    assert client.ingest.stats.failed_batches >= 1 and tiger.rows == []


def test_readings_buffered_during_an_outage_are_delivered_when_it_ends(client, tiger):
    tiger.fail = True
    sid = new_session(client)
    ws_cm, ws = open_ready(client, sid)
    try:
        ws.send_text(json.dumps(sensor_msg(sid, 1, 1000, flex_raw=1)))
        ws.send_text(json.dumps(sensor_msg(sid, 2, 1020, flex_raw=2)))
        assert wait_for(lambda: client.ingest.stats.failed_batches >= 1)
        assert tiger.rows == []
        tiger.fail = False  # Tiger Data comes back
        assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 200  # finish flushes
    finally:
        ws_cm.__exit__(None, None, None)
    assert [r.metric_value for r in tiger.rows] == [1.0, 2.0]


def test_a_bug_inside_ingestion_cannot_break_the_workout(client, store, monkeypatch):
    def boom(_):
        raise RuntimeError("ingest bug")

    monkeypatch.setattr(client.ingest, "add", boom)
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=3):
            ws.send_text(json.dumps(m))
        assert [ws.receive_json()["rep_count"] for _ in range(3)] == [1, 2, 3]


def test_without_tiger_data_configured_the_app_works_exactly_as_before(store):
    off = FakeTiger(configured=False)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=off)) as c:
        sid = new_session(c)
        with c.websocket_connect(f"/ws/workouts/{sid}") as ws:
            ws.send_text(json.dumps(auth_message()))
            ws.receive_json()
            ws.send_text(json.dumps(sensor_msg(sid, 1, 1, flex_raw=1)))  # accepted and ignored
            for m in squat_messages(sid, reps=2):
                ws.send_text(json.dumps(m))
            assert [ws.receive_json()["rep_count"] for _ in range(2)] == [1, 2]
        assert c.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()["total_reps"] == 2
    assert off.rows == [] and off.batches == []


# ---- analytics endpoints ----------------------------------------------------


def test_analytics_endpoint_combines_supabase_and_tiger_data(client, tiger):
    tiger.result = lambda sql, params: (
        [{"n": 10, "avg_deg": 100.0, "min_deg": 85.0, "max_deg": 170.0, "stddev_deg": 30.0}] if "stddev_samp" in sql else []
    )
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=2):
            ws.send_text(json.dumps(m))
        [ws.receive_json() for _ in range(2)]
        client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    body = client.get(f"/api/workouts/{sid}/analytics", headers=ALICE).json()
    assert body["session_id"] == sid and body["supabase"]["stats"]["rep_count"] == 2
    assert body["timeseries"]["available"] is True and body["timeseries"]["knee_angle"]["min_deg"] == 85.0
    unnest = [p for s, p in tiger.queries if "unnest" in s]
    assert unnest and unnest[0]["nums"] == [1, 2]  # per-rep windows came from Supabase


def test_analytics_endpoint_survives_tiger_data_being_down_or_absent(client, store, tiger):
    sid = new_session(client)
    tiger.fail = True
    body = client.get(f"/api/workouts/{sid}/analytics", headers=ALICE).json()
    assert body["timeseries"] == {"available": False, "reason": "unavailable"} and "supabase" in body

    off = FakeTiger(configured=False)
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=off)) as c:
        body = c.get(f"/api/workouts/{sid}/analytics", headers=ALICE).json()
    assert body["timeseries"] == {"available": False, "reason": "not_configured"}


def test_analytics_and_readings_are_private_and_authenticated(client):
    sid = new_session(client)
    window = "start=2026-11-07T12:00:00Z&end=2026-11-07T12:01:00Z"
    for path in (f"/api/workouts/{sid}/analytics", f"/api/workouts/{sid}/readings?{window}"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=BOB).status_code == 404
        assert client.get(path, headers=ALICE).status_code == 200


def test_readings_endpoint_validates_the_window_and_returns_rows(client, tiger):
    sid = new_session(client)
    bad = client.get(f"/api/workouts/{sid}/readings?start=2026-11-07T12:01:00Z&end=2026-11-07T12:00:00Z", headers=ALICE)
    assert bad.status_code == 422
    assert client.get(f"/api/workouts/{sid}/readings?start=x&end=y", headers=ALICE).status_code == 422
    assert client.get(f"/api/workouts/{sid}/readings?start=2026-11-07T12:00:00Z&end=2026-11-07T12:01:00Z&limit=999999", headers=ALICE).status_code == 422

    t = datetime(2026, 11, 7, 12, 0, 5, tzinfo=timezone.utc)
    tiger.result = [{"time": t, "source": "camera", "sensor_id": "", "metric_name": "left_knee_angle", "metric_value": 91.0, "samples": 1}]
    ok = client.get(f"/api/workouts/{sid}/readings?start=2026-11-07T12:00:00Z&end=2026-11-07T12:01:00Z&metric=left_knee_angle&bucket_seconds=1", headers=ALICE).json()
    assert ok["available"] is True and ok["readings"][0]["metric"] == "left_knee_angle"
    params = tiger.queries[-1][1]
    assert str(params["sid"]) == sid and params["metrics"] == ["left_knee_angle"] and params["bucket"] == 1.0
