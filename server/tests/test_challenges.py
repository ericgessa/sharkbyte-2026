"""Challenges (the app's "your best + 1 in 3:00") are saved as Rep Racer solo rounds with a 180 s limit. The server
scores them from the workout's own counted reps, so the phone can't claim a win."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryGameStore, InMemoryStore, auth_message


@pytest.fixture
def client():
    with TestClient(create_app(store=InMemoryStore(), auth=FakeAuth(), timeseries=FakeTiger(configured=False), games=InMemoryGameStore())) as c:
        yield c


def finished_squats(client, reps):
    sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]
    with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
        ws.send_text(json.dumps(auth_message()))
        ws.receive_json()
        for m in squat_messages(sid, reps=reps):
            ws.send_text(json.dumps(m))
        got = 0
        while got < reps:
            e = ws.receive_json()
            if e["type"] == "rep_completed":
                got = e["rep_count"]
    client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
    return sid


def challenge(sid, target, duration_ms=60_000):
    return {"game": "rep_racer", "mode": "solo", "exercise": "squat", "workout_session_id": sid, "duration_ms": duration_ms, "target_reps": target, "time_limit_s": 180}


def test_a_challenge_is_won_or_missed_by_the_servers_own_count(client):
    won = client.post("/api/games/results", json=challenge(finished_squats(client, 5), 5), headers=ALICE)
    assert won.status_code == 201, won.text
    assert won.json()["completed"] is True and won.json()["valid_reps"] == 5 and won.json()["time_limit_s"] == 180

    missed = client.post("/api/games/results", json=challenge(finished_squats(client, 3), 6), headers=ALICE)
    assert missed.status_code == 201 and missed.json()["completed"] is False and missed.json()["valid_reps"] == 3

    history = client.get("/api/games/results?game=rep_racer", headers=ALICE).json()
    assert sorted(r["completed"] for r in history) == [False, True]
    assert client.get("/api/games/results?game=rep_racer", headers=BOB).json() == []


def test_a_challenge_cannot_be_saved_twice_or_by_someone_else(client):
    sid = finished_squats(client, 4)
    first = client.post("/api/games/results", json=challenge(sid, 4), headers=ALICE)
    again = client.post("/api/games/results", json=challenge(sid, 4), headers=ALICE)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert client.post("/api/games/results", json=challenge(sid, 4), headers=BOB).status_code == 404
