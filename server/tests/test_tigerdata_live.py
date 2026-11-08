"""LIVE tests against a real Tiger Data / TimescaleDB service. Skipped unless:

    RUN_TIGERDATA_TESTS=1  and  TIGERDATA_URL set in server/.env  and  migrations applied

    RUN_TIGERDATA_TESTS=1 python -m pytest tests/test_tigerdata_live.py -v        (PowerShell: $env:RUN_TIGERDATA_TESTS=1)

Each test writes readings under a random throwaway session UUID and deletes them afterwards.
These verify the parts mocks cannot: the hypertable schema, the dedupe index, and that every
analytics SQL query returns the right NUMBERS for data with known answers.
"""

import math
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from app.schemas.sensors import SensorReading
from app.services import analytics, coach_context
from app.services.timeseries import TigerStore
from tests.conftest import REAL_TIGERDATA_URL
from tests.fakes import InMemoryStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TIGERDATA_TESTS") != "1" or not REAL_TIGERDATA_URL,
    reason="set RUN_TIGERDATA_TESTS=1 and TIGERDATA_URL (server/.env) to run against a real Tiger Data service",
)

# Aligned to a 4 s boundary so each 2 s rep falls inside exactly one 4 s time_bucket.
_now = int(datetime.now(timezone.utc).timestamp())
BASE = datetime.fromtimestamp(_now - (_now % 4) - 120, tz=timezone.utc)
DEPTHS = [85.0, 90.0, 95.0]
TOP = 170.0


@pytest.fixture
def tiger():
    store = TigerStore(url=REAL_TIGERDATA_URL)
    info = store.ping()
    if not info["sensor_readings_is_hypertable"]:
        pytest.skip("sensor_readings is not a hypertable yet: run python -m scripts.apply_tigerdata_migrations")
    yield store
    store.close()


@pytest.fixture
def sid(tiger):
    session_id = uuid4()
    yield session_id
    with psycopg.connect(REAL_TIGERDATA_URL, connect_timeout=10) as conn:  # cleanup: remove this test's rows
        conn.execute("delete from sensor_readings where session_id = %s", (session_id,))


def squat_angles(depth):
    """21 samples at 10 Hz over 2 s: stand -> depth (exactly at sample 10) -> stand."""
    return [round(TOP - (TOP - depth) * 0.5 * (1 - math.cos(2 * math.pi * i / 20)), 4) for i in range(21)]


def build_workout(session_id):
    """3 reps starting at BASE+0s, +4s, +8s, both knees, confidence 0.9, plus one low-confidence glitch."""
    readings, windows = [], []
    for n, depth in enumerate(DEPTHS, start=1):
        start = BASE + timedelta(seconds=4 * (n - 1))
        windows.append((n, start, start + timedelta(seconds=2)))
        for i, angle in enumerate(squat_angles(depth)):
            for metric in ("left_knee_angle", "right_knee_angle"):
                readings.append(
                    SensorReading(time=start + timedelta(milliseconds=100 * i), session_id=session_id, source="camera",
                                  metric_name=metric, metric_value=angle, metadata={"confidence": 0.9})
                )
    # Landmark glitch inside rep 2 (angle 10 at confidence 0.2). Must be ignored by every statistic.
    readings.append(
        SensorReading(time=BASE + timedelta(seconds=4, milliseconds=55), session_id=session_id, source="camera",
                      metric_name="left_knee_angle", metric_value=10.0, metadata={"confidence": 0.2})
    )
    return readings, windows


def count(session_id):
    with psycopg.connect(REAL_TIGERDATA_URL, connect_timeout=10) as conn:
        return conn.execute("select count(*) from sensor_readings where session_id = %s", (session_id,)).fetchone()[0]


# ---- connection and schema --------------------------------------------------


def test_connects_and_sensor_readings_is_a_hypertable(tiger):
    info = tiger.ping()
    assert info["timescaledb"] and info["sensor_readings_is_hypertable"] is True


# ---- insert / retrieve / association / batching -----------------------------


def test_insert_and_retrieve_with_session_association(tiger, sid):
    readings, _ = build_workout(sid)
    assert tiger.insert_readings(readings) == len(readings)
    rows = tiger.query("select session_id, source, metric_name, metadata from sensor_readings where session_id = %s limit 5", (sid,))
    assert rows and all(r["session_id"] == sid and r["source"] == "camera" for r in rows)
    assert rows[0]["metadata"].get("confidence") in (0.9, 0.2)
    other = tiger.query("select count(*) as n from sensor_readings where session_id = %s", (uuid4(),))
    assert other[0]["n"] == 0  # readings are scoped to their session


def test_large_batch_insert(tiger, sid):
    batch = [
        SensorReading(time=BASE + timedelta(milliseconds=10 * i), session_id=sid, source="imu", sensor_id="esp32-01",
                      metric_name="spine_lumbar", metric_value=(i % 300) / 10)
        for i in range(1200)
    ]
    assert tiger.insert_readings(batch) == 1200
    assert count(sid) == 1200


def test_retrying_a_batch_does_not_create_duplicates(tiger, sid):
    readings, _ = build_workout(sid)
    first = tiger.insert_readings(readings)
    assert tiger.insert_readings(readings) == 0  # the dedupe index + ON CONFLICT DO NOTHING
    assert count(sid) == first


# ---- analytics return the right numbers -------------------------------------


def test_angle_stats_are_correct_and_ignore_low_confidence_frames(tiger, sid):
    readings, _ = build_workout(sid)
    tiger.insert_readings(readings)
    trusted = [r.metric_value for r in readings if r.metadata["confidence"] >= 0.6]
    s = analytics.angle_stats(tiger, sid)
    assert s["samples"] == len(trusted)
    assert s["min_deg"] == 85.0  # NOT the 10-degree glitch
    assert s["max_deg"] == TOP
    assert s["avg_deg"] == pytest.approx(sum(trusted) / len(trusted), abs=0.01)
    assert s["stddev_deg"] > 0


def test_single_joint_stats_and_empty_session(tiger, sid):
    readings, _ = build_workout(sid)
    tiger.insert_readings(readings)
    only_right = analytics.angle_stats(tiger, sid, metrics=["right_knee_angle"])
    assert only_right["samples"] == 63 and only_right["min_deg"] == 85.0
    empty = analytics.angle_stats(tiger, uuid4())
    assert empty["samples"] == 0 and empty["avg_deg"] is None


def test_rep_trends_depth_and_range_across_reps(tiger, sid):
    readings, windows = build_workout(sid)
    tiger.insert_readings(readings)
    out = analytics.rep_trends(tiger, sid, windows)
    assert [r["rep_number"] for r in out["reps"]] == [1, 2, 3]
    assert [r["deepest_deg"] for r in out["reps"]] == [85.0, 90.0, 95.0]  # glitch in rep 2 ignored
    assert [r["range_deg"] for r in out["reps"]] == [85.0, 80.0, 75.0]
    assert [r["top_deg"] for r in out["reps"]] == [TOP] * 3
    assert out["depth_trend_deg_per_rep"] == pytest.approx(5.0, abs=0.01)  # getting shallower
    assert out["range_trend_deg_per_rep"] == pytest.approx(-5.0, abs=0.01)  # shrinking range of motion


def test_movement_range_shrinks_across_the_set(tiger, sid):
    readings, _ = build_workout(sid)
    tiger.insert_readings(readings)
    out = analytics.movement_range_over_time(tiger, sid, bucket_seconds=4)
    assert [b["range_deg"] for b in out["buckets"]] == [85.0, 80.0, 75.0]
    assert out["trend_deg_per_min"] == pytest.approx(-75.0, abs=0.5)  # -5 deg every 4 s


def test_readings_in_a_time_window_raw_and_downsampled(tiger, sid):
    readings, _ = build_workout(sid)
    tiger.insert_readings(readings)
    start, end = BASE, BASE + timedelta(seconds=2)  # [start, end): 20 samples per knee, end excluded
    raw = analytics.readings_in_window(tiger, sid, start, end, metrics=["left_knee_angle"])
    assert len(raw) == 20 and [r["time"] for r in raw] == sorted(r["time"] for r in raw)
    assert {r["metric"] for r in raw} == {"left_knee_angle"}
    assert raw[0]["value"] == squat_angles(DEPTHS[0])[0]

    down = analytics.readings_in_window(tiger, sid, start, end, metrics=["left_knee_angle"], bucket_seconds=1)
    assert len(down) == 2 and all(d["samples"] == 10 for d in down)  # 20 rows -> 2 one-second averages
    assert analytics.readings_in_window(tiger, sid, end + timedelta(hours=1), end + timedelta(hours=2)) == []


def test_sensor_overview_and_esp32_metrics(tiger, sid):
    readings, _ = build_workout(sid)
    readings.append(SensorReading(time=BASE, session_id=sid, source="imu", sensor_id="esp32-01", metric_name="spine_lumbar", metric_value=18.0))
    readings.append(SensorReading(time=BASE, session_id=sid, source="imu", sensor_id="esp32-01", metric_name="imu_pitch", metric_value=12.5))
    tiger.insert_readings(readings)
    o = analytics.sensor_overview(tiger, sid)
    assert o["metrics"]["spine_lumbar"]["source"] == "imu" and o["metrics"]["imu_pitch"]["avg"] == 12.5
    assert o["readings"] == len(readings)


def test_coach_context_end_to_end_with_both_databases(tiger, sid):
    readings, windows = build_workout(sid)
    tiger.insert_readings(readings)
    supabase = InMemoryStore()
    for n, start, end in windows:
        supabase.save_repetition(sid, n, {"min_knee_angle_deg": DEPTHS[n - 1], "duration_ms": 2000,
                                          "started_at": start.isoformat(), "ended_at": end.isoformat()})
    ctx = coach_context.build_coach_context(supabase, tiger, sid)
    assert ctx["supabase"]["stats"]["rep_count"] == 3
    ts = ctx["timeseries"]
    assert ts["available"] is True and ts["knee_angle"]["min_deg"] == 85.0
    assert ts["rep_trends"]["depth_trend_deg_per_rep"] == pytest.approx(5.0, abs=0.01)
    assert ts["overview"]["readings"] == len(readings)


def test_delete_session_readings_removes_only_that_workout(tiger, sid):
    readings, _ = build_workout(sid)
    tiger.insert_readings(readings)
    other = uuid4()
    other_rows = [SensorReading(time=BASE, session_id=other, source="imu", metric_name="imu_pitch", metric_value=1.0)]
    tiger.insert_readings(other_rows)
    try:
        assert tiger.delete_session_readings(sid) == len(readings)
        assert count(sid) == 0 and count(other) == 1  # the neighbour is untouched
    finally:
        tiger.delete_session_readings(other)
