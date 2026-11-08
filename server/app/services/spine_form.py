"""Spine form per rep, from the wearable's section angles. Deterministic: no LLM, no clock reads, no randomness.

The wearable (hardware/README.md on Hardware_branch) runs four IMUs down the spine and sends the bend WITHIN each
section, in degrees, + = bending forward (flexion) versus the user's calibrated neutral posture:
    spine_upper_deg  = BNO085 minus MPU A,  spine_mid_deg = MPU A minus MPU B,  spine_lumbar_deg = MPU B minus MPU C.
Because each value compares two neighbouring sensors, it does not change when the whole body tilts: it reads the same
standing up or in a plank. So it judges back SHAPE during a rep, not where the rep is (top or bottom).

Division of labour:
  * The camera rep counter (rep_counter.py) decides phases and whether a rep counts.
  * This module judges each COUNTED rep's back shape inside that rep's time window and may issue a warning. It never
    rejects a rep: thresholds are provisional until tuned on labelled recordings (team decision: posture warns, range
    of motion rejects).
  * Warnings are rare: an issue must show in ``consecutive`` reps in a row, with the same rate limits as the other coaches.

Thresholds are UNTUNED starting values chosen from the README's descriptions (lumbar is "the most important one for
form"; rounding raises it) and one short push-up log. Tune them on real labelled sets before trusting them.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from app.services.coaching import CoachingDecision

Metric = Literal["upper", "mid", "lumbar"]


@dataclass(frozen=True)
class SpineRule:
    issue_code: str
    metric: Metric
    direction: Literal["above", "below"]
    threshold_deg: float
    min_fraction: float  # share of the rep's samples that must be beyond the threshold (a lasting shape, not a spike)


# Per exercise slug (services/exercises.py). Exercises not listed here are not judged.
RULES: dict[str, tuple[SpineRule, ...]] = {
    "push-up": (
        SpineRule("hips_sagging", "lumbar", "below", -8.0, 0.5),  # lower back arching: hips dropping
        SpineRule("hips_piking", "lumbar", "above", 10.0, 0.5),  # lower back flexing: hips rising
    ),
    "squat": (
        SpineRule("lower_back_rounding", "lumbar", "above", 15.0, 0.3),  # most of the bottom third of the rep
    ),
}

MESSAGES = {
    "hips_sagging": "Your hips are dropping during your push-ups. Squeeze your glutes and keep your body in one straight line.",
    "hips_piking": "Your hips are rising during your push-ups. Lower them a little so your body stays in one straight line.",
    "lower_back_rounding": "Your lower back is rounding during your squats. Keep your chest up and your back flat as you go down.",
}


@dataclass(frozen=True)
class SpineConfig:
    min_samples: int = 5  # fewer samples inside a rep window (wearable off, Bluetooth gap): the rep is not judged
    buffer_samples: int = 2400  # about 2 minutes at 20 Hz
    consecutive: int = 2
    min_reps_between_messages: int = 2
    same_issue_cooldown_reps: int = 4
    max_messages: int = 6


@dataclass(frozen=True)
class SpineRepForm:
    rep_number: int
    judged: bool  # False when there were too few samples in the rep's window
    samples: int
    issues: tuple[str, ...] = ()
    lumbar_min_deg: float | None = None
    lumbar_max_deg: float | None = None
    lumbar_mean_deg: float | None = None
    mid_max_deg: float | None = None
    upper_max_deg: float | None = None

    def as_metrics(self) -> dict[str, Any]:
        """Stored with the rep (Supabase ``repetitions.metrics["spine"]``), so the end-of-workout report can use it."""
        return {
            "judged": self.judged,
            "samples": self.samples,
            "issues": list(self.issues),
            "lumbar_min_deg": self.lumbar_min_deg,
            "lumbar_max_deg": self.lumbar_max_deg,
            "lumbar_mean_deg": self.lumbar_mean_deg,
            "mid_max_deg": self.mid_max_deg,
            "upper_max_deg": self.upper_max_deg,
        }


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


class SpineFormCoach:
    """Buffers section-angle samples (on server time) and judges each counted rep's window."""

    def __init__(self, exercise: str, config: SpineConfig | None = None) -> None:
        self.exercise = exercise
        self.rules = RULES.get(exercise, ())
        self.config = config or SpineConfig()
        self.enabled = True  # False after a pain report: still judged and stored, never voiced
        self.reps: list[SpineRepForm] = []
        self.decisions: list[CoachingDecision] = []
        self._samples: deque[tuple[float, float | None, float | None, float | None]] = deque(
            maxlen=self.config.buffer_samples
        )
        self._last_message_rep: int | None = None
        self._last_rep_by_issue: dict[str, int] = {}

    @property
    def supported(self) -> bool:
        return bool(self.rules)

    def add(self, server_ms: float, upper: float | None, mid: float | None, lumbar: float | None) -> None:
        if upper is None and mid is None and lumbar is None:
            return
        if self._samples and server_ms < self._samples[-1][0]:
            return  # out of order (clock re-anchored): keep the buffer sorted
        self._samples.append((server_ms, upper, mid, lumbar))

    def judge_rep(self, rep_number: int, start_ms: float, end_ms: float) -> tuple[SpineRepForm, CoachingDecision | None]:
        """Judge one counted rep (window on server time). Returns its form record and a warning to give, or None."""
        window = [s for s in self._samples if start_ms <= s[0] <= end_ms]
        values: dict[str, list[float]] = {
            name: [s[i] for s in window if s[i] is not None] for i, name in ((1, "upper"), (2, "mid"), (3, "lumbar"))
        }
        if len(window) < self.config.min_samples:
            form = SpineRepForm(rep_number, judged=False, samples=len(window))
            self.reps.append(form)
            return form, None

        issues = []
        for rule in self.rules:
            vals = values[rule.metric]
            if len(vals) < self.config.min_samples:
                continue
            beyond = sum(1 for v in vals if (v >= rule.threshold_deg if rule.direction == "above" else v <= rule.threshold_deg))
            if beyond / len(vals) >= rule.min_fraction:
                issues.append(rule.issue_code)

        lumbar = values["lumbar"]
        form = SpineRepForm(
            rep_number,
            judged=True,
            samples=len(window),
            issues=tuple(issues),
            lumbar_min_deg=_r(min(lumbar)) if lumbar else None,
            lumbar_max_deg=_r(max(lumbar)) if lumbar else None,
            lumbar_mean_deg=_r(statistics.fmean(lumbar)) if lumbar else None,
            mid_max_deg=_r(max(values["mid"])) if values["mid"] else None,
            upper_max_deg=_r(max(values["upper"])) if values["upper"] else None,
        )
        self.reps.append(form)
        return form, self._decide(rep_number)

    def _decide(self, rep_number: int) -> CoachingDecision | None:
        if not self.enabled:
            return None
        judged = [r for r in self.reps if r.judged]
        recent = judged[-self.config.consecutive :]
        if len(recent) < self.config.consecutive:
            return None
        for rule in self.rules:  # priority order = rule order
            code = rule.issue_code
            if all(code in r.issues for r in recent) and self._allowed(code, rep_number):
                self._last_message_rep = rep_number
                self._last_rep_by_issue[code] = rep_number
                decision = CoachingDecision(rep_number, code, MESSAGES[code])
                self.decisions.append(decision)
                return decision
        return None

    def _allowed(self, issue: str, rep_number: int) -> bool:
        cfg = self.config
        if len(self.decisions) >= cfg.max_messages:
            return False
        if self._last_message_rep is not None and rep_number - self._last_message_rep < cfg.min_reps_between_messages:
            return False
        last_same = self._last_rep_by_issue.get(issue)
        return last_same is None or rep_number - last_same >= cfg.same_issue_cooldown_reps
