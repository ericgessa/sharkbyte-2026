"""The laptop sensor bridge: pairing codes, the /ws/sensors socket, and forwarding readings to the app."""

import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.services.ingest import SensorIngestor
from app.services import netinfo
from app.services.sensor_pairing import (
    CODE_TTL_S,
    MAX_FAILURES_PER_CLIENT,
    MAX_FAILURES_PER_WINDOW,
    TOKEN_TTL_S,
    Pairing,
    SensorPairing,
    bridge_secret,
    normalize_code,
)
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryStore, auth_message


@pytest.fixture
def tiger():
    return FakeTiger()


@pytest.fixture
def client(tiger):
    ingest = SensorIngestor(tiger, flush_interval_s=0.05)
    with TestClient(create_app(store=InMemoryStore(), auth=FakeAuth(), timeseries=tiger, ingest=ingest)) as c:
        yield c


def new_session(client, headers=ALICE):
    return client.post("/api/workouts", json={"exercise": "squat"}, headers=headers).json()["session_id"]


def get_code(client, sid, headers=ALICE):
    r = client.post(f"/api/workouts/{sid}/sensor-code", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def bridge_auth(**over):
    return json.dumps({"schema_version": 1, "type": "sensor_auth", **over})


def sensor_msg(sid, seq, ts, **data):
    return json.dumps({"schema_version": 1, "type": "sensor_update", "session_id": sid, "device_id": "esp32-spine",
                       "sequence": seq, "timestamp_ms": ts, "data": data or {"spine_lumbar_deg": 5.0}})


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- pairing service ---------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_code_works_once_and_is_forgiving_about_case_and_dashes():
    p, sid = SensorPairing(), uuid4()
    code = p.issue_code(sid, "u1")
    assert len(code) == 8 and normalize_code(f" {code[:4].lower()}-{code[4:]} ") == code
    pair, token = p.redeem(f"{code[:4].lower()}-{code[4:]}")
    assert (pair.session_id, pair.user_id) == (sid, "u1")
    assert p.check_token(token) == pair
    assert p.redeem(code) is None  # used


def test_codes_expire_and_a_new_code_replaces_the_old_one():
    clock = Clock()
    p, sid = SensorPairing(now=clock), uuid4()
    old = p.issue_code(sid, "u1")
    new = p.issue_code(sid, "u1")
    assert p.redeem(old) is None
    clock.t += CODE_TTL_S + 1
    assert p.redeem(new) is None


def test_revoking_a_session_ends_its_codes_and_tokens():
    p, sid, other = SensorPairing(), uuid4(), uuid4()
    _, token = p.redeem(p.issue_code(sid, "u1"))
    code = p.issue_code(sid, "u1")
    _, other_token = p.redeem(p.issue_code(other, "u1"))
    p.revoke_session(sid)
    assert p.check_token(token) is None and p.redeem(code) is None
    assert p.check_token(other_token) is not None


def test_a_flood_of_wrong_codes_locks_that_client_for_a_while_without_burning_the_real_code():
    clock = Clock()
    p, sid = SensorPairing(now=clock), uuid4()
    code = p.issue_code(sid, "u1")
    for _ in range(MAX_FAILURES_PER_CLIENT):
        assert p.redeem("WRONGWRONG", client="10.0.0.9") is None
    assert p.redeem(code, client="10.0.0.9") is None  # this client is locked
    clock.t += 61
    assert p.redeem(code, client="10.0.0.9") is not None  # still usable afterwards


def test_one_guessing_or_mistyping_client_cannot_lock_everyone_else_out():
    p, sid = SensorPairing(), uuid4()
    code = p.issue_code(sid, "u1")
    for _ in range(MAX_FAILURES_PER_CLIENT * 5):
        p.redeem("WRONGWRONG", client="203.0.113.7")
    assert p.redeem(code, client="192.168.1.40") is not None  # the real user's laptop pairs fine


def test_a_distributed_flood_still_hits_the_shared_ceiling():
    clock = Clock()
    p, sid = SensorPairing(now=clock), uuid4()
    code = p.issue_code(sid, "u1")
    for i in range(MAX_FAILURES_PER_WINDOW):
        p.redeem("WRONGWRONG", client=f"198.51.100.{i}")
    assert p.redeem(code, client="192.168.1.40") is None
    clock.t += 61
    assert p.redeem(code, client="192.168.1.40") is not None


def test_a_bridge_token_survives_a_server_restart_with_the_same_secret():
    secret, sid = b"k" * 32, uuid4()
    before = SensorPairing(secret=secret)
    pair, token = before.redeem(before.issue_code(sid, "u1"))
    after = SensorPairing(secret=secret)  # a new process: nothing in memory
    assert after.check_token(token) == pair
    assert SensorPairing(secret=b"x" * 32).check_token(token) is None  # another server's secret


def test_forged_tampered_or_expired_tokens_are_refused():
    now = [1_800_000_000.0]
    p, sid = SensorPairing(secret=b"k" * 32, wall=lambda: now[0]), uuid4()
    _, token = p.redeem(p.issue_code(sid, "u1"))
    version, payload, sig = token.split(".")
    other = SensorPairing(secret=b"k" * 32).make_token(Pairing(uuid4(), "mallory"))
    for bad in ("", "x", "v1..", f"v2.{payload}.{sig}", f"{version}.{payload}.{sig[:-2]}AA",
                f"{version}.{other.split('.')[1]}.{sig}", token + "x"):
        assert p.check_token(bad) is None, bad
    now[0] += TOKEN_TTL_S + 1
    assert p.check_token(token) is None


def test_the_secret_comes_from_the_environment_and_is_never_the_supabase_key_itself(monkeypatch):
    monkeypatch.delenv("SENSOR_BRIDGE_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    assert bridge_secret() is None
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_abc")
    derived = bridge_secret()
    assert derived and b"sb_secret_abc" not in derived and derived == bridge_secret()
    monkeypatch.setenv("SENSOR_BRIDGE_SECRET", "team-secret")
    assert bridge_secret() != derived


# ---- REST ----------------------------------------------------------------------


def test_only_the_owner_can_get_a_code_and_only_for_an_active_workout(client):
    sid = new_session(client)
    assert client.post(f"/api/workouts/{sid}/sensor-code").status_code == 401
    assert client.post(f"/api/workouts/{sid}/sensor-code", headers=BOB).status_code == 404
    assert client.post(f"/api/workouts/{sid}/sensor-code", headers=ALICE).status_code == 200
    client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    assert client.post(f"/api/workouts/{sid}/sensor-code", headers=ALICE).status_code == 409


def test_the_code_reply_says_how_a_bridge_on_another_computer_reaches_the_server(client):
    sid = new_session(client)
    body = client.post(f"/api/workouts/{sid}/sensor-code", headers=ALICE).json()
    assert body["server_urls"] and all(u.startswith("http") for u in body["server_urls"])
    assert body["bridge_command"] == f"python hardware/laptop/spine_bridge.py --server {body['server_urls'][0]} --code {body['code']}"


def test_bridge_addresses_never_offer_localhost_when_a_network_address_is_known(monkeypatch):
    monkeypatch.setattr(netinfo, "accepts_from_network", lambda ip, port, now=None: True)
    urls, warning = netinfo.bridge_addresses("http://localhost:8000/", "localhost", 8000, ips=["192.168.1.20"])
    assert urls == ["http://192.168.1.20:8000"] and warning is None
    # The app reached the server at a LAN address or a tunnel: that address comes first.
    urls, _ = netinfo.bridge_addresses("https://abc.trycloudflare.com/", "abc.trycloudflare.com", 8000, ips=["192.168.1.20"])
    assert urls == ["https://abc.trycloudflare.com", "http://192.168.1.20:8000"]
    # Nothing but loopback known: the same-computer address is still offered (better than nothing).
    assert netinfo.bridge_addresses("http://localhost:8000/", "localhost", 8000, ips=[])[0] == ["http://localhost:8000"]


def test_a_server_that_only_listens_on_its_own_computer_is_called_out_with_the_fix(monkeypatch):
    monkeypatch.setattr(netinfo, "accepts_from_network", lambda ip, port, now=None: False)
    _, warning = netinfo.bridge_addresses("http://127.0.0.1:8000/", "127.0.0.1", 8000, ips=["192.168.1.20"])
    assert warning and "dev_server" in warning and "0.0.0.0" in warning


def test_the_reachability_check_tells_a_local_only_listener_from_a_network_one():
    import socket

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))  # listening on loopback ONLY, like plain 'uvicorn app.main:app'
        srv.listen()
        port = srv.getsockname()[1]
        assert netinfo.accepts_from_network("127.0.0.1", port, now=0.0) is True
        lan = netinfo.lan_ips()
        if lan:
            assert netinfo.accepts_from_network(lan[0], port, now=0.0) is False


def test_a_paired_bridge_reconnects_after_the_server_restarts(monkeypatch, tiger):
    monkeypatch.setenv("SENSOR_BRIDGE_SECRET", "same-on-every-start")
    store = InMemoryStore()
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger)) as first:
        sid = new_session(first)
        with first.websocket_connect("/ws/sensors") as ws:
            ws.send_text(bridge_auth(pair_code=get_code(first, sid)))
            token = ws.receive_json()["bridge_token"]
    with TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger)) as restarted:  # memory is empty
        with restarted.websocket_connect("/ws/sensors") as ws:
            ws.send_text(bridge_auth(bridge_token=token))
            ready = ws.receive_json()
            assert ready["type"] == "sensor_ready" and ready["session_id"] == sid
        restarted.post(f"/api/workouts/{sid}/finish", headers=ALICE)
        with restarted.websocket_connect("/ws/sensors") as ws:  # finished: the token is dead, restart or not
            ws.send_text(bridge_auth(bridge_token=token))
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4409


# ---- the bridge socket ------------------------------------------------------------


def test_bridge_readings_are_stored_and_shown_live_in_the_app(client, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as app_ws:
        app_ws.send_text(json.dumps(auth_message()))
        assert app_ws.receive_json()["sensor_connected"] is False
        code = get_code(client, sid)
        with client.websocket_connect("/ws/sensors") as bridge:
            bridge.send_text(bridge_auth(pair_code=code))
            ready = bridge.receive_json()
            assert ready["type"] == "sensor_ready" and ready["session_id"] == sid and ready["bridge_token"]
            assert app_ws.receive_json() == {"schema_version": 1, "type": "sensor_link", "session_id": sid, "connected": True, "note": None}

            bridge.send_text(sensor_msg(sid, 0, 1000, spine_lumbar_deg=18.0, imu_pitch_deg=30.1))
            live = app_ws.receive_json()
            assert live["type"] == "sensor_live" and live["device_id"] == "esp32-spine"
            assert live["data"]["spine_lumbar_deg"] == 18.0 and live["data"]["imu_pitch_deg"] == 30.1

            bridge.send_text(json.dumps({"schema_version": 1, "type": "sensor_note", "session_id": sid, "note": "wearable calibrating (neutral)"}))
            assert app_ws.receive_json()["note"] == "wearable calibrating (neutral)"
            assert wait_for(lambda: any(r.metric_name == "spine_lumbar" for r in tiger.rows))
        gone = app_ws.receive_json()
        assert gone["type"] == "sensor_link" and gone["connected"] is False
    stored = [r for r in tiger.rows if r.source == "imu"]
    assert {str(r.session_id) for r in stored} == {sid}
    assert {r.metric_name for r in stored} == {"spine_lumbar", "imu_pitch"}


def test_live_readings_to_the_app_are_throttled_but_all_are_stored(client, tiger):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as app_ws:
        app_ws.send_text(json.dumps(auth_message()))
        app_ws.receive_json()
        with client.websocket_connect("/ws/sensors") as bridge:
            bridge.send_text(bridge_auth(pair_code=get_code(client, sid)))
            bridge.receive_json()
            app_ws.receive_json()  # sensor_link connected
            for i in range(20):  # sent back to back: far faster than 10/s
                bridge.send_text(sensor_msg(sid, i, 1000 + 50 * i, spine_mid_deg=float(i)))
            assert wait_for(lambda: len([r for r in tiger.rows if r.metric_name == "spine_mid"]) == 20)
        events = []
        while True:
            e = app_ws.receive_json()
            if e["type"] == "sensor_link":
                break
            events.append(e)
    assert 1 <= len(events) < 20 and all(e["type"] == "sensor_live" for e in events)


def test_the_bridge_works_before_the_app_connects_and_the_app_then_sees_it(client, tiger):
    sid = new_session(client)
    code = get_code(client, sid)
    with client.websocket_connect("/ws/sensors") as bridge:
        bridge.send_text(bridge_auth(pair_code=code))
        bridge.receive_json()
        bridge.send_text(sensor_msg(sid, 0, 1000))
        assert wait_for(lambda: len(tiger.rows) == 1)
        with client.websocket_connect(f"/ws/workouts/{sid}") as app_ws:
            app_ws.send_text(json.dumps(auth_message()))
            assert app_ws.receive_json()["sensor_connected"] is True


@pytest.mark.parametrize("auth", [{"pair_code": "NOPE-NOPE"}, {"bridge_token": "made-up"}, {}, {"pair_code": "A", "bridge_token": "B"}])
def test_bad_bridge_auth_is_rejected(client, auth):
    new_session(client)
    with client.websocket_connect("/ws/sensors") as bridge:
        bridge.send_text(bridge_auth(**auth))
        with pytest.raises(WebSocketDisconnect) as exc:
            bridge.receive_json()
    assert exc.value.code == 4401


def test_the_token_reconnects_and_the_newer_bridge_replaces_the_old(client):
    sid = new_session(client)
    with client.websocket_connect("/ws/sensors") as first:
        first.send_text(bridge_auth(pair_code=get_code(client, sid)))
        token = first.receive_json()["bridge_token"]
        with client.websocket_connect("/ws/sensors") as second:
            second.send_text(bridge_auth(bridge_token=token))
            assert second.receive_json()["session_id"] == sid
            with pytest.raises(WebSocketDisconnect) as exc:
                first.receive_json()
            assert exc.value.code == 4410


def test_finishing_the_workout_ends_the_bridge_and_its_token(client):
    sid = new_session(client)
    with client.websocket_connect("/ws/sensors") as bridge:
        bridge.send_text(bridge_auth(pair_code=get_code(client, sid)))
        token = bridge.receive_json()["bridge_token"]
        client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
        bridge.send_text(sensor_msg(sid, 0, 1000))
        with pytest.raises(WebSocketDisconnect) as exc:
            bridge.receive_json()
        assert exc.value.code == 4409
    with client.websocket_connect("/ws/sensors") as again:
        again.send_text(bridge_auth(bridge_token=token))
        with pytest.raises(WebSocketDisconnect) as exc:
            again.receive_json()
        assert exc.value.code == 4409  # "the workout ended" (not 4401 "unknown pairing"): the bridge says the right thing


def test_the_bridge_only_takes_sensor_messages_for_its_own_workout(client):
    sid, other = new_session(client), new_session(client)
    with client.websocket_connect("/ws/sensors") as bridge:
        bridge.send_text(bridge_auth(pair_code=get_code(client, sid)))
        bridge.receive_json()
        bridge.send_text(sensor_msg(other, 0, 1000))
        assert bridge.receive_json()["code"] == "session_mismatch"
        bridge.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pain"}))
        assert bridge.receive_json()["code"] == "invalid_message"  # a bridge can't speak for the user


def test_recalibrate_from_the_app_reaches_the_bridge(client):
    sid = new_session(client)
    cmd = json.dumps({"schema_version": 1, "type": "device_command", "session_id": sid, "command": "recalibrate"})
    with client.websocket_connect(f"/ws/workouts/{sid}") as app_ws:
        app_ws.send_text(json.dumps(auth_message()))
        app_ws.receive_json()
        app_ws.send_text(cmd)
        assert app_ws.receive_json()["code"] == "sensor_not_connected"
        with client.websocket_connect("/ws/sensors") as bridge:
            bridge.send_text(bridge_auth(pair_code=get_code(client, sid)))
            bridge.receive_json()
            app_ws.receive_json()  # sensor_link
            app_ws.send_text(cmd)
            assert bridge.receive_json() == {"schema_version": 1, "type": "device_command", "session_id": sid, "command": "recalibrate"}


def test_bridge_data_feeds_the_per_rep_back_analysis_like_app_relayed_data(client):
    sid = new_session(client)  # a squat: the back-shape rules apply
    with client.websocket_connect(f"/ws/workouts/{sid}") as app_ws:
        app_ws.send_text(json.dumps(auth_message()))
        app_ws.receive_json()
        with client.websocket_connect("/ws/sensors") as bridge:
            bridge.send_text(bridge_auth(pair_code=get_code(client, sid)))
            bridge.receive_json()
            for i in range(5):
                bridge.send_text(sensor_msg(sid, i, 1000 + 50 * i, spine_upper_deg=2.0, spine_mid_deg=1.0, spine_lumbar_deg=12.0))
            app_ws.receive_json()  # sensor_link
            assert app_ws.receive_json()["type"] == "sensor_live"  # the readings arrived
            live = client.app.state.live.get(UUID(sid))
            assert wait_for(lambda: live.spine is not None and len(live.spine._samples) == 5)
