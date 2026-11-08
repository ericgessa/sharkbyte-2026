"""Form IQ: rejected reps (rep_counter + rep_rejected over the socket) and back shape per rep (spine_form).

Deterministic throughout; Gemini is not involved. Sensor and pose data here are SIMULATED.
"""

import json
from types import SimpleNamespace

import pytest

from app.api import websocket as wsmod
from app.services.agent import FOCUS, ISSUE_LABELS, ISSUE_MEANINGS, issues_seen, rules_summary
from app.services.rep_counter import PoseSample, PushUpRepCounter, RejectReason, SquatRepCounter
from app.services.spine_form import MESSAGES, RULES, SpineFormCoach
from app.simulation import squat_messages
from app.spine_simulation import SPINE_SCENARIOS, generate_spine
from tests.fakes import InMemoryStore
from tests.test_agent import make_client, new_session, open_ws, wait_for
from app.services.agent import CoachAgent


# ---- rejected attempts (the counter) ---------------------------------------------------------------


def _run(counter, angles, step_ms=33, joint="knee"):
    results = []
    for i, a in enumerate(angles):
        kw = {"left_knee_deg": a, "right_knee_deg": a} if joint == "knee" else {"left_elbow_deg": a, "right_elbow_deg": a}
        results.append(counter.update(PoseSample(sequence=i, timestamp_ms=i * step_ms, confidence=0.95, **kw)))
    return results


def _dip(top, low, frames=40):
    """top -> low -> top along a smooth curve."""
    import math

    return [top - (top - low) * 0.5 * (1 - math.cos(2 * math.pi * k / frames)) for k in range(frames + 1)]


def test_a_partial_squat_is_rejected_with_its_reason_and_depth():
    results = _run(SquatRepCounter(), [170] * 10 + _dip(170, 125) + [170] * 10)
    rejected = [r.rejected for r in results if r.rejected]
    assert len(rejected) == 1 and rejected[0].reason is RejectReason.PARTIAL_RANGE
    assert rejected[0].min_angle_deg == pytest.approx(125, abs=1) and rejected[0].joint == "knee"
    assert not any(r.rep for r in results)


def test_a_small_dip_is_not_reported():
    """Halfway rule: 150 (leaving the top) to 110 (bottom) -> only attempts reaching 130 or lower are reported."""
    results = _run(SquatRepCounter(), [170] * 10 + _dip(170, 140) + [170] * 10)
    assert not any(r.rejected for r in results)


def test_a_full_rep_counts_and_is_not_rejected():
    results = _run(SquatRepCounter(), [170] * 10 + _dip(170, 85) + [170] * 10)
    assert sum(1 for r in results if r.rep) == 1 and not any(r.rejected for r in results)


def test_a_too_fast_rep_is_rejected():
    results = _run(SquatRepCounter(), [170] * 10 + _dip(170, 85, frames=18) + [170] * 10, step_ms=33)  # ~0.6 s
    (rejected,) = [r.rejected for r in results if r.rejected]
    assert rejected.reason is RejectReason.TOO_FAST and 300 <= rejected.duration_ms < 800


# ---- glitch guard: pose tracking that returns nonsense for a few frames ------------------------------


def _quiet(results):
    return not any(r.rep or r.rejected for r in results)


def test_one_and_two_frame_spikes_never_count_or_get_reported():
    angles = [170] * 15
    for spike in ([60], [55, 70], [20], [90, 85]):
        angles += spike + [170] * 12
    for counter, joint in ((SquatRepCounter(), "knee"), (PushUpRepCounter(), "elbow")):
        assert _quiet(_run(counter, angles, joint=joint))


def test_a_very_short_deep_dip_is_a_glitch_not_a_too_fast_rep():
    """Deep enough to be a rep but over in ~0.2 s: no human does that, so it is ignored silently."""
    c = SquatRepCounter()
    assert _quiet(_run(c, [170] * 10 + _dip(170, 85, frames=6) + [170] * 10))
    assert c.rep_count == 0


def test_impossible_angles_are_ignored():
    """Seen live: an elbow reading 11-18 deg in the middle of a push-up. Below 30 deg is not a real joint angle."""
    c = PushUpRepCounter()
    assert _quiet(_run(c, [170] * 10 + [15] * 8 + [170] * 10, joint="elbow"))
    assert c.glitch_frames == 8


def test_a_mistracked_side_is_not_followed():
    """One elbow stays straight, the other reads deep for half a second: the sides disagree by far more than any real
    asymmetry, so the counter keeps following the side that continues the motion."""
    c = PushUpRepCounter()
    results = []
    for i in range(60):
        left = 170.0
        right = 70.0 if 20 <= i < 35 else 170.0
        results.append(c.update(PoseSample(i, i * 33, 0.9, left_elbow_deg=left, right_elbow_deg=right)))
    assert _quiet(results) and c.glitch_frames == 15


def test_a_mistracked_side_is_not_reported_as_asymmetry():
    c = PushUpRepCounter()
    angles = [170] * 10 + _dip(170, 85) + [170] * 10
    results = []
    for i, a in enumerate(angles):
        right = 20.0 if i == 30 else a  # one implausible frame on one side, mid-rep
        results.append(c.update(PoseSample(i, i * 33, 0.9, left_elbow_deg=a, right_elbow_deg=right)))
    (rep,) = [r.rep for r in results if r.rep]
    assert rep.max_asymmetry_deg == 0.0


def test_a_glitch_mid_rep_does_not_change_the_recorded_depth():
    angles = [170] * 10 + _dip(170, 85) + [170] * 10
    angles[28] = 40  # a single nonsense frame near the bottom
    (rep,) = [r.rep for r in _run(SquatRepCounter(), angles) if r.rep]
    assert rep.min_angle_deg == pytest.approx(85, abs=1)


# ---- one attempt per descent ---------------------------------------------------------------------------


def test_wobbling_around_the_top_is_one_attempt_not_several():
    """A shallow dip, a brief flicker above the top threshold, then down again: ONE rejected attempt."""
    wobble = [162, 158, 163, 157, 161, 159]  # under the 250 ms top hold
    angles = [170] * 10 + _dip(170, 125)[:-8] + wobble + _dip(170, 125)[12:] + [170] * 12
    rejected = [r.rejected for r in _run(SquatRepCounter(), angles) if r.rejected]
    assert len(rejected) == 1 and rejected[0].reason is RejectReason.PARTIAL_RANGE


def test_an_attempt_that_flickers_at_the_top_then_reaches_the_bottom_is_a_rep():
    angles = [170] * 10 + _dip(170, 125)[:-8] + [162, 163, 162] + _dip(170, 85)[14:] + [170] * 12
    results = _run(SquatRepCounter(), angles)
    assert sum(1 for r in results if r.rep) == 1 and not any(r.rejected for r in results)


def test_two_separate_shallow_attempts_are_two_rejections():
    """Holding the top in between ends the first attempt, so a second shallow one is reported on its own."""
    angles = [170] * 10 + _dip(170, 125) + [170] * 15 + _dip(170, 125) + [170] * 15
    rejected = [r.rejected for r in _run(SquatRepCounter(), angles) if r.rejected]
    assert len(rejected) == 2


def test_a_partial_push_up_is_rejected_on_the_elbow():
    results = _run(PushUpRepCounter(), [170] * 10 + _dip(170, 118) + [170] * 10, joint="elbow")
    (rejected,) = [r.rejected for r in results if r.rejected]
    assert rejected.reason is RejectReason.PARTIAL_RANGE and rejected.joint == "elbow"


def test_rep_rejected_reaches_the_app_and_is_saved():
    store = InMemoryStore()
    with make_client(store, CoachAgent(None)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            for m in squat_messages(sid, reps=3, depths=[85, 125, 85]):
                ws.send_text(json.dumps(m))
            events = [ws.receive_json() for _ in range(3)]
        finally:
            cm.__exit__(None, None, None)
    assert [e["type"] for e in events] == ["rep_completed", "rep_rejected", "rep_completed"]
    rej = events[1]
    assert rej["reason"] == "partial_range" and rej["rep_count"] == 1 and rej["joint"] == "knee"
    assert rej["message"] == wsmod.REJECT_MESSAGES[RejectReason.PARTIAL_RANGE]
    assert events[2]["rep_count"] == 2  # the rejected attempt did not take a number
    assert wait_for(lambda: len(store.events.get(sid, {})) == 1)
    (saved,) = store.events[sid].values()
    assert saved["issue_code"] == "partial_range" and saved["action"] == "rep_rejected"


# ---- back shape per rep (spine_form) --------------------------------------------------------------


def _coach_for(name, **kw):
    samples, windows = generate_spine(name, **kw)
    coach = SpineFormCoach(SPINE_SCENARIOS[name].exercise)
    for t, upper, mid, lumbar in samples:
        coach.add(t, upper, mid, lumbar)
    results = [coach.judge_rep(i + 1, start, end) for i, (start, end) in enumerate(windows)]
    return coach, results


@pytest.mark.parametrize("name", list(SPINE_SCENARIOS))
@pytest.mark.parametrize("seed", range(3))
def test_each_rep_is_judged_with_exactly_the_scenarios_issues(name, seed):
    coach, results = _coach_for(name, seed=seed)
    expected = set(SPINE_SCENARIOS[name].expected_issues)
    for form, _ in results:
        assert form.judged and set(form.issues) == expected
    assert {d.issue_code for d in coach.decisions} == expected


def test_a_warning_needs_two_reps_in_a_row_and_is_rate_limited():
    coach, results = _coach_for("pushup_hips_sagging", reps=12)
    reps = [form.rep_number for form, d in results if d is not None]
    assert reps[0] == 2
    assert all(b - a >= coach.config.same_issue_cooldown_reps for a, b in zip(reps, reps[1:]))
    assert results[1][1].message == MESSAGES["hips_sagging"] and results[1][1].source == "rules"


def test_too_few_samples_means_not_judged_and_no_warning():
    coach = SpineFormCoach("push-up")
    coach.add(0, 0.0, 0.0, -15.0)
    form, decision = coach.judge_rep(1, 0, 2000)
    assert form.judged is False and decision is None


def test_after_pain_reps_are_still_recorded_but_never_voiced():
    samples, windows = generate_spine("squat_lower_back_rounding")
    coach = SpineFormCoach("squat")
    coach.enabled = False
    for t, u, m, l in samples:
        coach.add(t, u, m, l)
    results = [coach.judge_rep(i + 1, s, e) for i, (s, e) in enumerate(windows)]
    assert all("lower_back_rounding" in f.issues for f, _ in results)
    assert coach.decisions == []


def test_exercises_without_spine_rules_are_not_judged():
    assert SpineFormCoach("pull-up").supported is False
    assert set(RULES) == {"push-up", "squat"}


def test_the_stored_record_has_the_rep_numbers_the_report_needs():
    _, results = _coach_for("pushup_hips_piking")
    m = results[0][0].as_metrics()
    assert m["judged"] and m["issues"] == ["hips_piking"] and m["lumbar_max_deg"] > 10


def test_record_spine_reads_the_section_angles_when_the_schema_has_them():
    live = SimpleNamespace(
        spine=None,
        exercise=SimpleNamespace(slug="push-up"),
        pain_reported=False,
        clock=SimpleNamespace(to_datetime=lambda key, ts: SimpleNamespace(timestamp=lambda: ts / 1000)),
    )
    with_spine = SimpleNamespace(
        device_id="esp32-spine", timestamp_ms=1000, data=SimpleNamespace(spine_upper_deg=1.0, spine_mid_deg=0.5, spine_lumbar_deg=-9.0)
    )
    wsmod._record_spine(live, with_spine)
    assert live.spine is not None and live.spine.supported and len(live.spine._samples) == 1

    live2 = SimpleNamespace(spine=None, exercise=SimpleNamespace(slug="push-up"), pain_reported=False, clock=live.clock)
    wsmod._record_spine(live2, SimpleNamespace(device_id="x", timestamp_ms=1, data=SimpleNamespace(imu_pitch_deg=3.0)))
    assert live2.spine is None  # today's schema has no section angles: nothing happens


# ---- the end-of-workout report ---------------------------------------------------------------------


def test_every_new_code_has_a_label_a_meaning_and_a_focus():
    for code in (*MESSAGES, "partial_range", "too_fast"):
        assert code in ISSUE_LABELS and code in ISSUE_MEANINGS and code in FOCUS


def test_the_rules_report_counts_rejected_attempts_apart_from_cues():
    ctx = {
        "supabase": {
            "stats": {"rep_count": 10},
            "coaching_events": [
                {"issue_code": "partial_range", "action": "rep_rejected"},
                {"issue_code": "partial_range", "action": "rep_rejected"},
                {"issue_code": "hips_sagging", "action": "send_feedback"},
            ],
        }
    }
    t = rules_summary(ctx)
    assert "2 attempts didn't count." in t and "1 coaching cue" in t
    assert "attempts that didn't reach the bottom (2x)" in t
    assert t.endswith(f"Next time, focus on this: {FOCUS['partial_range']}.")
    assert issues_seen(ctx)[0]["code"] == "partial_range"
