"""Analytics functions against a mock query layer: parameter passing, result mapping, empty cases.
(The SQL itself is exercised against a real TimescaleDB in test_tigerdata_live.py.)"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.services import analytics, coach_context
from tests.fakes import FakeTiger, InMemoryStore

SID = uuid4()
T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_angle_stats_maps_and_rounds_and_filters_on_confidence():
    tiger = FakeTiger()
    tiger.result = [{"n": 600, "avg_deg": 118.456, "min_deg": 84.99, "max_deg": 171.0, "stddev_deg": 30.123}]
    out = analytics.angle_stats(tiger, SID)
    assert out == {"samples": 600, "avg_deg": 118.46, "min_deg": 84.99, "max_deg": 171.0, "stddev_deg": 30.12}
    sql, params = tiger.queries[0]
    assert "metadata->>'confidence'" in sql and "min_conf" in sql
    assert params == {"sid": SID, "metrics": ["left_knee_angle", "right_knee_angle"], "min_conf": 0.6}


def test_angle_stats_for_a_workout_with_no_data_is_all_none_not_a_crash():
    tiger = FakeTiger()
    tiger.result = [{"n": 0, "avg_deg": None, "min_deg": None, "max_deg": None, "stddev_deg": None}]
    out = analytics.angle_stats(tiger, SID)
    assert out["samples"] == 0 and out["avg_deg"] is None and out["min_deg"] is None


def test_movement_range_over_time_returns_buckets_and_trend():
    tiger = FakeTiger()
    tiger.result = [
        {"bucket": T0, "min_deg": 85.0, "max_deg": 170.0, "range_deg": 85.0, "trend_deg_per_min": -4.567},
        {"bucket": T0 + timedelta(seconds=5), "min_deg": 95.0, "max_deg": 170.0, "range_deg": 75.0, "trend_deg_per_min": -4.567},
    ]
    out = analytics.movement_range_over_time(tiger, SID, bucket_seconds=5)
    assert out["trend_deg_per_min"] == -4.57 and out["bucket_seconds"] == 5
    assert [b["range_deg"] for b in out["buckets"]] == [85.0, 75.0]
    assert out["buckets"][0]["time"] == T0.isoformat()
    assert "time_bucket" in tiger.queries[0][0] and tiger.queries[0][1]["bucket"] == 5.0


def test_movement_range_with_no_data():
    out = analytics.movement_range_over_time(FakeTiger(), SID)
    assert out["buckets"] == [] and out["trend_deg_per_min"] is None


def test_readings_window_is_bounded_and_scoped_to_the_session():
    tiger = FakeTiger()
    tiger.result = [{"time": T0, "source": "camera", "sensor_id": "", "metric_name": "left_knee_angle", "metric_value": 91.25, "samples": 1}]
    out = analytics.readings_in_window(tiger, SID, T0, T0 + timedelta(seconds=10), ["left_knee_angle"], limit=10**9)
    assert out == [{"time": T0.isoformat(), "source": "camera", "sensor_id": "", "metric": "left_knee_angle", "value": 91.25, "samples": 1}]
    sql, params = tiger.queries[0]
    assert params["sid"] == SID and params["t0"] == T0 and params["limit"] == analytics.MAX_WINDOW_ROWS  # capped
    assert "time_bucket" not in sql  # raw window


def test_readings_window_can_be_downsampled_in_sql():
    tiger = FakeTiger()
    analytics.readings_in_window(tiger, SID, T0, T0 + timedelta(minutes=1), bucket_seconds=2)
    sql, params = tiger.queries[0]
    assert "time_bucket" in sql and "avg(metric_value)" in sql and params["bucket"] == 2.0


def test_rep_trends_passes_windows_as_parallel_arrays_and_maps_trends():
    tiger = FakeTiger()
    tiger.result = [
        {"rep_number": 1, "samples": 50, "deepest_deg": 85.0, "top_deg": 170.0, "range_deg": 85.0, "avg_deg": 120.0, "depth_slope": 4.1234, "range_slope": -4.0},
        {"rep_number": 2, "samples": 48, "deepest_deg": 90.0, "top_deg": 170.0, "range_deg": 80.0, "avg_deg": 124.0, "depth_slope": 4.1234, "range_slope": -4.0},
    ]
    windows = [(1, T0, T0 + timedelta(seconds=2)), (2, T0 + timedelta(seconds=4), T0 + timedelta(seconds=6))]
    out = analytics.rep_trends(tiger, SID, windows)
    assert out["depth_trend_deg_per_rep"] == 4.123 and out["range_trend_deg_per_rep"] == -4.0
    assert [r["deepest_deg"] for r in out["reps"]] == [85.0, 90.0]
    params = tiger.queries[0][1]
    assert params["nums"] == [1, 2] and params["starts"] == [w[1] for w in windows] and params["ends"] == [w[2] for w in windows]
    assert "regr_slope" in tiger.queries[0][0]


def test_rep_trends_without_windows_skips_the_database():
    tiger = FakeTiger()
    tiger.fail = True  # would raise if queried
    assert analytics.rep_trends(tiger, SID, []) == {"reps": [], "depth_trend_deg_per_rep": None, "range_trend_deg_per_rep": None}


def test_sensor_overview_summarises_each_metric():
    tiger = FakeTiger()
    tiger.result = [
        {"source": "camera", "metric_name": "left_knee_angle", "n": 100, "avg_v": 120.0, "min_v": 85.0, "max_v": 170.0, "first_t": T0, "last_t": T0 + timedelta(seconds=9)},
        {"source": "flex_sensor", "metric_name": "flex_value", "n": 40, "avg_v": 1800.0, "min_v": 1200.0, "max_v": 2500.0, "first_t": T0 + timedelta(seconds=1), "last_t": T0 + timedelta(seconds=10)},
    ]
    out = analytics.sensor_overview(tiger, SID)
    assert out["readings"] == 140 and set(out["metrics"]) == {"left_knee_angle", "flex_value"}
    assert out["first_time"] == T0.isoformat() and out["last_time"] == (T0 + timedelta(seconds=10)).isoformat()
    assert out["metrics"]["flex_value"]["source"] == "flex_sensor"


def test_sensor_overview_with_no_data():
    assert analytics.sensor_overview(FakeTiger(), SID) == {"readings": 0, "first_time": None, "last_time": None, "metrics": {}}


# ---- coach context: both databases, bounded, degrades gracefully -------------


def store_with_reps():
    s = InMemoryStore()
    row = s.create_session("user-alice", "squat")
    sid = row["id"]
    for n in (1, 2, 3):
        s.save_repetition(
            sid, n,
            {"min_knee_angle_deg": 85.0 + n, "duration_ms": 1700, "max_asymmetry_deg": 2.0,
             "started_at": (T0 + timedelta(seconds=4 * n)).isoformat(), "ended_at": (T0 + timedelta(seconds=4 * n + 2)).isoformat()},
        )
    s.save_repetition(sid, 4, {"min_knee_angle_deg": 90.0})  # an old rep without a time window
    s.save_coaching_event(sid, uuid4(), 3, "reduced_depth", "msg", "send_feedback")
    return s, sid


def test_coach_context_merges_both_databases_and_uses_rep_windows():
    store, sid = store_with_reps()
    tiger = FakeTiger()
    tiger.result = lambda sql, params: (
        [{"n": 10, "avg_deg": 100.0, "min_deg": 85.0, "max_deg": 170.0, "stddev_deg": 30.0}] if "stddev_samp" in sql else []
    )
    ctx = coach_context.build_coach_context(store, tiger, sid)
    assert ctx["supabase"]["stats"]["rep_count"] == 4
    assert ctx["supabase"]["coaching_events"] == [{"rep_number": 3, "issue_code": "reduced_depth", "action": "send_feedback"}]
    assert ctx["timeseries"]["available"] is True and ctx["timeseries"]["knee_angle"]["avg_deg"] == 100.0
    trend_params = [p for s, p in tiger.queries if "unnest" in s][0]
    assert trend_params["nums"] == [1, 2, 3]  # rep 4 has no window and is skipped


def test_coach_context_still_works_when_tiger_data_is_down():
    store, sid = store_with_reps()
    tiger = FakeTiger()
    tiger.fail = True
    ctx = coach_context.build_coach_context(store, tiger, sid)
    assert ctx["timeseries"] == {"available": False, "reason": "unavailable"}
    assert ctx["supabase"]["stats"]["rep_count"] == 4  # Supabase half intact


def test_coach_context_when_tiger_data_is_not_configured():
    store, sid = store_with_reps()
    ctx = coach_context.build_coach_context(store, FakeTiger(configured=False), sid)
    assert ctx["timeseries"] == {"available": False, "reason": "not_configured"}


def test_coach_context_is_compact_no_matter_how_many_readings_exist():
    import json

    store, sid = store_with_reps()
    tiger = FakeTiger()
    tiger.result = lambda sql, params: [{"n": 10**7, "avg_deg": 1.0, "min_deg": 1.0, "max_deg": 1.0, "stddev_deg": 1.0}] if "stddev_samp" in sql else []
    assert len(json.dumps(coach_context.build_coach_context(store, tiger, sid))) < 3000
