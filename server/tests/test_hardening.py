"""Real-world robustness: reconnect takeover, oversized messages, floods, abandoned sessions."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import websocket as wsmod
from app.main import create_app
from app.services.ratelimit import TokenBucket
from app.services.session_manager import LiveSessions, sweep_once
from app.simulation import squat_messages
from tests.fakes import ALICE, FakeAuth, InMemoryStore, auth_message


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, auth=FakeAuth())) as c:
        yield c


def new_session(client):
    return client.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]


def ready(ws):
    ws.send_text(json.dumps(auth_message()))
    return ws.receive_json()


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def signal(ws, sid, name):
    ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": name}))


# ---- token bucket -----------------------------------------------------------


def test_token_bucket_allows_a_burst_then_the_sustained_rate():
    t = [0.0]
    b = TokenBucket(rate_per_s=10, burst=5, now=lambda: t[0])
    assert [b.allow() for _ in range(7)] == [True] * 5 + [False] * 2  # burst of 5, then empty
    t[0] += 0.3  # refills 3 tokens
    assert [b.allow() for _ in range(4)] == [True, True, True, False]
    t[0] += 100  # long idle never banks more than the burst
    assert sum(b.allow() for _ in range(20)) == 5


# ---- one socket per session: reconnect takeover -----------------------------


def test_a_new_connection_takes_the_session_over_from_a_stale_one(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as old:
        ready(old)
        for m in squat_messages(sid, reps=2):
            old.send_text(json.dumps(m))
        assert [old.receive_json()["rep_count"] for _ in range(2)] == [1, 2]

        with client.websocket_connect(f"/ws/workouts/{sid}") as new:  # the phone reconnected
            assert ready(new)["rep_count"] == 2  # counting continues; nothing lost
            with pytest.raises(WebSocketDisconnect) as exc:  # the stale socket is told to go away
                old.receive_json()
            assert exc.value.code == 4410
            for m in squat_messages(sid, reps=1, start_sequence=10_000, start_ts=1_794_100_000_000):
                new.send_text(json.dumps(m))
            assert new.receive_json()["rep_count"] == 3  # exactly one new rep, not two (no double counting)


def test_the_stale_socket_cannot_feed_the_session_after_takeover(client):
    sid = new_session(client)
    old_cm = client.websocket_connect(f"/ws/workouts/{sid}")
    old = old_cm.__enter__()
    ready(old)
    with client.websocket_connect(f"/ws/workouts/{sid}") as new:
        ready(new)
        try:
            for m in squat_messages(sid, reps=1):  # sent on the OLD socket: must be ignored
                old.send_text(json.dumps(m))
        except Exception:
            pass  # the server may already have closed it
        signal(new, sid, "pause")
        assert new.receive_json()["type"] == "session_state"  # first thing new sees: nothing leaked from old
    old_cm.__exit__(None, None, None)


def test_after_a_clean_disconnect_the_session_can_be_resumed(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ready(ws)
    assert wait_for(lambda: client.app.state.live.get(UUID(sid)).socket is None)  # released on disconnect
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        assert ready(ws)["type"] == "session_ready"


# ---- oversized messages and floods ------------------------------------------


def test_oversized_message_closes_the_socket(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ready(ws)
        ws.send_text("x" * (wsmod.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_oversized_auth_message_is_refused_too(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps({**auth_message(), "pad": "x" * (wsmod.MAX_MESSAGE_BYTES + 1)}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_a_flood_of_measurements_is_dropped_with_one_warning_and_the_socket_survives(client, monkeypatch):
    monkeypatch.setattr(wsmod, "BURST", 5)
    monkeypatch.setattr(wsmod, "RATE_PER_S", 0.0001)
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ready(ws)
        frames = list(squat_messages(sid, reps=1))
        for m in frames[:200]:
            ws.send_text(json.dumps(m))
        warning = ws.receive_json()
        assert warning["type"] == "error" and warning["code"] == "rate_limited"
        signal(ws, sid, "pause")  # the connection still works afterwards (signals have their own limit)
        nxt = ws.receive_json()
        assert nxt["type"] == "session_state"  # only ONE warning was sent, not ~195


def test_pain_report_is_never_starved_by_a_measurement_flood(client, store, monkeypatch):
    monkeypatch.setattr(wsmod, "BURST", 3)
    monkeypatch.setattr(wsmod, "RATE_PER_S", 0.0001)  # measurements are effectively blocked after 3
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ready(ws)
        for m in list(squat_messages(sid, reps=1))[:100]:
            ws.send_text(json.dumps(m))
        signal(ws, sid, "pain")
        seen = [ws.receive_json() for _ in range(3)]  # rate_limited warning, pain message, state
    types = {(e["type"], e.get("issue_code")) for e in seen}
    assert ("coaching_feedback", "pain_reported") in types
    assert wait_for(lambda: any(e["issue_code"] == "pain_reported" for e in store.events.get(sid, {}).values()))


def test_signal_spam_is_limited_too(client, monkeypatch):
    monkeypatch.setattr(wsmod, "SIGNAL_BURST", 2)
    monkeypatch.setattr(wsmod, "SIGNAL_RATE_PER_S", 0.0001)
    sid = new_session(client)
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ready(ws)
        for _ in range(10):
            signal(ws, sid, "pause")
        got = [ws.receive_json(), ws.receive_json(), ws.receive_json()]
    assert [e["type"] for e in got].count("session_state") == 2  # only the burst got through
    assert got[2]["code"] == "rate_limited"


# ---- abandoned live sessions -------------------------------------------------


class Conn:
    """Stand-in for a WebSocket on a LiveSession."""


def make_live(sessions, **kw):
    s = sessions.get_or_create(uuid4(), "user-alice")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def run(coro):
    return asyncio.run(coro)


def test_idle_sessions_without_a_socket_are_evicted_but_connected_ones_are_not():
    sessions = LiveSessions()
    gone = make_live(sessions, last_activity=0.0)
    connected = make_live(sessions, last_activity=0.0, socket=Conn())
    recent = make_live(sessions, last_activity=950.0)
    assert run(sweep_once(sessions, InMemoryStore(), ttl_s=100, now=1000.0)) == 1
    assert sessions.get(gone.session_id) is None
    assert sessions.get(connected.session_id) is connected and sessions.get(recent.session_id) is recent


def test_a_session_with_unsaved_writes_is_kept_until_they_save():
    store = InMemoryStore()
    sessions = LiveSessions()
    sid = uuid4()
    store.fail_saves = True
    s = make_live(sessions, last_activity=0.0)
    s.session_id = sid
    sessions._live = {sid: s}
    s.pending_writes = [("rep", (sid, 1, {"min_knee_angle_deg": 85.0}))]

    assert run(sweep_once(sessions, store, ttl_s=100, now=1000.0)) == 0  # DB still down: keep it, don't lose rep 1
    assert sessions.get(sid) is s and len(s.pending_writes) == 1

    store.fail_saves = False  # database is back
    assert run(sweep_once(sessions, store, ttl_s=100, now=1100.0)) == 1
    assert store.reps[str(sid)][1]["min_knee_angle_deg"] == 85.0  # flushed BEFORE eviction
    assert sessions.get(sid) is None


def test_eviction_gives_up_on_unsaved_writes_after_the_hard_limit():
    store = InMemoryStore()
    store.fail_saves = True
    sessions = LiveSessions()
    s = make_live(sessions, last_activity=0.0)
    s.pending_writes = [("rep", (s.session_id, 1, {}))]
    assert run(sweep_once(sessions, store, ttl_s=100, hard_ttl_s=500, now=1000.0)) == 1  # memory must not grow forever


def test_sessions_with_saves_in_flight_are_never_evicted():
    sessions = LiveSessions()

    async def go():
        s = make_live(sessions, last_activity=0.0)
        task = asyncio.create_task(asyncio.sleep(10))
        s.save_tasks.add(task)
        n = await sweep_once(sessions, InMemoryStore(), ttl_s=1, now=1000.0)
        task.cancel()
        return n

    assert run(go()) == 0


def test_the_running_app_sweeps_abandoned_sessions_on_its_own(store):
    with TestClient(create_app(store=store, auth=FakeAuth(), idle_ttl_s=0.05, sweep_interval_s=0.05)) as c:
        sid = new_session(c)
        with c.websocket_connect(f"/ws/workouts/{sid}") as ws:
            ready(ws)
            assert len(c.app.state.live) == 1  # while connected, it stays
            time.sleep(0.3)
            assert len(c.app.state.live) == 1
        assert wait_for(lambda: len(c.app.state.live) == 0)  # app went away -> evicted
        with c.websocket_connect(f"/ws/workouts/{sid}") as ws:  # and the workout can still be resumed
            assert ready(ws)["type"] == "session_ready"
