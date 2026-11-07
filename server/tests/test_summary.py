from app.services.summary import compute_stats


def rep(angle, duration=1700, asym=2.0):
    return {"metrics": {"min_knee_angle_deg": angle, "duration_ms": duration, "max_asymmetry_deg": asym}}


def test_empty_workout_has_no_numbers_but_does_not_crash():
    s = compute_stats([])
    assert s["rep_count"] == 0
    assert all(v is None for k, v in s.items() if k != "rep_count")


def test_basic_aggregates():
    s = compute_stats([rep(85, 1600, 2), rep(90, 1800, 6), rep(80, 1700, 4)])
    assert s["rep_count"] == 3
    assert s["avg_min_knee_angle_deg"] == 85.0
    assert s["deepest_knee_angle_deg"] == 80.0  # smaller angle = deeper
    assert s["shallowest_knee_angle_deg"] == 90.0
    assert s["avg_rep_duration_ms"] == 1700
    assert s["max_asymmetry_deg"] == 6.0
    assert s["depth_change_deg"] is None  # too few reps for a trend


def test_depth_change_positive_means_later_reps_were_shallower():
    s = compute_stats([rep(85), rep(85), rep(85), rep(98), rep(98)])
    # first three avg 85, last three avg (85+98+98)/3 = 93.67
    assert s["depth_change_deg"] == 8.7


def test_depth_change_negative_when_getting_deeper():
    assert compute_stats([rep(100), rep(100), rep(100), rep(90)])["depth_change_deg"] < 0


def test_missing_metrics_are_skipped_not_fatal():
    s = compute_stats([{"metrics": {}}, {"metrics": None}, rep(90, asym=None)])
    assert s["rep_count"] == 3 and s["avg_min_knee_angle_deg"] == 90.0 and s["max_asymmetry_deg"] is None
