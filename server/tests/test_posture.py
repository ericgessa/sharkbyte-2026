"""Posture detection checked against the SIMULATED, labelled upper-back dataset (app/sensor_simulation.py)."""

from __future__ import annotations

import copy

import pytest

from app.sensor_simulation import SCENARIOS, generate
from app.services.posture import (
    ISSUE_CODES,
    MESSAGES,
    Finding,
    PostureCoach,
    PostureConfig,
    analyze,
    detect_issues,
    render_message,
)

# lateral_shift is off by default until the wearable's roll sign is checked; these tests check the rule itself.
RULES_ON = PostureConfig(lateral_shift_enabled=True)

SEEDS = range(5)

# Issues a scenario may also legitimately show besides its labelled ones. lean_creep ends at about 60 deg,
# which really is past the excessive-lean threshold.
ALSO_ALLOWED = {"lean_creep": {"excessive_forward_lean"}}


def _all_findings(coach: PostureCoach) -> set[str]:
    return {f.issue_code for fs in coach.findings.values() for f in fs}


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", SCENARIOS)
def test_finds_exactly_the_labelled_issues(name, seed):
    rec = generate(name, seed=seed)
    found = _all_findings(analyze(rec.messages, RULES_ON))
    expected = set(rec.labels["expected_issues"])
    assert expected <= found, f"missed {expected - found}"
    assert found <= expected | ALSO_ALLOWED.get(name, set()), f"false alarm {found - expected}"


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", SCENARIOS)
def test_reps_line_up_with_the_labels(name, seed):
    rec = generate(name, seed=seed)
    coach = analyze(rec.messages, RULES_ON)
    labelled = rec.labels["reps"]
    dropped = rec.labels["dropouts"]

    # A rep cut by the Bluetooth gap is skipped, not guessed at. (It counts as aborted only if the
    # gap began after the rep had crossed the start threshold.)
    assert len(coach.tracker.reps) == len(labelled) - len(dropped)
    assert coach.tracker.aborted_reps <= len(dropped)

    for rep in coach.tracker.reps:
        [label] = [lab for lab in labelled if lab["start_ms"] <= rep.peak_ms <= lab["end_ms"]]
        assert rep.peak_pitch_deg == pytest.approx(label["bottom_pitch_deg"], abs=2.5)
        assert rep.standing_pitch_deg == pytest.approx(label["standing_pitch_deg"], abs=1.5)
        if label["bottom_roll_deg"]:
            assert rep.bottom_roll_deg == pytest.approx(label["bottom_roll_deg"], abs=1.5)


@pytest.mark.parametrize("name", ["good_form", "sensor_dropout"])
def test_good_form_gets_no_warning(name):
    assert analyze(generate(name).messages).decisions == []


def test_warnings_are_rate_limited():
    coach = analyze(generate("forward_lean", reps=20).messages)
    reps = [d.rep_number for d in coach.decisions]
    cfg = coach.config
    assert reps[0] == cfg.consecutive  # not before the issue has shown twice
    assert all(b - a >= cfg.same_issue_cooldown_reps for a, b in zip(reps, reps[1:]))
    assert len(reps) <= cfg.max_messages


def test_one_warning_per_rep_highest_priority_first():
    coach = analyze(generate("lean_creep").messages)
    [decision] = coach.decisions
    assert decision.issue_code == "forward_lean_increasing"
    assert decision.rep_number == 5
    assert decision.evidence["early_reps_peak_pitch_deg"] < min(decision.evidence["recent_peak_pitch_deg"])


def test_lateral_shift_is_off_by_default():
    assert analyze(generate("lateral_shift").messages).decisions == []


def test_lateral_shift_names_the_side():
    [d] = analyze(generate("lateral_shift").messages, RULES_ON).decisions
    assert d.evidence["side"] == "right"
    assert "to the right" in d.message


def test_strap_tilt_needs_calibration():
    """A strap sitting 15 deg forward looks like a rounded back until the mounting offset is set."""
    msgs = copy.deepcopy(generate("good_form").messages)
    for m in msgs:
        m["data"]["imu_pitch_deg"] += 15
    assert "rounded_standing_posture" in _all_findings(analyze(msgs))
    assert _all_findings(analyze(msgs, PostureConfig(mounting_offset_deg=15))) == set()


def test_standing_pitch_follows_a_sudden_posture_change():
    """Halfway through, the user starts standing 12 deg more hunched. The tracker must adopt it, not freeze."""
    msgs = copy.deepcopy(generate("good_form", reps=12).messages)
    for m in msgs[len(msgs) // 2 :]:
        m["data"]["imu_pitch_deg"] += 12
    coach = analyze(msgs)
    late = [r for r in coach.tracker.reps if r.start_ms > msgs[len(msgs) // 2]["timestamp_ms"]]
    assert len(late) >= 3  # counting resumes after a few seconds instead of sticking in one endless "rep"
    assert late[-1].standing_pitch_deg == pytest.approx(17, abs=1.5)
    assert "rounded_standing_posture" in _all_findings(coach)


def test_small_posture_change_is_followed_without_losing_reps():
    msgs = copy.deepcopy(generate("good_form", reps=8).messages)
    for m in msgs[len(msgs) // 2 :]:
        m["data"]["imu_pitch_deg"] += 4.5  # between the standing band and the rep threshold
    coach = analyze(msgs)
    assert len(coach.tracker.reps) == 8
    assert coach.tracker.reps[-1].standing_pitch_deg == pytest.approx(9.5, abs=1.5)


def test_disabled_coach_still_tracks_but_says_nothing():
    coach = PostureCoach()
    coach.enabled = False  # e.g. after a pain report
    for m in generate("forward_lean").messages:
        coach.feed(m)
    assert len(coach.tracker.reps) == 5
    assert coach.decisions == []


def test_duplicate_and_out_of_order_samples_are_dropped():
    msgs = generate("good_form").messages
    noisy = msgs[:100] + [msgs[50], msgs[99]] + msgs[100:]
    coach = analyze(noisy)
    assert coach.tracker.dropped_samples == 2
    assert len(coach.tracker.reps) == 5


def test_device_reboot_mid_set_keeps_counting():
    msgs = copy.deepcopy(generate("good_form").messages)
    cut = len(msgs) // 2
    shift = msgs[cut]["timestamp_ms"] - 1_000  # the ESP32 clock restarts near zero
    for m in msgs[cut:]:
        m["timestamp_ms"] -= shift
    coach = analyze(msgs)
    assert len(coach.tracker.reps) + coach.tracker.aborted_reps >= 4
    assert coach.decisions == []


def test_a_short_twitch_is_not_a_rep():
    coach = PostureCoach()
    t = 0
    for _ in range(30):  # standing
        coach.update(t, 5.0, 0.0)
        t += 20
    for _ in range(10):  # 200 ms forward jerk
        coach.update(t, 30.0, 0.0)
        t += 20
    for _ in range(30):
        coach.update(t, 5.0, 0.0)
        t += 20
    assert coach.tracker.reps == []
    assert coach.tracker.aborted_reps == 1


def test_messages_without_pitch_or_of_another_type_are_ignored():
    coach = PostureCoach()
    assert coach.feed({"type": "sensor_update", "timestamp_ms": 1, "data": {"accel_z_g": 1.0}}) is None
    assert coach.feed({"type": "pose_update", "timestamp_ms": 2, "data": {"imu_pitch_deg": 5.0}}) is None
    assert coach.tracker.standing_pitch() is None


def test_missing_roll_skips_the_lateral_check_only():
    msgs = copy.deepcopy(generate("forward_lean").messages)
    for m in msgs:
        del m["data"]["imu_roll_deg"]
    coach = analyze(msgs)
    assert all(r.bottom_roll_deg is None for r in coach.tracker.reps)
    assert _all_findings(coach) == {"excessive_forward_lean"}


def test_every_issue_has_a_message_and_none_diagnose():
    assert set(MESSAGES) == set(ISSUE_CODES)
    for code in ISSUE_CODES:
        text = render_message(Finding(code, {"side": "left"})).lower()
        assert "{" not in text
        for word in ("spine", "injur", "scoliosis", "diagnos", "unsafe"):
            assert word not in text


def test_detect_issues_needs_enough_reps():
    assert detect_issues([]) == []
