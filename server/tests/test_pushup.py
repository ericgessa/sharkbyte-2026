"""Push-up analysis: the elbow angle drives the same counter/coach/persistence pipeline as the squat.

All data here is simulated. The push-up thresholds are untuned guesses; these tests prove the plumbing and the
behaviour of the state machine, not that the thresholds suit real people.
"""

import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.agent import CoachAgent
from app.services.coaching import RuleCoach
from app.services.rep_counter import PoseSample, PushUpRepCounter, RepMetrics, SquatPhase, SquatRepCounter
from app.simulation import pushup_messages, squat_messages
from tests.fakes import ALICE, FakeAuth, FakeTiger, InMemoryStore, auth_message
from tests.test_agent import FakeProvider


def feed(counter, messages):
    reps = []
    for m in messages:
        d = m["data"]
        r = counter.update(PoseSample(
            m["sequence"], m["timestamp_ms"], d["landmark_confidence"],
            d.get("left_knee_angle_deg"), d.get("right_knee_angle_deg"),
            d.get("left_elbow_angle_deg"), d.get("right_elbow_angle_deg"),
        ))
        if r.rep:
            reps.append(r.rep)
    return reps


# ---------------------------------------------------------------- the counter


def test_five_simulated_pushups_are_counted_with_elbow_metrics():
    c = PushUpRepCounter()
    reps = feed(c, pushup_messages(uuid4(), reps=5))
    assert [r.rep_number for r in reps] == [1, 2, 3, 4, 5] and c.rep_count == 5
    assert all(r.joint == "elbow" for r in reps)
    assert all(75 <= r.min_angle_deg <= 85 for r in reps)
    assert all(r.max_asymmetry_deg == pytest.approx(2.0, abs=0.1) for r in reps)
    assert c.phase is SquatPhase.STANDING


def test_knee_data_never_counts_as_pushups_and_elbow_data_never_counts_as_squats():
    c = PushUpRepCounter()
    assert feed(c, squat_messages(uuid4(), reps=5)) == [] and c.rep_count == 0  # a squat is not a push-up
    s = SquatRepCounter()
    assert feed(s, pushup_messages(uuid4(), reps=5)) == [] and s.rep_count == 0


def test_a_half_pushup_is_not_counted():
    c = PushUpRepCounter()
    assert feed(c, pushup_messages(uuid4(), reps=4, depth_deg=125.0)) == []  # never got below ~100 degrees


def test_pushups_at_a_normal_pace_count_but_a_flurry_of_noise_does_not():
    assert len(feed(PushUpRepCounter(), pushup_messages(uuid4(), reps=3, rep_ms=1200))) == 3
    assert feed(PushUpRepCounter(), pushup_messages(uuid4(), reps=3, rep_ms=400)) == []  # under min_rep_ms: noise


def test_pushup_with_arms_never_fully_straight_still_counts_via_the_adaptive_top():
    c = PushUpRepCounter()
    reps = feed(c, pushup_messages(uuid4(), reps=4, standing_deg=148.0))  # tops out at 148, below the 155 default
    assert len(reps) == 4 and c.standing_threshold_deg < 155.0


def test_one_visible_elbow_is_enough_and_has_no_asymmetry():
    c = PushUpRepCounter()
    msgs = list(pushup_messages(uuid4(), reps=2))
    for m in msgs:
        m["data"]["right_elbow_angle_deg"] = None
    reps = feed(c, msgs)
    assert len(reps) == 2 and all(r.max_asymmetry_deg is None for r in reps)


# ---------------------------------------------------------------- the coach


def rep(n, depth=80.0, asym=2.0, joint="elbow"):
    return RepMetrics(n, depth, 400, 400, 1700, asym, joint=joint)


def test_coach_notices_pushups_getting_shallower_and_talks_about_elbows():
    coach = RuleCoach(joint="elbow")
    out = [coach.observe(r) for r in [rep(1), rep(2), rep(3), rep(4, 95), rep(5, 95)]]
    d = out[-1]
    assert d is not None and d.issue_code == "reduced_depth"
    asym = RuleCoach(joint="elbow")
    for r in [rep(1, asym=20), rep(2, asym=20)]:
        d = asym.observe(r)
    assert d is not None and d.issue_code == "asymmetry" and "elbows" in d.message and "knees" not in d.message


def test_the_knee_coach_wording_is_unchanged():
    d = None
    coach = RuleCoach()
    for r in [rep(1, 85, 20, "knee"), rep(2, 85, 20, "knee")]:
        d = coach.observe(r)
    assert d is not None and "knees" in d.message


def test_min_knee_angle_deg_still_works_on_metrics():
    assert rep(1, 83.0, joint="knee").min_knee_angle_deg == 83.0


# ---------------------------------------------------------------- the whole pipeline


def make_client(store, agent=None, tiger=None):
    return TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger or FakeTiger(), agent=agent))


def open_ws(client, sid):
    cm = client.websocket_connect(f"/ws/workouts/{sid}")
    ws = cm.__enter__()
    ws.send_text(json.dumps(auth_message()))
    return cm, ws, ws.receive_json()


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_pushup_workout_end_to_end_over_the_websocket():
    store = InMemoryStore()
    with make_client(store) as client:
        sid = client.post("/api/workouts", json={"exercise": "Push Up"}, headers=ALICE).json()["session_id"]
        cm, ws, ready = open_ws(client, sid)
        try:
            assert ready["exercise"] == "push-up" and ready["analysis_supported"] is True
            for m in pushup_messages(sid, reps=5, depths=[80, 80, 80, 95, 95]):
                ws.send_text(json.dumps(m))
            events = [ws.receive_json() for _ in range(6)]
        finally:
            cm.__exit__(None, None, None)
        reps = [e for e in events if e["type"] == "rep_completed"]
        assert [e["rep_count"] for e in reps] == [1, 2, 3, 4, 5]
        m1 = reps[0]["metrics"]
        assert m1["joint"] == "elbow" and m1["min_knee_angle_deg"] is None and 75 <= m1["min_angle_deg"] <= 85
        cue = next(e for e in events if e["type"] == "coaching_feedback")
        assert cue["issue_code"] == "reduced_depth" and cue["source"] == "rules"
        assert wait_for(lambda: len(store.reps.get(sid, {})) == 5)
        done = client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()
        assert done["total_reps"] == 5 and done["analysis_supported"] is True
        stats = client.get(f"/api/workouts/{sid}/summary", headers=ALICE).json()["stats"]
        assert stats["rep_count"] == 5 and stats["avg_min_knee_angle_deg"] is None  # knee fields do not apply
        assert stats["deepest_angle_deg"] == pytest.approx(80.0, abs=6) and stats["depth_change_deg"] is not None
        assert stats["shallowest_angle_deg"] > stats["deepest_angle_deg"]


def test_squat_over_the_websocket_still_reports_the_original_knee_field_and_the_new_ones():
    store = InMemoryStore()
    with make_client(store) as client:
        sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]
        cm, ws, _ = open_ws(client, sid)
        try:
            for m in squat_messages(sid, reps=1):
                ws.send_text(json.dumps(m))
            m = ws.receive_json()["metrics"]
        finally:
            cm.__exit__(None, None, None)
        assert m["joint"] == "knee" and m["min_knee_angle_deg"] == m["min_angle_deg"]
        assert wait_for(lambda: len(store.reps.get(sid, {})) == 1)
        stats = client.get(f"/api/workouts/{sid}/summary", headers=ALICE).json()["stats"]
        assert stats["avg_min_knee_angle_deg"] == stats["avg_min_angle_deg"]
        assert stats["deepest_knee_angle_deg"] == stats["deepest_angle_deg"]


def test_pushup_cues_use_the_rules_and_gemini_writes_the_end_of_workout_report():
    prov = FakeProvider()
    store = InMemoryStore()
    with make_client(store, CoachAgent(prov)) as client:
        sid = client.post("/api/workouts", json={"exercise": "push-up"}, headers=ALICE).json()["session_id"]
        cm, ws, _ = open_ws(client, sid)
        try:
            for m in pushup_messages(sid, reps=5, depths=[80, 80, 80, 95, 95]):
                ws.send_text(json.dumps(m))
            events = [ws.receive_json() for _ in range(6)]
        finally:
            cm.__exit__(None, None, None)
        cue = next(e for e in events if e["type"] == "coaching_feedback")
        assert cue["source"] == "rules"  # delivered straight from the rules, no model involved
        assert wait_for(lambda: len(store.reps.get(sid, {})) == 5)
        client.post(f"/api/workouts/{sid}/finish", headers=ALICE)
        recap = client.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    # Reporter-only: no LLM during the workout, Gemini writes the report afterwards, told what was counted.
    assert recap["source"] == "agent" and recap["summary"] == "You did great today."
    assert prov.decide_calls == []
    (ctx,) = prov.summarize_calls
    assert ctx["exercise"] == "Push-up" and ctx["counted_joint"] == "elbow"


def test_resuming_a_pushup_workout_continues_counting_from_the_saved_reps():
    store = InMemoryStore()
    with make_client(store) as client:
        sid = client.post("/api/workouts", json={"exercise": "push-up"}, headers=ALICE).json()["session_id"]
        cm, ws, _ = open_ws(client, sid)
        try:
            for m in pushup_messages(sid, reps=2):
                ws.send_text(json.dumps(m))
            assert [ws.receive_json()["rep_count"] for _ in range(2)] == [1, 2]
        finally:
            cm.__exit__(None, None, None)
        assert wait_for(lambda: len(store.reps.get(sid, {})) == 2)
        client.app.state.live.remove(UUID(sid))  # e.g. the server restarted
        cm, ws, ready = open_ws(client, sid)
        try:
            assert ready["rep_count"] == 2
            for m in pushup_messages(sid, reps=1, start_sequence=0, start_ts=1_794_100_000_000):
                ws.send_text(json.dumps(m))
            assert ws.receive_json()["rep_count"] == 3
        finally:
            cm.__exit__(None, None, None)
