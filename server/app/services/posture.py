"""Posture warnings from the upper-back IMU (``sensor_update`` pitch/roll). Deterministic; no LLM.

Where it sits:
    sensor_update -> PostureTracker (finds reps in the torso-pitch signal, summarizes each one)
                  -> detect_issues (rules over the rep history)
                  -> PostureCoach (rate limits; picks a templated message)
                  -> later: Gemini rewords the message from the same ``evidence``; the template stays
                     as the fallback when the model is slow, down, or returns something invalid.

It works from the IMU alone, so it does not need the camera rep counter.

Limits to keep in mind:
  * Mounting is an assumption (see app/sensor_simulation.py): IMU flat between the shoulder blades,
    pitch + = leaning forward, roll + = tilting to the user's right.
  * Absolute angles depend on how the strap sits. Set ``mounting_offset_deg`` to the pitch read while the user
    stands tall (a short calibration) before trusting the absolute checks (excessive_forward_lean,
    rounded_standing_posture). The relative checks (forward_lean_increasing) do not need it.
  * One IMU measures tilt, not spinal shape: it cannot tell a hip hinge from a rounded back. Messages describe
    tilt only and never diagnose.
  * Thresholds are untuned starting points chosen against SIMULATED data, not validated biomechanics.

Try it on the mock dataset (from ``server/``):
    python -m app.sensor_simulation --out mock_data/sensors
    python -m app.services.posture mock_data/sensors/lean_creep.jsonl
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

ACTION_SEND_FEEDBACK = "send_feedback"
CLOCK_RESET_MS = 5_000  # the device clock jumping back further than this = ESP32 rebooted

# Priority order: when several issues are found on the same rep, the first one is reported.
ISSUE_CODES = ("forward_lean_increasing", "excessive_forward_lean", "lateral_shift", "rounded_standing_posture")


@dataclass(frozen=True)
class PostureConfig:
    mounting_offset_deg: float = 0.0  # pitch read while standing tall; subtracted from every sample
    # finding reps in the pitch signal
    rep_start_deg: float = 12.0  # this far above standing pitch = a rep started
    rep_end_deg: float = 6.0  # back under this = standing again (hysteresis)
    min_rep_ms: int = 800  # shorter excursions are noise (a twitch, adjusting the strap)
    max_rep_ms: int = 8_000  # longer = not a rep: the user's standing posture changed; re-learn it
    max_gap_ms: int = 500  # a longer silence abandons the rep in progress
    standing_window: int = 50  # recent standing samples that define "your standing pitch" (~1 s at 50 Hz)
    standing_band_deg: float = 3.0  # only samples this close to it count, so rep edges don't drag it up
    min_standing_samples: int = 10
    # issues
    excessive_lean_deg: float = 55.0  # peak pitch from vertical
    lean_increase_deg: float = 8.0  # peak pitch above the median of the first baseline_reps
    lateral_shift_deg: float = 5.0  # mean roll at the bottom
    rounded_standing_deg: float = 15.0  # standing pitch from vertical
    baseline_reps: int = 3
    consecutive: int = 2  # an issue must show in this many reps in a row
    # rate limiting (same scheme as RuleCoach)
    min_reps_between_messages: int = 2
    same_issue_cooldown_reps: int = 4
    max_messages: int = 6


@dataclass(frozen=True)
class RepPosture:
    rep_number: int
    start_ms: int  # device clock; when pitch crossed the rep_start threshold
    peak_ms: int
    end_ms: int
    standing_pitch_deg: float  # before this rep
    peak_pitch_deg: float
    bottom_roll_deg: float | None  # mean roll near the bottom; None if roll was never sent

    @property
    def lean_deg(self) -> float:
        return self.peak_pitch_deg - self.standing_pitch_deg

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Finding:
    issue_code: str
    evidence: dict[str, Any]  # the structured numbers behind it; this is what the AI coach will receive


@dataclass(frozen=True)
class PostureDecision:
    rep_number: int
    issue_code: str
    message: str
    evidence: dict[str, Any]
    action: str = ACTION_SEND_FEEDBACK
    source: str = "rules"
    event_id: UUID = field(default_factory=uuid4)  # idempotent saves, like CoachingDecision


# ------------------------------------------------------------------ rep segmentation


class PostureTracker:
    """Turns a stream of (device time, pitch, roll) samples into one ``RepPosture`` per squat.

    Assumes the user is standing still when the stream starts (that defines the first standing pitch).
    """

    def __init__(self, config: PostureConfig | None = None) -> None:
        self.config = config or PostureConfig()
        self.reps: list[RepPosture] = []
        self.dropped_samples = 0  # duplicate or out-of-order timestamps
        self.aborted_reps = 0  # cut off by a gap or a clock reset, or too short to be a rep
        self._standing: deque[float] = deque(maxlen=self.config.standing_window)
        self._shifted: list[float] = []  # consecutive standing samples outside the band
        self._last_ts: int | None = None
        self._armed = True  # False after a gap until the user is seen standing again
        self._rep: list[tuple[int, float, float | None]] | None = None
        self._rep_standing = 0.0

    def standing_pitch(self) -> float | None:
        if len(self._standing) < self.config.min_standing_samples:
            return None
        return statistics.median(self._standing)

    def update(self, timestamp_ms: int, pitch_deg: float, roll_deg: float | None = None) -> RepPosture | None:
        """Feed one sample; returns the rep it completed, if any."""
        cfg = self.config
        if self._last_ts is not None:
            if timestamp_ms <= self._last_ts:
                if self._last_ts - timestamp_ms <= CLOCK_RESET_MS:
                    self.dropped_samples += 1
                    return None
                self._abandon()  # the device rebooted: its clock restarted; keep the standing pitch
            elif timestamp_ms - self._last_ts > cfg.max_gap_ms:
                self._abandon()
        self._last_ts = timestamp_ms
        pitch = pitch_deg - cfg.mounting_offset_deg

        if self._rep is None:
            standing = self.standing_pitch()
            if standing is not None and self._armed and pitch >= standing + cfg.rep_start_deg:
                self._rep = [(timestamp_ms, pitch, roll_deg)]
                self._rep_standing = standing
            elif standing is None or pitch < standing + cfg.rep_end_deg:
                self._armed = True
                # Track slow drift (a posture that sags over the set) but skip the start/end of reps.
                if standing is None or abs(pitch - standing) <= cfg.standing_band_deg:
                    self._standing.append(pitch)
                    self._shifted.clear()
                else:
                    self._shifted.append(pitch)
                    if len(self._shifted) >= cfg.standing_window:  # a new posture held for ~1 s: adopt it
                        self._standing = deque(self._shifted, maxlen=cfg.standing_window)
                        self._shifted.clear()
            return None

        self._rep.append((timestamp_ms, pitch, roll_deg))
        if timestamp_ms - self._rep[0][0] > cfg.max_rep_ms:
            self._relearn_standing([p for _, p, _ in self._rep])
            return None
        if pitch >= self._rep_standing + cfg.rep_end_deg:
            return None
        samples, self._rep = self._rep, None
        if samples[-1][0] - samples[0][0] < cfg.min_rep_ms:
            self.aborted_reps += 1
            return None
        rep = self._summarize(samples)
        self.reps.append(rep)
        return rep

    def _abandon(self) -> None:
        if self._rep is not None:
            self.aborted_reps += 1
            self._rep = None
        self._armed = False

    def _relearn_standing(self, pitches: list[float]) -> None:
        """A "rep" that never ended means the standing posture moved past the rep threshold. Standing is the
        most upright part of a squat cycle, so take a low percentile of what was seen as the new standing pitch."""
        cfg = self.config
        new = statistics.quantiles(pitches, n=20)[2]  # 15th percentile
        near = [p for p in pitches if abs(p - new) <= cfg.standing_band_deg][-cfg.standing_window :]
        self._standing = deque(near or [new], maxlen=cfg.standing_window)
        self._shifted.clear()
        self._rep = None
        self.aborted_reps += 1
        self._armed = False  # we may be mid-squat right now; wait until the user is seen standing

    def _summarize(self, samples: list[tuple[int, float, float | None]]) -> RepPosture:
        peak_ts, peak, _ = max(samples, key=lambda s: s[1])
        near_bottom = self._rep_standing + 0.8 * (peak - self._rep_standing)
        rolls = [r for _, p, r in samples if p >= near_bottom and r is not None]
        return RepPosture(
            rep_number=len(self.reps) + 1,
            start_ms=samples[0][0],
            peak_ms=peak_ts,
            end_ms=samples[-1][0],
            standing_pitch_deg=self._rep_standing,
            peak_pitch_deg=peak,
            bottom_roll_deg=statistics.fmean(rolls) if rolls else None,
        )


# ------------------------------------------------------------------ issue rules


def _r(x: float) -> float:
    return round(x, 1)


def detect_issues(reps: list[RepPosture], config: PostureConfig | None = None) -> list[Finding]:
    """Issues shown by the most recent reps, in priority order. Pure function of the rep history."""
    cfg = config or PostureConfig()
    recent = reps[-cfg.consecutive :]
    if len(recent) < cfg.consecutive:
        return []
    found: list[Finding] = []
    nums = [r.rep_number for r in recent]

    if len(reps) > cfg.baseline_reps and all(r.rep_number > cfg.baseline_reps for r in recent):
        baseline = statistics.median(r.peak_pitch_deg for r in reps[: cfg.baseline_reps])
        if all(r.peak_pitch_deg - baseline >= cfg.lean_increase_deg for r in recent):
            found.append(
                Finding(
                    "forward_lean_increasing",
                    {
                        "reps": nums,
                        "early_reps_peak_pitch_deg": _r(baseline),
                        "recent_peak_pitch_deg": [_r(r.peak_pitch_deg) for r in recent],
                        "recent_duration_ms": [r.duration_ms for r in recent],
                        "early_duration_ms": [r.duration_ms for r in reps[: cfg.baseline_reps]],
                        "threshold_increase_deg": cfg.lean_increase_deg,
                    },
                )
            )

    if all(r.peak_pitch_deg >= cfg.excessive_lean_deg for r in recent):
        found.append(
            Finding(
                "excessive_forward_lean",
                {"reps": nums, "peak_pitch_deg": [_r(r.peak_pitch_deg) for r in recent], "threshold_deg": cfg.excessive_lean_deg},
            )
        )

    rolls = [r.bottom_roll_deg for r in recent]
    if all(x is not None for x in rolls):
        if all(x >= cfg.lateral_shift_deg for x in rolls) or all(x <= -cfg.lateral_shift_deg for x in rolls):
            found.append(
                Finding(
                    "lateral_shift",
                    {
                        "reps": nums,
                        "bottom_roll_deg": [_r(x) for x in rolls],
                        "side": "right" if rolls[0] > 0 else "left",
                        "threshold_deg": cfg.lateral_shift_deg,
                    },
                )
            )

    if all(r.standing_pitch_deg >= cfg.rounded_standing_deg for r in recent):
        found.append(
            Finding(
                "rounded_standing_posture",
                {
                    "reps": nums,
                    "standing_pitch_deg": [_r(r.standing_pitch_deg) for r in recent],
                    "threshold_deg": cfg.rounded_standing_deg,
                    "calibrated": cfg.mounting_offset_deg != 0.0,
                },
            )
        )
    return found


# ------------------------------------------------------------------ messages (the fallback for the AI coach)

MESSAGES = {
    "forward_lean_increasing": (
        "You're leaning further forward than you did in your first reps. "
        "If you're getting tired, it's fine to take a short rest."
    ),
    "excessive_forward_lean": "Your upper back tips well forward at the bottom of your squats. Try keeping your chest a little taller.",
    "lateral_shift": (
        "Your shoulders tilt to the {side} at the bottom of your squats. Try to keep your weight even on both feet."
    ),
    "rounded_standing_posture": (
        "Your upper back is tilted forward while you stand between reps. Try standing tall before the next rep."
    ),
}


def render_message(finding: Finding) -> str:
    return MESSAGES[finding.issue_code].format(side=finding.evidence.get("side", ""))


class PostureCoach:
    """Feeds the tracker and turns findings into rare warnings."""

    def __init__(self, config: PostureConfig | None = None) -> None:
        self.config = config or PostureConfig()
        self.tracker = PostureTracker(self.config)
        self.enabled = True  # set False after a pain report, like RuleCoach
        self.findings: dict[int, list[Finding]] = {}  # every issue seen, per rep, before rate limiting
        self.decisions: list[PostureDecision] = []
        self._last_message_rep: int | None = None
        self._last_rep_by_issue: dict[str, int] = {}

    def update(self, timestamp_ms: int, pitch_deg: float, roll_deg: float | None = None) -> PostureDecision | None:
        rep = self.tracker.update(timestamp_ms, pitch_deg, roll_deg)
        if rep is None:
            return None
        findings = detect_issues(self.tracker.reps, self.config)
        self.findings[rep.rep_number] = findings
        if not self.enabled:
            return None
        for f in findings:
            if self._allowed(f.issue_code, rep.rep_number):
                decision = PostureDecision(rep.rep_number, f.issue_code, render_message(f), f.evidence)
                self.decisions.append(decision)
                self._last_message_rep = rep.rep_number
                self._last_rep_by_issue[f.issue_code] = rep.rep_number
                return decision
        return None

    def feed(self, message: dict[str, Any]) -> PostureDecision | None:
        """Feed a ``sensor_update`` message (as a dict). Messages without pitch are ignored."""
        data = message.get("data") or {}
        pitch = data.get("imu_pitch_deg")
        if message.get("type") != "sensor_update" or pitch is None:
            return None
        return self.update(int(message["timestamp_ms"]), float(pitch), data.get("imu_roll_deg"))

    def _allowed(self, issue: str, rep_number: int) -> bool:
        cfg = self.config
        if len(self.decisions) >= cfg.max_messages:
            return False
        if self._last_message_rep is not None and rep_number - self._last_message_rep < cfg.min_reps_between_messages:
            return False
        last_same = self._last_rep_by_issue.get(issue)
        return last_same is None or rep_number - last_same >= cfg.same_issue_cooldown_reps


def analyze(messages: list[dict[str, Any]], config: PostureConfig | None = None) -> PostureCoach:
    """Replay a recorded stream (e.g. a mock .jsonl) and return the coach with its reps, findings and decisions."""
    coach = PostureCoach(config)
    for m in messages:
        coach.feed(m)
    return coach


if __name__ == "__main__":  # pragma: no cover - manual tool
    import argparse
    import json
    from pathlib import Path

    p = argparse.ArgumentParser(description="Replay a sensor_update .jsonl file through the posture coach.")
    p.add_argument("path", type=Path)
    p.add_argument("--mounting-offset", type=float, default=0.0, help="pitch read while standing tall (deg)")
    args = p.parse_args()

    msgs = [json.loads(line) for line in args.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    coach = analyze(msgs, PostureConfig(mounting_offset_deg=args.mounting_offset))
    t = coach.tracker
    print(f"{len(msgs)} messages, {len(t.reps)} reps, {t.aborted_reps} aborted, {t.dropped_samples} dropped samples")
    for r in t.reps:
        roll = "-" if r.bottom_roll_deg is None else f"{r.bottom_roll_deg:5.1f}"
        issues = ", ".join(f.issue_code for f in coach.findings.get(r.rep_number, [])) or "-"
        print(
            f"rep {r.rep_number}: standing {r.standing_pitch_deg:5.1f}  peak {r.peak_pitch_deg:5.1f}  "
            f"roll {roll}  {r.duration_ms} ms  issues: {issues}"
        )
    for d in coach.decisions:
        print(f"WARNING after rep {d.rep_number} [{d.issue_code}]: {d.message}")
        print(f"    evidence: {json.dumps(d.evidence)}")
