"""Multiple exercises: registry, normalization, validation messages, logging vs analysis."""

import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import database as db
from app.services import exercises as ex
from app.services.ingest import SensorIngestor
from app.services.rep_counter import NoopRepCounter, PoseSample, PushUpRepCounter, SquatRepCounter
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryStore, auth_message
from tests.test_database import FakeClient, calls

REQUESTED = ["squat", "push-up", "pull-up", "deadlift", "bench-press", "lunge", "plank"]
ANALYZED = ["squat", "push-up"]  # have a rep counter; everything else is logged only
UNANALYZED = [s for s in REQUESTED if s not in ANALYZED]


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


def create(client, body, headers=ALICE):
    return client.post("/api/workouts", json=body, headers=headers)


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- registry ---------------------------------------------------------------


def test_all_requested_exercises_are_registered():
    assert ex.supported_names() == REQUESTED


@pytest.mark.parametrize(
    "spelling, slug",
    [
        ("push-up", "push-up"), ("pushup", "push-up"), ("Push Up", "push-up"), ("PUSH_UP", "push-up"),
        ("  push  up  ", "push-up"), ("push-ups", "push-up"), ("Pushups", "push-up"),
        ("pull-up", "pull-up"), ("Pull Up", "pull-up"), ("pullups", "pull-up"),
        ("Bench Press", "bench-press"), ("benchpress", "bench-press"), ("BENCH-PRESS", "bench-press"), ("bench", "bench-press"),
        ("Squat", "squat"), ("squats", "squat"), ("SQUAT", "squat"),
        ("Dead Lift", "deadlift"), ("deadlifts", "deadlift"), ("Lunges", "lunge"), ("planks", "plank"),
    ],
)
def test_spellings_resolve_to_one_canonical_exercise(spelling, slug):
    assert ex.resolve(spelling).slug == slug


@pytest.mark.parametrize("bad", ["", "   ", "burpee", "pushh up xyz", "squat; drop table", "x" * 500, "12345"])
def test_unknown_names_raise_a_helpful_error(bad):
    with pytest.raises(ex.UnknownExerciseError) as e:
        ex.resolve(bad)
    msg = str(e.value)
    assert "Supported:" in msg or "must not be empty" in msg
    assert len(msg) < 400  # never echoes a huge input back


@pytest.mark.parametrize("bad", [None, 5, ["squat"], {"a": 1}, True])
def test_non_text_is_rejected(bad):
    with pytest.raises(ex.UnknownExerciseError):
        ex.resolve(bad)
    assert ex.find(bad) is None


def test_close_misspellings_get_a_suggestion():
    with pytest.raises(ex.UnknownExerciseError, match="Did you mean 'push-up'"):
        ex.resolve("pushap")
    with pytest.raises(ex.UnknownExerciseError, match="Did you mean 'deadlift'"):
        ex.resolve("dedlift")


def test_only_squat_and_pushup_have_analysis_and_the_flag_matches_reality():
    assert [e.slug for e in ex.all_exercises() if e.analysis_supported] == ANALYZED
    assert isinstance(ex.find("squat").make_counter(3), SquatRepCounter) and ex.find("squat").make_counter(3).rep_count == 3
    assert isinstance(ex.find("push-up").make_counter(2), PushUpRepCounter) and ex.find("push-up").make_counter(2).rep_count == 2
    for slug in UNANALYZED:
        assert isinstance(ex.find(slug).make_counter(5), NoopRepCounter)  # never counts, even if reps were saved


def test_noop_counter_never_counts_or_raises():
    c = NoopRepCounter()
    for i in range(100):
        r = c.update(PoseSample(i, i * 33, 0.9, 90.0 if i % 2 else 170.0, None))
        assert r.rep is None and r.dropped is None and r.tracking_changed is None
    c.reset_tracking()
    assert c.rep_count == 0


def test_a_name_from_an_old_database_row_gets_a_logging_only_placeholder():
    old = ex.for_stored_name("kettlebell-swing")
    assert old.slug == "kettlebell-swing" and old.analysis_supported is False
    assert ex.for_stored_name("squat").analysis_supported is True


# ---- create-workout endpoint ------------------------------------------------


@pytest.mark.parametrize("slug", REQUESTED)
def test_every_registered_exercise_can_start_a_workout(client, slug):
    r = create(client, {"exercise": slug})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["exercise"] == slug and body["status"] == "active"
    assert body["analysis_supported"] is (slug in ANALYZED)


def test_spelling_variants_are_stored_and_returned_as_the_canonical_name(client, store):
    for spelling in ("pushup", "Push Up", "PUSH-UP", "push_up"):
        r = create(client, {"exercise": spelling})
        assert r.status_code == 201 and r.json()["exercise"] == "push-up"
        assert store.sessions[r.json()["session_id"]]["exercises"]["name"] == "push-up"  # what Supabase sees


def test_omitting_the_exercise_still_means_squat_for_existing_clients(client):
    r = create(client, {})
    assert r.status_code == 201 and r.json()["exercise"] == "squat" and r.json()["analysis_supported"] is True


def test_unsupported_exercise_gets_a_clear_422(client):
    r = create(client, {"exercise": "burpee"})
    assert r.status_code == 422
    (err,) = r.json()["detail"]
    assert err["loc"] == ["body", "exercise"]
    assert "Unsupported exercise 'burpee'" in err["msg"]
    for name in REQUESTED:
        assert name in err["msg"]  # tells the caller exactly what is allowed


@pytest.mark.parametrize("body", [{"exercise": 5}, {"exercise": None}, {"exercise": ""}, {"exercise": ["squat"]}, {"exercise": "x" * 5000}])
def test_junk_exercise_values_are_422_not_500(client, body):
    assert create(client, body).status_code == 422


def test_extra_fields_are_still_rejected(client):
    assert create(client, {"exercise": "squat", "reps": 10}).status_code == 422


def test_history_and_get_report_each_workouts_exercise(client):
    a = create(client, {"exercise": "squat"}).json()["session_id"]
    b = create(client, {"exercise": "Pull Up"}).json()["session_id"]
    got = {w["session_id"]: (w["exercise"], w["analysis_supported"]) for w in client.get("/api/workouts", headers=ALICE).json()}
    assert got == {a: ("squat", True), b: ("pull-up", False)}
    assert client.get(f"/api/workouts/{b}", headers=ALICE).json()["analysis_supported"] is False


def test_exercise_listing_for_the_frontend_needs_no_login(client):
    r = client.get("/api/exercises")
    assert r.status_code == 200
    listing = r.json()
    assert [e["slug"] for e in listing] == REQUESTED
    squat = listing[0]
    assert squat == {"slug": "squat", "display_name": "Squat", "aliases": ["squats", "air squat", "bodyweight squat"],
                     "analysis_supported": True, "primary_angles": ["left_knee_angle", "right_knee_angle"]}
    assert {e["slug"]: e["analysis_supported"] for e in listing} == {s: s in ANALYZED for s in REQUESTED}


# ---- logging vs analysis: what the live socket does per exercise ------------


def open_ws(client, sid):
    cm = client.websocket_connect(f"/ws/workouts/{sid}")
    ws = cm.__enter__()
    ws.send_text(json.dumps(auth_message()))
    return cm, ws, ws.receive_json()


def test_squat_session_ready_says_analysis_is_on_and_reps_still_count(client):
    sid = create(client, {"exercise": "squat"}).json()["session_id"]
    cm, ws, ready = open_ws(client, sid)
    try:
        assert ready["exercise"] == "squat" and ready["analysis_supported"] is True
        for m in squat_messages(sid, reps=2):
            ws.send_text(json.dumps(m))
        assert [ws.receive_json()["rep_count"] for _ in range(2)] == [1, 2]
    finally:
        cm.__exit__(None, None, None)
    assert client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()["total_reps"] == 2


@pytest.mark.parametrize("slug", UNANALYZED)
def test_other_exercises_are_logged_but_never_counted_or_coached(client, store, tiger, slug):
    sid = create(client, {"exercise": slug}).json()["session_id"]
    cm, ws, ready = open_ws(client, sid)
    try:
        assert ready["exercise"] == slug and ready["analysis_supported"] is False and ready["rep_count"] == 0
        # Squat-shaped movement that WOULD be 5 squat reps (with a coaching message) if mis-routed to the squat counter.
        for m in squat_messages(sid, reps=5, depths=[85, 85, 85, 98, 98]):
            ws.send_text(json.dumps(m))
        # No rep/coaching events must arrive. Prove the socket is alive and silent by asking for state.
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
        first = ws.receive_json()
        assert first["type"] == "session_state"  # the first thing back: nothing was queued before it
        assert wait_for(lambda: len(tiger.rows) > 100)  # ...but the raw data WAS recorded (logging works)
    finally:
        cm.__exit__(None, None, None)
    done = client.post(f"/api/workouts/{sid}/finish", headers=ALICE).json()
    assert done["total_reps"] == 0 and done["exercise"] == slug and done["analysis_supported"] is False
    assert store.reps.get(sid, {}) == {} and store.events.get(sid, {}) == {}  # no reps, no coaching events saved
    assert {str(r.session_id) for r in tiger.rows} == {sid}


def test_pain_reporting_works_for_every_exercise_even_without_analysis(client, store):
    sid = create(client, {"exercise": "deadlift"}).json()["session_id"]
    cm, ws, _ = open_ws(client, sid)
    try:
        ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pain"}))
        fb, state = ws.receive_json(), ws.receive_json()
    finally:
        cm.__exit__(None, None, None)
    assert fb["issue_code"] == "pain_reported" and "stop" in fb["message"].lower() and state["coaching_enabled"] is False
    assert wait_for(lambda: any(e["issue_code"] == "pain_reported" for e in store.events.get(sid, {}).values()))


def test_elbow_and_hip_angles_are_accepted_and_recorded(client, tiger):
    sid = create(client, {"exercise": "push-up"}).json()["session_id"]
    cm, ws, _ = open_ws(client, sid)
    try:
        ws.send_text(json.dumps({
            "schema_version": 1, "type": "pose_update", "session_id": sid, "sequence": 1, "timestamp_ms": 1000,
            "data": {"left_elbow_angle_deg": 95.0, "right_elbow_angle_deg": 97.0, "left_hip_angle_deg": 175.0, "landmark_confidence": 0.9},
        }))
        assert wait_for(lambda: len(tiger.rows) == 3)
    finally:
        cm.__exit__(None, None, None)
    assert {r.metric_name: r.metric_value for r in tiger.rows} == {"left_elbow_angle": 95.0, "right_elbow_angle": 97.0, "left_hip_angle": 175.0}


def test_pose_update_with_no_angles_is_still_rejected(client):
    sid = create(client, {"exercise": "push-up"}).json()["session_id"]
    cm, ws, _ = open_ws(client, sid)
    try:
        ws.send_text(json.dumps({"schema_version": 1, "type": "pose_update", "session_id": sid, "sequence": 1,
                                 "timestamp_ms": 1, "data": {"landmark_confidence": 0.9}}))
        assert ws.receive_json()["code"] == "invalid_message"
    finally:
        cm.__exit__(None, None, None)


def test_a_squat_workout_by_one_user_is_unaffected_by_another_users_pushup(client):
    s = create(client, {"exercise": "squat"}).json()["session_id"]
    create(client, {"exercise": "push-up"}, BOB)
    assert [w["exercise"] for w in client.get("/api/workouts", headers=ALICE).json()] == ["squat"]
    assert client.get(f"/api/workouts/{s}", headers=BOB).status_code == 404


# ---- database: the exercise row is created on first use ----------------------


def test_ensure_exercise_creates_a_missing_row_once_and_never_touches_an_existing_one():
    cfg = ex.find("push-up").db_config()
    missing = FakeClient({"exercises": []})  # select finds nothing, upsert runs, re-select still empty in the fake
    with pytest.raises(db.DatabaseError):  # the fake never "creates" the row; the point is WHAT was sent
        db.ensure_exercise("push-up", cfg, missing)
    (args, kwargs), = calls(missing, "upsert")
    assert args[0] == {"name": "push-up", "config": cfg} and kwargs == {"on_conflict": "name", "ignore_duplicates": True}

    existing = FakeClient({"exercises": [{"id": 7}]})
    assert db.ensure_exercise("squat", ex.find("squat").db_config(), existing) == 7
    assert calls(existing, "upsert") == []  # the seeded squat row is left exactly as it is


def test_create_workout_session_with_config_ensures_the_exercise_first():
    client = FakeClient({"exercises": [{"id": 9}], "workout_sessions": [{"id": "new"}]})
    row = db.create_workout_session(uuid4(), "lunge", client, exercise_config=ex.find("lunge").db_config())
    assert row == {"id": "new"}
    (args, _), = calls(client, "insert")
    assert args[0]["exercise_id"] == 9


def test_stored_exercise_config_describes_the_exercise_honestly():
    cfg = ex.find("plank").db_config()
    assert cfg["analysis_supported"] is False and cfg["display_name"] == "Plank" and "left_hip_angle" in cfg["primary_angles"]
    sq = ex.find("squat").db_config()
    assert sq["analysis_supported"] is True and sq["bottom_deg"] == 110 and sq["min_rep_ms"] == 800
