"""GRIT Games over HTTP and WebSocket: saved results, personal bests, rooms, a full 1v1 race, and who can see what.

Everything is faked (in-memory stores, fake tokens) and pose data is SIMULATED. Nothing here talks to Supabase or
a camera; ``scripts/e2e_games.py`` is the live check against a real project once the migration is applied.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.services import game_rooms
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, InMemoryGameStore, InMemoryStore, auth_message

CAROL = {"Authorization": "Bearer carol-token"}
T0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def stores():
    return InMemoryStore(), InMemoryGameStore()


@pytest.fixture
def client(stores):
    store, games = stores
    auth = FakeAuth()
    auth.tokens["carol-token"] = "user-carol"
    with TestClient(create_app(store=store, auth=auth, games=games)) as c:
        yield c


def workout(store: InMemoryStore, user: str, rep_seconds: list[float], exercise: str = "squat", finished: bool = True) -> str:
    """A saved workout whose reps were counted at the given seconds after its start (what the server's counter would
    have written). Deterministic, so the timing rules can be tested exactly."""
    row = store.create_session(user, exercise)
    sid = row["id"]
    store.sessions[sid]["started_at"] = T0.isoformat()
    for n, sec in enumerate(rep_seconds, start=1):
        store.save_repetition(sid, n, {"min_angle_deg": 85.0, "ended_at": (T0 + timedelta(seconds=sec)).isoformat()})
    if finished:
        store.finish_session(store.sessions[sid], len(rep_seconds))
        store.sessions[sid]["ended_at"] = (T0 + timedelta(seconds=max(rep_seconds + [1]) + 2)).isoformat()
    return sid


def racer(sid: str, **over) -> dict:
    body = {"game": "rep_racer", "mode": "solo", "exercise": "squat", "workout_session_id": sid, "duration_ms": 30_000, "target_reps": 5, "time_limit_s": 60}
    return {**body, **over}


# ------------------------------------------------------------------ saving results


def test_a_won_race_is_scored_from_the_server_counted_reps_not_from_the_client(client, stores):
    store, games = stores
    sid = workout(store, "user-alice", [3, 6, 9, 12, 15, 18])
    r = client.post("/api/games/results", headers=ALICE, json=racer(sid, duration_ms=1_000))  # the client lies about the time
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["valid_reps"] == 5 and body["completed"] is True  # capped at the target: the race ends there
    assert body["duration_ms"] == 15_000  # the moment the server counted rep 5, not the client's claim
    assert body["rep_times_ms"] == [3000, 6000, 9000, 12000, 15000]
    assert body["personal_best"] == {"has_previous": False, "previous_duration_ms": None, "previous_reps": None, "is_new_best": True, "delta_ms": None, "reps_delta": None}
    assert games.results[0]["input_source"] == "camera" and games.results[0]["workout_session_id"] == sid


def test_the_client_cannot_claim_reps_that_were_not_counted(client, stores):
    store, _ = stores
    sid = workout(store, "user-alice", [4, 9])
    body = client.post("/api/games/results", headers=ALICE, json=racer(sid, target_reps=5, duration_ms=45_000)).json()
    assert body["valid_reps"] == 2 and body["completed"] is False
    assert body["duration_ms"] == 12_500  # not the claimed 45 s: capped by how long the workout really lasted (11 s + slack)


def test_reps_after_the_time_limit_do_not_count(client, stores):
    store, _ = stores
    sid = workout(store, "user-alice", [5, 10, 15, 20, 90])  # the fifth rep came long after the 30 s limit
    body = client.post("/api/games/results", headers=ALICE, json=racer(sid, time_limit_s=30, duration_ms=30_000)).json()
    assert body["valid_reps"] == 4 and body["completed"] is False
    assert body["duration_ms"] <= 30_000 + 1500


def test_an_unfinished_workout_cannot_be_saved_as_a_result(client, stores):
    store, _ = stores
    sid = workout(store, "user-alice", [3, 6], finished=False)
    r = client.post("/api/games/results", headers=ALICE, json=racer(sid))
    assert r.status_code == 409 and "finish the workout" in r.json()["detail"]


def test_the_result_must_match_the_workouts_exercise(client, stores):
    store, _ = stores
    sid = workout(store, "user-alice", [3, 6], exercise="push-up")
    assert client.post("/api/games/results", headers=ALICE, json=racer(sid, exercise="squat")).status_code == 409
    assert client.post("/api/games/results", headers=ALICE, json=racer(sid, exercise="pushups")).status_code == 201


def test_saving_the_same_workout_twice_returns_the_first_result(client, stores):
    store, games = stores
    sid = workout(store, "user-alice", [3, 6, 9, 12, 15])
    first = client.post("/api/games/results", headers=ALICE, json=racer(sid))
    second = client.post("/api/games/results", headers=ALICE, json=racer(sid, duration_ms=999))
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"] and len(games.results) == 1


def test_nobody_can_attach_a_result_to_someone_elses_workout(client, stores):
    store, games = stores
    alices = workout(store, "user-alice", [3, 6, 9, 12, 15])
    assert client.post("/api/games/results", headers=BOB, json=racer(alices)).status_code == 404  # same as "does not exist"
    assert games.results == []
    assert client.post("/api/games/results", json=racer(alices)).status_code == 401
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.post("/api/games/results", headers=ALICE, json=racer(missing)).status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"game": "rep_racer", "mode": "multiplayer"},  # a client can never claim a multiplayer result
        {"game": "rep_racer", "mode": "bird"},
        {"game": "rep_racer", "score": 5},
        {"game": "rep_racer", "target_reps": None},
        {"game": "rep_racer", "time_limit_s": None},
        {"game": "rep_racer", "target_reps": 0},
        {"game": "rep_racer", "target_reps": 101},
        {"game": "rep_racer", "time_limit_s": 5},
        {"game": "rep_racer", "time_limit_s": 601},
        {"game": "rep_racer", "exercise": "pull-up"},  # no counter for it
        {"game": "rep_racer", "exercise": "nonsense"},
        {"game": "tetris"},
        {"game": "grit_bird", "mode": "bird"},
        {"game": "rep_racer", "duration_ms": -1},
        {"game": "rep_racer", "form_feedback": {"x" * 100: 1}},
        {"game": "rep_racer", "form_feedback": {"ok": -3}},
        {"game": "rep_racer", "extra_field": 1},
    ],
)
def test_bad_result_bodies_are_rejected_with_422(client, stores, change):
    store, _ = stores
    sid = workout(store, "user-alice", [3, 6, 9, 12, 15])
    r = client.post("/api/games/results", headers=ALICE, json=racer(sid, **change))
    assert r.status_code == 422, r.text


# ------------------------------------------------------------------ personal best


def test_a_faster_second_race_is_a_new_best_and_a_slower_one_is_not(client, stores):
    store, _ = stores
    first = client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", [4, 8, 12, 16, 20]))).json()
    assert first["duration_ms"] == 20_000
    faster = client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", [3, 6, 9, 12, 15]), mode="personal_best")).json()
    assert faster["personal_best"]["is_new_best"] is True and faster["personal_best"]["delta_ms"] == -5000
    assert faster["personal_best"]["previous_duration_ms"] == 20_000
    slower = client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", [5, 10, 15, 20, 25]))).json()
    assert slower["personal_best"]["is_new_best"] is False and slower["personal_best"]["delta_ms"] == 10_000  # compared with the 15 s best


def test_best_is_empty_on_a_first_attempt_and_never_invented(client):
    r = client.get("/api/games/best", headers=ALICE, params={"game": "rep_racer", "exercise": "squat", "target_reps": 5})
    assert r.status_code == 200 and r.json() == {"best": None, "attempts": 0}


def test_the_ghost_is_the_fastest_finished_race_with_the_same_target(client, stores):
    store, _ = stores
    for times in ([4, 8, 12, 16, 20], [3, 6, 9, 12, 15], [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]):  # the last one is a 10-rep race
        body = racer(workout(store, "user-alice", times), target_reps=10 if len(times) == 10 else 5)
        assert client.post("/api/games/results", headers=ALICE, json=body).status_code == 201
    best = client.get("/api/games/best", headers=ALICE, params={"game": "rep_racer", "exercise": "squat", "target_reps": 5}).json()
    assert best["attempts"] == 3 and best["best"]["duration_ms"] == 15_000 and best["best"]["rep_times_ms"] == [3000, 6000, 9000, 12000, 15000]


def test_without_a_finished_race_the_ghost_is_the_attempt_with_the_most_reps(client, stores):
    store, _ = stores
    for times in ([5, 15], [4, 9, 14, 19]):
        client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", times), target_reps=10, time_limit_s=60))
    best = client.get("/api/games/best", headers=ALICE, params={"game": "rep_racer", "exercise": "squat", "target_reps": 10}).json()["best"]
    assert best["valid_reps"] == 4 and best["completed"] is False


def test_personal_bests_are_per_player_and_per_exercise(client, stores):
    store, _ = stores
    client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", [3, 6, 9, 12, 15])))
    assert client.get("/api/games/best", headers=BOB, params={"game": "rep_racer", "exercise": "squat", "target_reps": 5}).json()["best"] is None
    assert client.get("/api/games/best", headers=ALICE, params={"game": "rep_racer", "exercise": "push-up", "target_reps": 5}).json()["best"] is None
    assert client.get("/api/games/best", headers=ALICE, params={"game": "rep_racer", "exercise": "plank"}).status_code == 422


# ------------------------------------------------------------------ history and isolation


def test_history_lists_only_your_own_results_newest_first(client, stores):
    store, _ = stores
    ids = []
    for times in ([3, 6, 9, 12, 15], [2, 4, 6, 8, 10]):
        ids.append(client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", times))).json()["id"])
    bobs = client.post("/api/games/results", headers=BOB, json=racer(workout(store, "user-bob", [3, 6, 9, 12, 15]))).json()["id"]
    mine = client.get("/api/games/results", headers=ALICE).json()
    assert [r["id"] for r in mine] == ids[::-1]
    assert bobs not in [r["id"] for r in mine]
    assert [r["id"] for r in client.get("/api/games/results", headers=BOB).json()] == [bobs]
    assert client.get("/api/games/results", headers=ALICE, params={"game": "x"}).status_code == 422
    assert client.get("/api/games/results", headers=ALICE, params={"game": "grit_bird"}).status_code == 422


def test_one_players_result_is_invisible_to_another(client, stores):
    store, _ = stores
    rid = client.post("/api/games/results", headers=ALICE, json=racer(workout(store, "user-alice", [3, 6, 9, 12, 15]))).json()["id"]
    assert client.get(f"/api/games/results/{rid}", headers=ALICE).status_code == 200
    assert client.get(f"/api/games/results/{rid}", headers=BOB).status_code == 404
    assert client.get(f"/api/games/results/{rid}").status_code == 401


def test_every_games_endpoint_needs_a_login(client):
    for method, path in (
        ("get", "/api/games/results"),
        ("get", "/api/games/best?game=rep_racer&exercise=squat"),
        ("post", "/api/games/rooms"),
        ("post", "/api/games/rooms/ABCDEF/join"),
        ("get", "/api/games/rooms/ABCDEF"),
        ("post", "/api/games/rooms/ABCDEF/leave"),
    ):
        assert getattr(client, method)(path).status_code == 401, path


def test_a_missing_migration_gives_a_clear_503_not_a_crash(client, stores):
    store, games = stores
    sid = workout(store, "user-alice", [3, 6, 9, 12, 15])
    games.not_set_up = True
    r = client.post("/api/games/results", headers=ALICE, json=racer(sid))
    assert r.status_code == 503 and "20261108000001_grit_games.sql" in r.json()["detail"]
    assert client.get("/api/games/results", headers=ALICE).status_code == 503


# ------------------------------------------------------------------ rooms over REST


def make_room(client, headers=ALICE, **body) -> dict:
    r = client.post("/api/games/rooms", headers=headers, json={"exercise": "squat", "target_reps": 2, "time_limit_s": 30, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_join_and_look_at_a_room(client):
    room = make_room(client)
    assert len(room["code"]) == 6 and room["status"] == "lobby" and room["players"] == 1 and room["host_id"] == "user-alice"
    joined = client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={"display_name": "Bob"})
    assert joined.status_code == 200 and joined.json()["players"] == 2
    state = client.get(f"/api/games/rooms/{room['code'].lower()}", headers=ALICE).json()["state"]
    assert [p["name"] for p in state["players"]] == ["Player ALIC", "Bob"] or len(state["players"]) == 2


def test_a_full_room_a_stranger_and_bad_codes(client):
    room = make_room(client)
    client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={})
    full = client.post(f"/api/games/rooms/{room['code']}/join", headers=CAROL, json={})
    assert full.status_code == 409 and full.json()["detail"]["code"] == "room_full"
    assert client.get(f"/api/games/rooms/{room['code']}", headers=CAROL).status_code == 404  # not a member
    assert client.post("/api/games/rooms/NOPE99/join", headers=CAROL, json={}).status_code == 404
    assert client.post("/api/games/rooms/!!!/join", headers=CAROL, json={}).status_code == 404


@pytest.mark.parametrize("body", [{"exercise": "pull-up"}, {"target_reps": 0}, {"target_reps": 500}, {"time_limit_s": 3}, {"x": 1}])
def test_room_settings_are_validated(client, body):
    assert client.post("/api/games/rooms", headers=ALICE, json=body).status_code == 422


def test_leaving_a_room_removes_you_and_the_last_one_out_closes_it(client):
    room = make_room(client)
    client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={})
    assert client.post(f"/api/games/rooms/{room['code']}/leave", headers=ALICE).status_code == 204
    assert client.get(f"/api/games/rooms/{room['code']}", headers=ALICE).status_code == 404
    assert client.get(f"/api/games/rooms/{room['code']}", headers=BOB).json()["host_id"] == "user-bob"
    client.post(f"/api/games/rooms/{room['code']}/leave", headers=BOB)
    assert client.get(f"/api/games/rooms/{room['code']}", headers=BOB).status_code == 404


# ------------------------------------------------------------------ rooms over WebSocket


@pytest.fixture
def fast_countdown(monkeypatch):
    monkeypatch.setattr(game_rooms, "COUNTDOWN_S", 0.25)


def recv_until(ws, kind: str, limit: int = 600) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"never received {kind}")


def room_frames(reps: int, start_sequence: int = 0, **kw):
    for m in squat_messages("unused", reps=reps, start_sequence=start_sequence, **kw):
        m.pop("session_id")
        yield m


def test_the_socket_refuses_bad_tokens_and_people_who_are_not_in_the_room(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as ws:
        ws.send_json(auth_message("not-a-token"))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as ws:
        ws.send_json(auth_message("bob-token"))  # a valid user who never joined
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4404
    with client.websocket_connect("/ws/games/rooms/ZZZZZZ") as ws:
        ws.send_json(auth_message("alice-token"))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4404


def test_a_full_race_between_two_players_over_websockets(client, stores, fast_countdown):
    store, games = stores
    room = make_room(client, target_reps=2, time_limit_s=30)
    code = room["code"]
    client.post(f"/api/games/rooms/{code}/join", headers=BOB, json={"display_name": "Bob"})
    with client.websocket_connect(f"/ws/games/rooms/{code}") as alice, client.websocket_connect(f"/ws/games/rooms/{code}") as bob:
        alice.send_json(auth_message("alice-token"))
        bob.send_json(auth_message("bob-token"))
        first = recv_until(alice, "room_state")
        assert first["you"] == "user-alice" and first["status"] == "lobby" and first["target_reps"] == 2
        recv_until(bob, "room_state")

        alice.send_json({"schema_version": 1, "type": "ready", "ready": True})
        bob.send_json({"schema_version": 1, "type": "ready", "ready": True})
        for ws in (alice, bob):  # both laptops get the same countdown, then the same start
            cd = recv_until(ws, "room_state")
            while cd["status"] != "countdown":
                cd = recv_until(ws, "room_state")
            assert 0 < cd["starts_in_ms"] <= 250
            assert recv_until(ws, "race_started")["target_reps"] == 2

        for m in room_frames(2):
            alice.send_json(m)
        for ws in (alice, bob):
            done = recv_until(ws, "race_finished")
            assert done["winner_id"] == "user-alice" and done["end_reason"] == "target_reached"
            assert {r["user_id"]: r["result"] for r in done["results"]} == {"user-alice": "win", "user-bob": "loss"}

        # bob saw alice's reps arrive live
        # (rep_counted events were queued before race_finished; the scores in the final state agree)
        final = recv_until(bob, "room_state")
        assert {p["user_id"]: p["reps"] for p in final["players"]}["user-alice"] == 2

    deadline = time.time() + 5
    while time.time() < deadline and not games.races:
        time.sleep(0.05)
    assert len(games.races) == 1, "the finished race was not saved"
    race = next(iter(games.races.values()))
    assert race["winner_id"] == "user-alice" and race["end_reason"] == "target_reached" and race["code"] == code
    assert {p["user_id"]: p["result"] for p in race["players"]} == {"user-alice": "win", "user-bob": "loss"}
    by_user = {r["user_id"]: r for r in games.results}
    assert by_user["user-alice"]["mode"] == "multiplayer" and by_user["user-alice"]["completed"] is True and by_user["user-alice"]["valid_reps"] == 2
    assert by_user["user-bob"]["completed"] is False and by_user["user-bob"]["room_id"] == by_user["user-alice"]["room_id"]
    # each player also got a normal workout, so history and the AI recap work for races too
    assert len(store.list_sessions("user-alice", 10)) == 1 and store.list_sessions("user-alice", 10)[0]["status"] == "completed"
    assert store.count_repetitions(by_user["user-alice"]["workout_session_id"]) == 2
    # and the saved race is readable by its players only
    rid = by_user["user-alice"]["room_id"]
    assert client.get(f"/api/games/races/{rid}", headers=ALICE).status_code == 200
    assert client.get(f"/api/games/races/{rid}", headers=CAROL).status_code == 404


def test_a_race_still_finishes_for_the_players_if_saving_it_fails(client, stores, fast_countdown):
    _, games = stores
    games.fail_race_save = True
    room = make_room(client, target_reps=1)
    client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={})
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as alice, client.websocket_connect(f"/ws/games/rooms/{room['code']}") as bob:
        alice.send_json(auth_message("alice-token"))
        bob.send_json(auth_message("bob-token"))
        recv_until(alice, "room_state")
        recv_until(bob, "room_state")
        alice.send_json({"schema_version": 1, "type": "ready", "ready": True})
        bob.send_json({"schema_version": 1, "type": "ready", "ready": True})
        recv_until(alice, "race_started")
        for m in room_frames(1):
            alice.send_json(m)
        assert recv_until(alice, "race_finished")["winner_id"] == "user-alice"
    assert games.races == {}


def test_rematch_over_websockets(client, fast_countdown):
    room = make_room(client, target_reps=1)
    client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={})
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as alice, client.websocket_connect(f"/ws/games/rooms/{room['code']}") as bob:
        alice.send_json(auth_message("alice-token"))
        bob.send_json(auth_message("bob-token"))
        recv_until(alice, "room_state")
        recv_until(bob, "room_state")
        for ws in (alice, bob):
            ws.send_json({"schema_version": 1, "type": "ready", "ready": True})
        recv_until(alice, "race_started")
        for m in room_frames(1):
            alice.send_json(m)
        recv_until(alice, "race_finished")
        alice.send_json({"schema_version": 1, "type": "rematch"})
        bob.send_json({"schema_version": 1, "type": "rematch"})
        state = recv_until(alice, "room_state")
        while not (state["status"] == "lobby" and state["race_id"] == 1):
            state = recv_until(alice, "room_state")
        assert all(p["reps"] == 0 and not p["ready"] for p in state["players"])


def test_reconnecting_takes_the_seat_over_and_closes_the_old_connection(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as first:
        first.send_json(auth_message("alice-token"))
        recv_until(first, "room_state")
        with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as second:
            second.send_json(auth_message("alice-token"))
            state = recv_until(second, "room_state")
            assert state["you"] == "user-alice" and state["players"][0]["connected"] is True
            with pytest.raises(WebSocketDisconnect) as e:
                for _ in range(50):
                    first.receive_json()
            assert e.value.code == 4410
            # the new connection still works
            second.send_json({"schema_version": 1, "type": "ready", "ready": True})
            assert recv_until(second, "room_state")["players"][0]["ready"] is True


def test_dropping_out_marks_you_disconnected_and_the_seat_survives(client):
    room = make_room(client)
    client.post(f"/api/games/rooms/{room['code']}/join", headers=BOB, json={})
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as alice:
        alice.send_json(auth_message("alice-token"))
        recv_until(alice, "room_state")
        with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as bob:
            bob.send_json(auth_message("bob-token"))
            recv_until(bob, "room_state")
        state = recv_until(alice, "room_state")
        while {p["user_id"]: p["connected"] for p in state["players"]}["user-bob"]:
            state = recv_until(alice, "room_state")
        assert state["status"] == "lobby"
    state = client.get(f"/api/games/rooms/{room['code']}", headers=BOB).json()["state"]
    assert {p["user_id"] for p in state["players"]} == {"user-alice", "user-bob"}  # bob can come back


def test_bad_and_flooding_messages_are_answered_without_hurting_the_room(client, monkeypatch):
    from app.api import games as games_api

    room = make_room(client)
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as ws:
        ws.send_json(auth_message("alice-token"))
        recv_until(ws, "room_state")
        ws.send_text("not json")
        assert recv_until(ws, "error")["code"] == "invalid_message"
        ws.send_json({"schema_version": 1, "type": "ready", "ready": "yes please", "extra": 1})
        assert recv_until(ws, "error")["code"] == "invalid_message"
        ws.send_json({"schema_version": 1, "type": "pose_update", "sequence": 0, "timestamp_ms": 1, "data": {"landmark_confidence": 0.9}})  # no angle
        assert recv_until(ws, "error")["code"] == "invalid_message"
        monkeypatch.setattr(games_api, "MAX_MESSAGE_BYTES", 200)
        ws.send_text("x" * 500)
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(10):
                ws.receive_json()
        assert e.value.code == 1009


def test_a_flood_of_control_messages_is_rate_limited(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as ws:
        ws.send_json(auth_message("alice-token"))
        recv_until(ws, "room_state")
        for _ in range(80):
            ws.send_json({"schema_version": 1, "type": "ready", "ready": True})
        assert recv_until(ws, "error", limit=400)["code"] == "rate_limited"


def test_leaving_by_message_closes_your_connection(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/games/rooms/{room['code']}") as ws:
        ws.send_json(auth_message("alice-token"))
        recv_until(ws, "room_state")
        ws.send_json({"schema_version": 1, "type": "leave"})
        with pytest.raises(WebSocketDisconnect):
            for _ in range(10):
                ws.receive_json()
    assert client.get(f"/api/games/rooms/{room['code']}", headers=ALICE).status_code == 404
