from app.services.coaching import CoachConfig, MESSAGES, RuleCoach
from app.services.rep_counter import RepMetrics


def rep(n, depth=85.0, asym=2.0):
    return RepMetrics(n, depth, 400, 400, 1700, asym)


def run(coach, reps):
    """Feed reps; return {rep_number: issue_code} for the ones that produced feedback."""
    out = {}
    for r in reps:
        d = coach.observe(r)
        if d:
            out[r.rep_number] = d.issue_code
    return out


def test_steady_reps_get_no_feedback():
    assert run(RuleCoach(), [rep(n) for n in range(1, 11)]) == {}


def test_no_feedback_while_baseline_is_forming():
    # Even very shallow early reps can't be "worse than baseline": they ARE the baseline.
    assert run(RuleCoach(), [rep(1, 100), rep(2, 100), rep(3, 100)]) == {}


def test_reduced_depth_needs_two_consecutive_shallow_reps_after_baseline():
    reps = [rep(1), rep(2), rep(3), rep(4, 98), rep(5, 98)]
    assert run(RuleCoach(), reps) == {5: "reduced_depth"}  # not at rep 4: a single shallow rep is noise


def test_one_shallow_rep_then_recovery_is_ignored():
    assert run(RuleCoach(), [rep(1), rep(2), rep(3), rep(4, 98), rep(5), rep(6)]) == {}


def test_small_depth_change_is_ignored():
    assert run(RuleCoach(), [rep(1), rep(2), rep(3), rep(4, 90), rep(5, 90)]) == {}


def test_baseline_uses_median_so_one_odd_early_rep_does_not_matter():
    reps = [rep(1, 85), rep(2, 120), rep(3, 85), rep(4, 98), rep(5, 98)]  # median 85
    assert run(RuleCoach(), reps) == {5: "reduced_depth"}


def test_asymmetry_flagged_when_repeated():
    reps = [rep(1, asym=20), rep(2, asym=20), rep(3)]
    assert run(RuleCoach(), reps) == {2: "asymmetry"}


def test_missing_asymmetry_data_is_never_flagged():
    assert run(RuleCoach(), [rep(n, asym=None) for n in range(1, 8)]) == {}


def test_same_issue_is_not_repeated_within_cooldown():
    reps = [rep(1), rep(2), rep(3)] + [rep(n, 98) for n in range(4, 13)]
    fired = run(RuleCoach(), reps)
    assert fired == {5: "reduced_depth", 9: "reduced_depth"}  # cooldown 4 reps
    assert all(b - a >= 4 for a, b in zip(sorted(fired), sorted(fired)[1:]))


def test_different_issues_still_respect_min_gap_between_any_messages():
    c = RuleCoach()
    # rep 5 -> depth message; asymmetry also true at rep 6 but only 1 rep later: suppressed.
    reps = [rep(1), rep(2), rep(3), rep(4, 98, 20), rep(5, 98, 20), rep(6, 98, 20), rep(7, 98, 20)]
    assert run(c, reps) == {5: "reduced_depth", 7: "asymmetry"}


def test_message_cap_per_session():
    c = RuleCoach(CoachConfig(max_messages=2, same_issue_cooldown_reps=1, min_reps_between_messages=1))
    fired = run(c, [rep(1), rep(2), rep(3)] + [rep(n, 98) for n in range(4, 20)])
    assert len(fired) == 2


def test_decision_content_is_descriptive_and_idempotent_id():
    c = RuleCoach()
    d = None
    for r in [rep(1), rep(2), rep(3), rep(4, 98), rep(5, 98)]:
        d = c.observe(r) or d
    assert d.message == MESSAGES["reduced_depth"]
    assert d.action == "send_feedback" and d.source == "rules" and d.rep_number == 5
    assert d.event_id  # generated once per decision so a retried save is an upsert
    for word in ("injur", "dangerous", "unsafe", "pain", "diagnos"):
        assert word not in d.message.lower()
