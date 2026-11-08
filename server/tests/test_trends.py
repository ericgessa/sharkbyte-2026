"""Trends: the deterministic facts and advice, the AI wording under guardrails, the endpoint, and device-counted reps."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.agent import CoachAgent
from app.services.trends import SetStats, build_trends, rules_headline, set_stats
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryGameStore, InMemoryStore, auth_message

T0 = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)


def s(i, reps, days_ago=0, **kw):
    return SetStats(session_id=f"s{i}", at=T0 - timedelta(days=days_ago, minutes=i), reps=reps, duration_s=kw.pop("dur", 60), **kw)


def by_key(t):
    return {f["key"]: f for f in t["facts"]}


# ---- the deterministic part -----------------------------------------------------------


def test_no_sets_yet_invites_the_first_one():
    t = build_trends("Squats", [], 0, 0)
    assert t["advice_key"] == "first_set" and t["facts"][0]["big"] == "0"


def test_more_reps_than_last_time_a_new_best_a_streak_and_the_next_target():
    t = build_trends("Push-ups", [s(0, 14, 0), s(1, 11, 1), s(2, 12, 2)], all_time_reps=37, all_time_sets=3)
    f = by_key(t)
    assert f["delta"]["big"] == "+3" and f["best"]["small"] == "new personal best!"
    assert f["total"]["big"] == "37" and f["streak"]["big"] == "3 days"
    assert t["advice_key"] == "push" and "15" in t["advice"]
    assert t["context"]["next_target"] == 15 and rules_headline(t) == "+3 more reps than your last set"


def test_advice_priorities_back_issues_then_depth_then_balance_then_recovery():
    back = build_trends("Push-ups", [s(0, 10, back_issues={"hips_sagging": 4}, back_peak_deg=22.0), s(1, 10, 1)], 20, 2)
    assert back["advice_key"] == "back_hips_sagging" and by_key(back)["back"]["big"] == "22°"
    depth = build_trends("Squats", [s(0, 10, depth_deg=100.0, joint="knee"), s(1, 10, 1, depth_deg=90.0, joint="knee")], 20, 2)
    assert depth["advice_key"] == "depth" and by_key(depth)["depth"]["small"] == "shallower than your other sets"
    bal = build_trends("Squats", [s(0, 10, balance_deg=9.0), s(1, 10, 1)], 20, 2)
    assert bal["advice_key"] == "balance"
    tired = build_trends("Squats", [s(0, 5), s(1, 10, 1), s(2, 10, 2)], 25, 3)
    assert tired["advice_key"] == "recover" and "10" in tired["advice"]


def test_focus_is_the_set_just_finished_not_always_the_newest():
    sets = [s(0, 20, 0), s(1, 8, 1), s(2, 6, 2)]
    t = build_trends("Squats", sets, 34, 3, focus_id="s1")
    assert by_key(t)["delta"]["big"] == "+2"  # 8 vs the 6 before it


def test_set_stats_reads_rep_metrics_and_the_wearable_record():
    row = {"id": "x", "started_at": "2026-11-01T18:00:00+00:00", "ended_at": "2026-11-01T18:01:30+00:00", "total_reps": 3}
    reps = [
        {"metrics": {"min_angle_deg": 90, "duration_ms": 2000, "max_asymmetry_deg": 4, "joint": "knee", "spine": {"lumbar_max_deg": 20, "issues": ["lower_back_rounding"]}}},
        {"metrics": {"min_angle_deg": 94, "duration_ms": 2200, "max_asymmetry_deg": None, "joint": "knee", "spine": {"lumbar_max_deg": 24, "issues": []}}},
        {"metrics": {"counted_by": "device"}},
    ]
    st = set_stats(row, reps)
    assert (st.reps, st.duration_s, st.depth_deg, st.tempo_s, st.balance_deg, st.back_peak_deg) == (3, 90.0, 92.0, 2.1, 4.0, 22.0)
    assert st.back_issues == {"lower_back_rounding": 1} and st.joint == "knee"


# ---- the AI wording ----------------------------------------------------------------------


class FakeProvider:
    def __init__(self, reply):
        self.reply = reply

    async def trends(self, context):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def aclose(self):
        return None


def run(agent, t):
    return asyncio.run(agent.trends(t["context"], rules_headline(t), t["advice"]))


def test_ai_words_the_trend_but_only_with_numbers_from_the_data_and_no_medical_words():
    t = build_trends("Push-ups", [s(0, 14, 0), s(1, 11, 1)], 25, 2)
    ok = run(CoachAgent(FakeProvider({"headline": "Three more than last time!", "advice": "Go for 15 next time, one past your best of 14."})), t)
    assert ok[2] == "agent" and ok[0] == "Three more than last time!"
    invented = run(CoachAgent(FakeProvider({"headline": "50 push-ups!", "advice": "Great."})), t)
    assert invented == (rules_headline(t), t["advice"], "rules")
    medical = run(CoachAgent(FakeProvider({"headline": "Nice set", "advice": "Protect your spine from injury."})), t)
    assert medical[2] == "rules"
    broken = run(CoachAgent(FakeProvider(RuntimeError("boom"))), t)
    assert broken[2] == "rules"
    assert run(CoachAgent(None), t)[2] == "rules"


# ---- the endpoints --------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(create_app(store=InMemoryStore(), auth=FakeAuth(), timeseries=FakeTiger(configured=False), games=InMemoryGameStore())) as c:
        yield c


def squat_set(client, reps):
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


def test_trends_endpoint_builds_bubbles_from_saved_sets(client):
    assert client.get("/api/trends/squat").status_code == 401
    empty = client.get("/api/trends/squat", headers=ALICE).json()
    assert empty["sets"] == 0 and empty["source"] == "rules"
    squat_set(client, 3)
    last = squat_set(client, 5)
    t = client.get(f"/api/trends/squats?session_id={last}", headers=ALICE).json()  # any spelling of the exercise
    f = {x["key"]: x for x in t["facts"]}
    assert t["exercise"] == "squat" and t["sets"] == 2
    assert f["delta"]["big"] == "+2" and f["total"]["big"] == "8" and f["best"]["small"] == "new personal best!"
    assert "depth" in f and "tempo" in f  # from the saved rep metrics
    assert "6" in t["advice"]  # next target = best + 1
    # Bob sees none of Alice's sets
    assert client.get("/api/trends/squat", headers=BOB).json()["sets"] == 0
    assert client.get("/api/trends/burpee", headers=ALICE).status_code == 404


def test_jumping_jacks_counted_on_the_phone_are_saved_and_trend(client):
    sid = client.post("/api/workouts", json={"exercise": "jumping jacks"}, headers=ALICE).json()["session_id"]
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": 12}, headers=BOB).status_code == 404
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": 12}, headers=ALICE).status_code == 204
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": 12}, headers=ALICE).status_code == 204  # retry: no double count
    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()["total_reps"] == 12
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": 3}, headers=ALICE).status_code == 409  # finished
    t = client.get("/api/trends/jumping-jack", headers=ALICE).json()
    assert t["sets"] == 1 and {x["key"]: x for x in t["facts"]}["first"]["big"] == "12"
    stats = client.get("/api/stats", headers=ALICE).json()
    assert {e["exercise"]: e["total_reps"] for e in stats["per_exercise"]}["jumping-jack"] == 12


def test_device_reps_never_overwrite_camera_counted_exercises(client):
    sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": 50}, headers=ALICE).status_code == 409
    assert client.post(f"/api/workouts/{sid}/device-reps", json={"reps": -1}, headers=ALICE).status_code == 422


# ---- rep waves and fatigue ------------------------------------------------------------------


def rep_rows(durations, depths):
    return [{"rep_number": i + 1, "metrics": {"duration_ms": t, "min_angle_deg": d, "joint": "elbow"}} for i, (t, d) in enumerate(zip(durations, depths))]


def test_fatigue_compares_the_last_third_of_the_reps_with_the_first():
    row = {"id": "x", "started_at": "2026-11-01T18:00:00+00:00", "ended_at": "2026-11-01T18:01:00+00:00", "total_reps": 9}
    reps = rep_rows([1000, 1000, 1000, 1200, 1200, 1200, 1300, 1300, 1300], [80, 80, 80, 85, 85, 85, 90, 90, 90])
    st = set_stats(row, list(reversed(reps)))  # rows can arrive in any order; rep_number decides
    assert st.slowdown_pct == 30 and st.depth_drop_deg == 10.0
    assert [b["t"] for b in st.bars] == [1000, 1000, 1000, 1200, 1200, 1200, 1300, 1300, 1300]
    t = build_trends("Push-ups", [st], 9, 1)
    f = by_key(t)
    assert f["fatigue"]["big"] == "+30%" and t["advice_key"] == "fade"
    assert t["context"]["this_set_slowdown_percent"] == 30
    assert t["waves"][0]["bars"][0] == {"d": 80, "t": 1000}


def test_too_few_reps_means_no_fatigue_claim():
    row = {"id": "x", "started_at": None, "ended_at": None, "total_reps": 3}
    st = set_stats(row, rep_rows([1000, 2000, 3000], [80, 90, 100]))
    assert st.slowdown_pct is None and st.depth_drop_deg is None
    assert "fatigue" not in by_key(build_trends("Push-ups", [st], 3, 1))


def test_the_endpoint_returns_recent_waves_oldest_first(client):
    squat_set(client, 2)
    squat_set(client, 3)
    t = client.get("/api/trends/squat", headers=ALICE).json()
    assert [w["reps"] for w in t["waves"]] == [2, 3]
    assert len(t["waves"][1]["bars"]) == 3 and t["waves"][1]["bars"][0]["d"] is not None
