from uuid import uuid4

from app.services.rep_counter import (
    DropReason,
    PoseSample,
    SquatConfig,
    SquatPhase,
    SquatRepCounter,
)
from app.simulation import squat_messages


def sample(seq, ts, left=170.0, right=None, conf=0.95):
    return PoseSample(seq, ts, conf, left, left if right is None else right)


def feed(counter, messages):
    reps, results = [], []
    for m in messages:
        d = m["data"]
        r = counter.update(
            PoseSample(
                m["sequence"],
                m["timestamp_ms"],
                d["landmark_confidence"],
                d["left_knee_angle_deg"],
                d["right_knee_angle_deg"],
            )
        )
        results.append(r)
        if r.rep:
            reps.append(r.rep)
    return reps, results


def test_scripted_five_rep_demo_counts_exactly_five():
    c = SquatRepCounter()
    reps, _ = feed(c, squat_messages(uuid4(), reps=5))
    assert [r.rep_number for r in reps] == [1, 2, 3, 4, 5]
    assert c.rep_count == 5
    assert c.phase is SquatPhase.STANDING
    assert c.aborted_attempts == 0 and c.dropped_frames == 0


def test_rep_metrics_are_plausible():
    c = SquatRepCounter()
    reps, _ = feed(c, squat_messages(uuid4(), reps=1, depth_deg=85, asymmetry_deg=4))
    m = reps[0]
    assert 83 <= m.min_knee_angle_deg <= 87
    assert m.max_asymmetry_deg == 4.0
    # Measured between the standing thresholds, so shorter than the 2400 ms cycle.
    assert 1200 <= m.duration_ms <= 2400
    assert m.descent_ms + m.ascent_ms <= m.duration_ms


def test_shallow_squat_is_not_counted():
    c = SquatRepCounter()
    reps, _ = feed(c, squat_messages(uuid4(), reps=3, depth_deg=130))
    assert reps == [] and c.rep_count == 0
    assert c.aborted_attempts == 3


def test_jitter_around_thresholds_does_not_double_count():
    c = SquatRepCounter()
    sid = uuid4()
    msgs = list(squat_messages(sid, reps=1, rest_frames=3))
    reps, _ = feed(c, msgs)
    assert len(reps) == 1
    # Wobble ±3 deg around the standing/descent boundary after the rep.
    seq, ts = msgs[-1]["sequence"], msgs[-1]["timestamp_ms"]
    for i in range(1, 40):
        angle = 155 + (3 if i % 2 else -3)
        assert c.update(sample(seq + i, ts + i * 33, angle)).rep is None
    assert c.rep_count == 1


def test_too_fast_rep_is_discarded():
    c = SquatRepCounter()
    reps, _ = feed(c, squat_messages(uuid4(), reps=1, rep_ms=400))
    assert reps == [] and c.rep_count == 0 and c.aborted_attempts == 1


def test_low_confidence_frames_are_dropped_and_edge_reported():
    c = SquatRepCounter()
    c.update(sample(0, 0))
    r1 = c.update(sample(1, 33, 90, conf=0.2))
    r2 = c.update(sample(2, 66, 90, conf=0.2))
    r3 = c.update(sample(3, 99, 170, conf=0.9))
    assert r1.dropped is DropReason.LOW_CONFIDENCE and r1.tracking_changed is True
    assert r2.dropped is DropReason.LOW_CONFIDENCE and r2.tracking_changed is None
    assert r3.dropped is None and r3.tracking_changed is False
    assert c.dropped_frames == 2
    assert c.phase is SquatPhase.STANDING  # bad frames did not move the state


def test_duplicate_and_out_of_order_sequences_are_ignored():
    c = SquatRepCounter()
    assert c.update(sample(5, 100)).dropped is None
    assert c.update(sample(5, 133)).dropped is DropReason.DUPLICATE
    assert c.update(sample(4, 166)).dropped is DropReason.OUT_OF_ORDER
    assert c.update(sample(6, 90)).dropped is DropReason.OUT_OF_ORDER  # timestamp went backwards


def test_single_visible_knee_still_counts_and_has_no_asymmetry():
    c = SquatRepCounter()
    msgs = list(squat_messages(uuid4(), reps=1))
    for m in msgs:
        m["data"]["right_knee_angle_deg"] = None
    reps, _ = feed(c, msgs)
    assert len(reps) == 1 and reps[0].max_asymmetry_deg is None


def test_session_starting_mid_squat_does_not_count_until_standing_seen():
    c = SquatRepCounter()
    # Begin at the bottom and stand up: must not produce a rep.
    for i, angle in enumerate([90, 100, 120, 140, 165]):
        assert c.update(sample(i, i * 100, angle)).rep is None
    assert c.phase is SquatPhase.STANDING and c.rep_count == 0


def test_tracking_gap_mid_rep_aborts_and_disarms():
    c = SquatRepCounter(config=SquatConfig(max_gap_ms=1000))
    c.update(sample(0, 0, 170))
    c.update(sample(1, 100, 140))
    c.update(sample(2, 200, 100))
    r = c.update(sample(3, 5000, 100))  # 4.8 s of silence
    assert r.rep is None and c.phase is SquatPhase.UNARMED
    assert c.aborted_attempts == 1
    assert c.update(sample(4, 5100, 168)).phase is SquatPhase.STANDING


def test_bounce_at_bottom_is_one_rep():
    c = SquatRepCounter()
    angles = [170, 140, 100, 100, 125, 105, 100, 125, 150, 165, 170]
    reps = [
        r.rep for i, a in enumerate(angles) if (r := c.update(sample(i, i * 250, a))).rep
    ]
    assert len(reps) == 1


def test_reset_tracking_discards_partial_rep_and_requires_standing_again():
    c = SquatRepCounter()
    for i, a in enumerate([170, 140, 100]):
        c.update(sample(i, i * 300, a))
    assert c.phase is SquatPhase.BOTTOM
    c.reset_tracking()  # e.g. user paused mid-squat
    assert c.phase is SquatPhase.UNARMED and c.rep_count == 0
    # Resuming mid-movement must not produce a rep...
    for i, a in enumerate([100, 120, 150, 168], start=3):
        assert c.update(sample(i, 100_000 + i * 300, a)).rep is None
    # ...but the next full squat counts normally.
    reps, _ = feed(c, squat_messages(uuid4(), reps=1, start_sequence=50, start_ts=200_000))
    assert len(reps) == 1 and c.rep_count == 1
