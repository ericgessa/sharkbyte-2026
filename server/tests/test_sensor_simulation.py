"""Tests for the SIMULATED upper-back sensor dataset (app/sensor_simulation.py)."""

from __future__ import annotations

import json
import statistics

import pytest

from app.sensor_simulation import SCENARIOS, generate, write_dataset


def _pitch(m):
    return m["data"]["imu_pitch_deg"]


def _in_window(messages, start_ms, end_ms):
    return [m for m in messages if start_ms <= m["timestamp_ms"] <= end_ms]


def _rep_peaks(rec):
    return [max(_pitch(m) for m in _in_window(rec.messages, r["start_ms"], r["end_ms"])) for r in rec.labels["reps"]]


def _standing(rec):
    """Messages outside every rep window."""
    reps = rec.labels["reps"]
    return [m for m in rec.messages if not any(r["start_ms"] <= m["timestamp_ms"] <= r["end_ms"] for r in reps)]


@pytest.mark.parametrize("name", SCENARIOS)
def test_messages_match_the_sensor_update_contract(name):
    sensors = pytest.importorskip("app.schemas.sensors", reason="SensorUpdate lives on main; merge main first")
    for m in generate(name).messages:
        sensors.SensorUpdate.model_validate_json(json.dumps(m))


@pytest.mark.parametrize("name", SCENARIOS)
def test_sequence_and_device_clock_strictly_increase(name):
    msgs = generate(name).messages
    assert all(b["sequence"] > a["sequence"] for a, b in zip(msgs, msgs[1:]))
    assert all(b["timestamp_ms"] > a["timestamp_ms"] for a, b in zip(msgs, msgs[1:]))


@pytest.mark.parametrize("name", SCENARIOS)
def test_labels_are_marked_simulated_and_match_the_data(name):
    rec = generate(name, reps=4)
    assert rec.labels["simulated"] is True
    assert rec.labels["message_count"] == len(rec.messages)
    assert [r["rep_number"] for r in rec.labels["reps"]] == [1, 2, 3, 4]
    for r in rec.labels["reps"]:
        assert r["start_ms"] < r["bottom_ms"] < r["end_ms"]


def test_same_seed_same_data_different_seed_different_data():
    assert generate("good_form", seed=1).messages == generate("good_form", seed=1).messages
    assert generate("good_form", seed=1).messages != generate("good_form", seed=2).messages


def test_sample_rate():
    msgs = generate("good_form", rate_hz=50).messages
    gaps = [b["timestamp_ms"] - a["timestamp_ms"] for a, b in zip(msgs, msgs[1:])]
    assert statistics.median(gaps) == 20


def test_rep_peaks_match_the_labelled_bottom_pitch():
    for name in ("good_form", "forward_lean", "lean_creep"):
        rec = generate(name)
        for peak, r in zip(_rep_peaks(rec), rec.labels["reps"]):
            assert peak == pytest.approx(r["bottom_pitch_deg"], abs=2.5), name


def test_forward_lean_leans_further_than_good_form():
    assert min(_rep_peaks(generate("forward_lean"))) > max(_rep_peaks(generate("good_form"))) + 15


def test_lean_creep_gets_worse_and_slower_every_rep():
    rec = generate("lean_creep")
    peaks = _rep_peaks(rec)
    assert all(b > a for a, b in zip(peaks, peaks[1:]))
    durations = [r["end_ms"] - r["start_ms"] for r in rec.labels["reps"]]
    assert durations[-1] > durations[0] + 400


def test_lateral_shift_tilts_one_way_and_good_form_stays_level():
    shift = max(m["data"]["imu_roll_deg"] for m in generate("lateral_shift").messages)
    level = max(abs(m["data"]["imu_roll_deg"]) for m in generate("good_form").messages)
    assert shift > 6.5
    assert level < 3


def test_slouched_standing_is_rounded_between_reps():
    slouched = statistics.median(_pitch(m) for m in _standing(generate("slouched_standing")))
    upright = statistics.median(_pitch(m) for m in _standing(generate("good_form")))
    assert slouched > 15 > 8 > upright


def test_gravity_shows_up_in_the_accelerometer():
    for m in _standing(generate("good_form")):
        d = m["data"]
        magnitude = (d["accel_x_g"] ** 2 + d["accel_y_g"] ** 2 + d["accel_z_g"] ** 2) ** 0.5
        assert magnitude == pytest.approx(1.0, abs=0.1)


def test_dropout_leaves_a_gap_in_time_and_sequence():
    rec = generate("sensor_dropout")
    msgs = rec.messages
    [gap] = rec.labels["dropouts"]
    assert gap["missing_messages"] > 50
    assert max(b["timestamp_ms"] - a["timestamp_ms"] for a, b in zip(msgs, msgs[1:])) > 1400
    assert max(b["sequence"] - a["sequence"] for a, b in zip(msgs, msgs[1:])) == gap["missing_messages"] + 1
    assert _in_window(msgs, gap["start_ms"] + 5, gap["end_ms"] - 5) == []


def test_only_dropout_has_dropouts():
    for name in SCENARIOS:
        assert bool(generate(name).labels["dropouts"]) == (name == "sensor_dropout")


def test_write_dataset_writes_jsonl_and_labels(tmp_path):
    paths = write_dataset(tmp_path, ["good_form", "lateral_shift"], reps=2)
    assert sorted(p.name for p in paths) == [
        "good_form.jsonl",
        "good_form.labels.json",
        "lateral_shift.jsonl",
        "lateral_shift.labels.json",
    ]
    lines = (tmp_path / "good_form.jsonl").read_text(encoding="utf-8").splitlines()
    labels = json.loads((tmp_path / "good_form.labels.json").read_text(encoding="utf-8"))
    assert len(lines) == labels["message_count"]
    assert json.loads(lines[0])["type"] == "sensor_update"


@pytest.mark.parametrize("kwargs", [{"name": "nope"}, {"name": "good_form", "reps": 0}, {"name": "good_form", "rate_hz": 500}])
def test_rejects_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        generate(**kwargs)
