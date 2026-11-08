"""Progress stats (GET /api/stats): the pure rules with fixed dates, then the endpoint (isolation, validation, fallbacks)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import user_stats
from tests.fakes import ALICE, BOB, FakeAuth, InMemoryGameStore, InMemoryStore

TODAY = date(2026, 11, 8)  # a Sunday
NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


def session(day: date, reps: int = 10, exercise: str = "squat", status: str = "completed", minutes: int = 20, hour: int = 12, sid=None) -> dict:
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
    return {
        "id": sid or str(uuid4()),
        "user_id": "user-alice",
        "status": status,
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(minutes=minutes)).isoformat() if status == "completed" else None,
        "total_reps": reps,
        "exercises": {"name": exercise},
    }


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


# ------------------------------------------------------------------ streaks


def test_a_run_of_consecutive_days_is_the_current_streak():
    days = {days_ago(0), days_ago(1), days_ago(2)}
    assert user_stats.streaks(days, TODAY) == (3, 3)


def test_the_streak_survives_until_you_have_trained_today():
    days = {days_ago(1), days_ago(2)}
    assert user_stats.streaks(days, TODAY) == (2, 2)  # nothing yet today: still alive through yesterday


def test_missing_a_whole_day_ends_the_current_streak_but_keeps_the_longest():
    days = {days_ago(2), days_ago(3), days_ago(4), days_ago(10)}
    assert user_stats.streaks(days, TODAY) == (0, 3)


def test_longest_streak_can_be_in_the_past():
    days = {days_ago(0)} | {days_ago(n) for n in range(5, 9)}
    assert user_stats.streaks(days, TODAY) == (1, 4)


def test_no_days_no_streak():
    assert user_stats.streaks(set(), TODAY) == (0, 0)


def test_one_workout_a_day_or_ten_is_the_same_day():
    sessions = [session(days_ago(0), sid="a"), session(days_ago(0), sid="b", hour=18)]
    assert len(user_stats.active_days(sessions)) == 1


def test_only_workouts_that_counted_reps_make_a_day_active():
    sessions = [session(days_ago(0), reps=0), session(days_ago(1), reps=5, status="active"), session(days_ago(2), reps=3)]
    assert user_stats.active_days(sessions) == {days_ago(2)}


def test_the_time_zone_decides_which_day_a_workout_belongs_to():
    late = session(date(2026, 11, 7), hour=23)  # 23:00 UTC on the 7th
    assert user_stats.active_days([late], 0) == {date(2026, 11, 7)}
    assert user_stats.active_days([late], 120) == {date(2026, 11, 8)}  # UTC+2: already the 8th
    assert user_stats.active_days([late], -300) == {date(2026, 11, 7)}  # US Eastern: still the 7th (6 pm)
    early = session(date(2026, 11, 8), hour=1)
    assert user_stats.active_days([early], -300) == {date(2026, 11, 7)}  # 8:00 pm the evening before


# ------------------------------------------------------------------ totals, exercises, weeks


def test_totals_count_only_completed_workouts():
    sessions = [session(days_ago(0), reps=10), session(days_ago(1), reps=5), session(days_ago(2), reps=99, status="active")]
    p = user_stats.compute_progress(sessions, TODAY)
    assert p["totals"]["workouts"] == 2 and p["totals"]["reps"] == 15
    assert p["totals"]["active_seconds"] == 2 * 20 * 60
    assert p["totals"]["last_workout_at"].date() == days_ago(0) and p["totals"]["first_workout_at"].date() == days_ago(1)


def test_an_empty_history_is_all_zeros_not_an_error():
    p = user_stats.compute_progress([], TODAY, weeks=4)
    assert p["totals"] == {"workouts": 0, "reps": 0, "active_seconds": 0, "first_workout_at": None, "last_workout_at": None}
    assert p["per_exercise"] == [] and p["streak"] == {"current_days": 0, "longest_days": 0}
    assert p["records"]["most_reps_in_a_workout"] is None
    assert [w["reps"] for w in p["weekly"]] == [0, 0, 0, 0]


def test_per_exercise_totals_best_workout_and_spelling_variants():
    sessions = [
        session(days_ago(0), reps=12, exercise="squat", sid="s1"),
        session(days_ago(3), reps=20, exercise="squat", sid="s2"),
        session(days_ago(1), reps=8, exercise="push-up", sid="p1"),
        session(days_ago(2), reps=7, exercise="Pushups", sid="p2"),  # an older spelling still groups with push-up
    ]
    per = {r["exercise"]: r for r in user_stats.compute_progress(sessions, TODAY)["per_exercise"]}
    assert per["squat"]["total_reps"] == 32 and per["squat"]["workouts"] == 2
    assert per["squat"]["best_workout_reps"] == 20 and per["squat"]["best_workout_id"] == "s2"
    assert per["push-up"]["total_reps"] == 15 and per["push-up"]["workouts"] == 2
    ordered = [r["exercise"] for r in user_stats.compute_progress(sessions, TODAY)["per_exercise"]]
    assert ordered == ["squat", "push-up"]  # most reps first


def test_the_overall_record_is_the_biggest_single_workout():
    sessions = [session(days_ago(0), reps=12, exercise="squat"), session(days_ago(1), reps=30, exercise="push-up", sid="big")]
    best = user_stats.compute_progress(sessions, TODAY)["records"]["most_reps_in_a_workout"]
    assert (best["exercise"], best["reps"], best["workout_id"]) == ("push-up", 30, "big")


def test_weekly_activity_runs_monday_to_sunday_and_fills_empty_weeks():
    sessions = [
        session(date(2026, 11, 8), reps=10),  # Sunday: the same week as Monday the 2nd
        session(date(2026, 11, 2), reps=5),
        session(date(2026, 11, 1), reps=7),  # Sunday of the week before
        session(date(2026, 10, 12), reps=4),  # three weeks before that
    ]
    weeks = user_stats.compute_progress(sessions, TODAY, weeks=4)["weekly"]
    by_start = {w["week_start"]: (w["workouts"], w["reps"]) for w in weeks}
    assert by_start[date(2026, 11, 2)] == (2, 15)
    assert by_start[date(2026, 10, 26)] == (1, 7)
    assert by_start[date(2026, 10, 19)] == (0, 0)
    assert [w["week_start"] for w in weeks] == sorted(w["week_start"] for w in weeks)  # oldest first, ending with this week
    assert weeks[-1]["week_start"] == date(2026, 11, 2)
    assert len(weeks) == 4 and by_start[date(2026, 10, 12)] == (1, 4)
    narrow = user_stats.compute_progress(sessions, TODAY, weeks=3)["weekly"]
    assert len(narrow) == 3 and date(2026, 10, 12) not in {w["week_start"] for w in narrow}  # outside a 3-week window


def test_a_workout_left_open_does_not_count_as_hours_of_training():
    s = session(days_ago(0), minutes=20)
    s["ended_at"] = (datetime.fromisoformat(s["started_at"]) + timedelta(days=3)).isoformat()
    assert user_stats.compute_progress([s], TODAY)["totals"]["active_seconds"] == user_stats.MAX_ACTIVE_SECONDS


def test_odd_rows_do_not_crash_the_stats():
    broken = [{"id": "x", "status": "completed", "started_at": "not a date", "ended_at": None, "total_reps": None, "exercises": None}]
    p = user_stats.compute_progress(broken, TODAY)
    assert p["totals"]["workouts"] == 1 and p["totals"]["reps"] == 0 and p["streak"]["current_days"] == 0


# ------------------------------------------------------------------ race bests


def result(exercise="squat", target=5, duration=15_000, completed=True, mode="solo", created="2026-11-05T10:00:00+00:00") -> dict:
    return {"game": "rep_racer", "exercise": exercise, "target_reps": target, "duration_ms": duration, "completed": completed, "mode": mode, "created_at": created}


def test_race_bests_pick_the_fastest_finished_race_per_exercise_and_target():
    rows = [
        result(duration=20_000),
        result(duration=15_000, mode="personal_best"),
        result(duration=9_000, completed=False),  # did not finish: never a "best time"
        result(target=10, duration=40_000),
        result(exercise="push-up", duration=25_000),
    ]
    bests = user_stats.race_bests(rows)
    assert bests["races"] == 5 and bests["finished"] == 4
    fastest = {(b["exercise"], b["target_reps"]): b["duration_ms"] for b in bests["fastest"]}
    assert fastest == {("push-up", 5): 25_000, ("squat", 5): 15_000, ("squat", 10): 40_000}
    assert next(b for b in bests["fastest"] if (b["exercise"], b["target_reps"]) == ("squat", 5))["mode"] == "personal_best"


def test_no_races_yet():
    assert user_stats.race_bests([]) == {"races": 0, "finished": 0, "fastest": []}


# ------------------------------------------------------------------ the endpoint


@pytest.fixture
def stores():
    return InMemoryStore(), InMemoryGameStore()


@pytest.fixture
def client(stores):
    store, games = stores
    app = create_app(store=store, auth=FakeAuth(), games=games)
    app.state.clock = lambda: NOW
    with TestClient(app) as c:
        yield c


def put(store: InMemoryStore, user: str, **kw) -> dict:
    row = session(**kw)
    row["user_id"] = user
    store.sessions[row["id"]] = row
    return row


def test_stats_need_a_login(client):
    assert client.get("/api/stats").status_code == 401


def test_a_new_user_gets_zeros(client):
    body = client.get("/api/stats", headers=ALICE).json()
    assert body["totals"]["workouts"] == 0 and body["streak"] == {"current_days": 0, "longest_days": 0}
    assert len(body["weekly"]) == 8 and body["workouts_considered"] == 0
    assert body["games"] == {"races": 0, "finished": 0, "fastest": []}


def test_stats_add_up_the_users_workouts(client, stores):
    store, _ = stores
    put(store, "user-alice", day=days_ago(0), reps=10)
    put(store, "user-alice", day=days_ago(1), reps=15, exercise="push-up")
    put(store, "user-alice", day=days_ago(2), reps=5, status="active")
    body = client.get("/api/stats", headers=ALICE).json()
    assert body["totals"]["workouts"] == 2 and body["totals"]["reps"] == 25
    assert body["streak"] == {"current_days": 2, "longest_days": 2}
    assert {e["exercise"]: e["total_reps"] for e in body["per_exercise"]} == {"squat": 10, "push-up": 15}
    assert body["records"]["most_reps_in_a_workout"]["reps"] == 15
    assert body["workouts_considered"] == 3


def test_one_user_never_sees_another_users_stats(client, stores):
    store, games = stores
    put(store, "user-alice", day=days_ago(0), reps=40)
    games.results.append({**result(), "user_id": "user-alice", "id": "r1", "workout_session_id": None})
    bob = client.get("/api/stats", headers=BOB).json()
    assert bob["totals"]["reps"] == 0 and bob["totals"]["workouts"] == 0
    assert bob["games"]["races"] == 0 and bob["per_exercise"] == []
    alice = client.get("/api/stats", headers=ALICE).json()
    assert alice["totals"]["reps"] == 40 and alice["games"]["races"] == 1


def test_the_time_zone_parameter_moves_the_day(client, stores):
    store, _ = stores
    put(store, "user-alice", day=date(2026, 11, 7), reps=10, hour=23)  # 23:00 UTC on the 7th; "now" is 15:00 UTC on the 8th
    utc = client.get("/api/stats", headers=ALICE).json()
    assert utc["streak"]["current_days"] == 1  # trained yesterday, not yet today
    plus2 = client.get("/api/stats", headers=ALICE, params={"tz_offset_minutes": 120}).json()  # the workout is on the 8th: today
    assert plus2["streak"]["current_days"] == 1 and plus2["weekly"][-1]["reps"] == 10


def test_the_weeks_parameter_sets_the_window(client):
    assert len(client.get("/api/stats", headers=ALICE, params={"weeks": 3}).json()["weekly"]) == 3
    assert len(client.get("/api/stats", headers=ALICE, params={"weeks": 52}).json()["weekly"]) == 52


@pytest.mark.parametrize("params", [{"weeks": 0}, {"weeks": 53}, {"tz_offset_minutes": 900}, {"tz_offset_minutes": -900}, {"weeks": "x"}])
def test_bad_parameters_are_rejected(client, params):
    assert client.get("/api/stats", headers=ALICE, params=params).status_code == 422


def test_race_stats_are_null_when_the_games_tables_are_missing_but_workout_stats_still_work(client, stores):
    store, games = stores
    put(store, "user-alice", day=days_ago(0), reps=12)
    games.not_set_up = True
    r = client.get("/api/stats", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert body["games"] is None and body["totals"]["reps"] == 12


def test_race_bests_show_up_in_the_endpoint(client, stores):
    _, games = stores
    for duration in (20_000, 14_000):
        games.results.append({**result(duration=duration), "user_id": "user-alice", "id": str(uuid4()), "workout_session_id": None})
    body = client.get("/api/stats", headers=ALICE).json()
    assert body["games"]["finished"] == 2
    assert body["games"]["fastest"][0]["duration_ms"] == 14_000
