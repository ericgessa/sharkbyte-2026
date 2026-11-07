"""Deterministic repetition counter (squat on the knee angle, push-up on the elbow angle).

A small state machine driven by knee-angle samples. No I/O, no clock reads, no
randomness: the same sample stream always yields the same reps, which makes it
easy to unit test and safe to run inside the WebSocket receive loop.

Phases:  UNARMED -> STANDING -> DESCENDING -> BOTTOM -> ASCENDING -> STANDING

* UNARMED: nothing counts until a clearly standing frame is seen. This stops a
  session that starts mid-squat (or resumes after a tracking gap) from
  producing a bogus rep with wrong metrics.
* Hysteresis: leaving STANDING needs a lower angle than re-entering it, and
  leaving BOTTOM needs a higher angle than entering it, so jitter near a
  threshold cannot flip the phase back and forth.
* A rep only counts if the bottom threshold was reached and the whole rep took
  at least ``min_rep_ms``.

The same state machine serves every "angle goes down, then back up" exercise: ``SquatConfig`` reads the knee angle,
``PushUpConfig`` reads the elbow angle (see ``joint``), each with its own thresholds. The default thresholds are detection thresholds, not a definition of a
"correct" squat. They are meant to be overridden per exercise/user
(``exercises.config``) once calibration exists.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class SquatPhase(str, Enum):
    UNARMED = "unarmed"
    STANDING = "standing"
    DESCENDING = "descending"
    BOTTOM = "bottom"
    ASCENDING = "ascending"


class DropReason(str, Enum):
    LOW_CONFIDENCE = "low_confidence"
    OUT_OF_ORDER = "out_of_order"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class SquatConfig:
    joint: str = "knee"  # which angle drives the counter: "knee" or "elbow"
    min_confidence: float = 0.6
    standing_deg: float = 160.0  # at/above: standing
    descent_start_deg: float = 150.0  # below: left standing (10 deg hysteresis)
    bottom_deg: float = 110.0  # at/below: depth reached
    bottom_exit_deg: float = 120.0  # above: left the bottom (10 deg hysteresis)
    min_rep_ms: int = 800  # faster than this is treated as noise
    max_gap_ms: int = 2000  # longer silence disarms the counter
    # Personal standing angle. Not everyone reads >= standing_deg when upright (camera angle, slightly bent knees).
    # Without this the counter would stay unarmed and count nothing, silently. If the knee angle sits still
    # (spread <= adapt_std_deg, as a standard deviation, over adapt_frames frames) at or above adapt_floor_deg, THAT becomes standing, minus
    # adapt_margin_deg. The thresholds are only ever lowered, never raised above the values above, so a person who
    # locks out normally gets exactly the behaviour above.
    adaptive_standing: bool = True
    adapt_floor_deg: float = 145.0
    adapt_frames: int = 10  # about a third of a second at 30 fps
    adapt_std_deg: float = 3.5  # camera jitter is a few degrees; real movement is far more
    adapt_margin_deg: float = 4.0


@dataclass(frozen=True)
class PushUpConfig(SquatConfig):
    """Push-up: the elbow angle is ~straight at the top and bends to about 90 degrees at the bottom.

    UNTUNED guesses, like the squat thresholds: they have only been exercised with simulated data. A little more
    lenient on depth than a strict 90 degrees because the camera sees the elbow from the side and reads it loosely.
    """

    joint: str = "elbow"
    standing_deg: float = 155.0  # "standing" here means arms extended at the top
    descent_start_deg: float = 145.0
    bottom_deg: float = 100.0
    bottom_exit_deg: float = 110.0
    min_rep_ms: int = 600  # push-ups are quicker than squats
    adapt_floor_deg: float = 135.0


@dataclass(frozen=True)
class PoseSample:
    sequence: int
    timestamp_ms: int
    confidence: float
    left_knee_deg: float | None = None
    right_knee_deg: float | None = None
    left_elbow_deg: float | None = None
    right_elbow_deg: float | None = None

    def pair(self, joint: str) -> tuple[float | None, float | None]:
        """(left, right) angle of the joint that drives the counter."""
        if joint == "elbow":
            return self.left_elbow_deg, self.right_elbow_deg
        return self.left_knee_deg, self.right_knee_deg


@dataclass(frozen=True)
class RepMetrics:
    rep_number: int
    min_angle_deg: float  # the deepest the counted joint got (knee for squats, elbow for push-ups)
    descent_ms: int
    ascent_ms: int
    duration_ms: int
    max_asymmetry_deg: float | None  # None if both sides were never visible together
    # The pose stream's own clock (ms). Lets time-series analytics slice a session into reps.
    start_ts_ms: int = 0
    end_ts_ms: int = 0
    joint: str = "knee"

    @property
    def min_knee_angle_deg(self) -> float:
        """Kept for knee exercises (the original field name). For other joints use ``min_angle_deg``."""
        return self.min_angle_deg


@dataclass(frozen=True)
class StepResult:
    rep: RepMetrics | None = None
    dropped: DropReason | None = None
    tracking_changed: bool | None = None  # True = tracking lost, False = recovered
    phase: SquatPhase = SquatPhase.UNARMED


@dataclass
class _RepInProgress:
    start_ts: int
    bottom_ts: int | None = None
    ascent_start_ts: int | None = None
    min_angle: float = 180.0
    max_asymmetry: float | None = None


@dataclass
class NoopRepCounter:
    """Stands in for exercises that have no rep analysis yet (see services/exercises.py).

    It never counts a rep, never reports tracking changes and never raises, so the live pipeline can treat
    every session the same way while only analyzed exercises produce reps and coaching.
    """

    rep_count: int = 0
    phase: SquatPhase = SquatPhase.UNARMED
    low_confidence: bool = False
    dropped_frames: int = 0
    aborted_attempts: int = 0

    def reset_tracking(self) -> None:
        pass

    def update(self, sample: PoseSample) -> StepResult:
        return StepResult()


@dataclass
class SquatRepCounter:
    config: SquatConfig = field(default_factory=SquatConfig)
    rep_count: int = 0
    phase: SquatPhase = SquatPhase.UNARMED
    low_confidence: bool = False
    dropped_frames: int = 0
    aborted_attempts: int = 0
    _last_sequence: int | None = field(default=None, repr=False)
    _last_ts: int | None = field(default=None, repr=False)
    _rep: _RepInProgress | None = field(default=None, repr=False)
    # Effective standing thresholds: the configured ones, lowered once if this person stands below them.
    _stand_deg: float = field(default=0.0, repr=False)
    _descent_deg: float = field(default=0.0, repr=False)
    _settled: deque = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self._stand_deg = self.config.standing_deg
        self._descent_deg = self.config.descent_start_deg
        self._settled = deque(maxlen=self.config.adapt_frames)

    @property
    def standing_threshold_deg(self) -> float:
        """The knee angle currently treated as standing (the configured value unless this person stands lower)."""
        return self._stand_deg

    def reset_tracking(self) -> None:
        """Forget any rep in progress and wait for a standing frame (e.g. after a pause).

        Counted reps and the sequence/timestamp ordering are kept.
        """
        self._rep = None
        self.phase = SquatPhase.UNARMED
        self._settled.clear()
        self._last_ts = None  # the pause may have lasted longer than max_gap_ms

    def update(self, sample: PoseSample) -> StepResult:
        cfg = self.config

        # --- frame filtering -------------------------------------------------
        if self._last_sequence is not None and sample.sequence <= self._last_sequence:
            reason = (
                DropReason.DUPLICATE
                if sample.sequence == self._last_sequence
                else DropReason.OUT_OF_ORDER
            )
            return self._drop(reason, sample)

        self._last_sequence = sample.sequence

        if sample.confidence < cfg.min_confidence:
            changed = None if self.low_confidence else True
            self.low_confidence = True
            return self._drop(DropReason.LOW_CONFIDENCE, sample, tracking_changed=changed)

        changed = False if self.low_confidence else None
        self.low_confidence = False

        if self._last_ts is not None and sample.timestamp_ms < self._last_ts:
            return self._drop(DropReason.OUT_OF_ORDER, sample, tracking_changed=changed)

        # --- gap handling ----------------------------------------------------
        if self._last_ts is not None and sample.timestamp_ms - self._last_ts > cfg.max_gap_ms:
            if self._rep is not None:
                self.aborted_attempts += 1
                logger.info("rep aborted: %d ms gap in tracking", sample.timestamp_ms - self._last_ts)
            self._rep = None
            self.phase = SquatPhase.UNARMED
            self._settled.clear()
        self._last_ts = sample.timestamp_ms

        angle = self._angle(sample)
        if angle is None:
            return StepResult(phase=self.phase, tracking_changed=changed)

        rep = self._advance(angle, sample)
        return StepResult(rep=rep, phase=self.phase, tracking_changed=changed)

    # ------------------------------------------------------------------ internals

    def _drop(
        self, reason: DropReason, sample: PoseSample, tracking_changed: bool | None = None
    ) -> StepResult:
        self.dropped_frames += 1
        logger.debug("dropped frame seq=%s reason=%s", sample.sequence, reason.value)
        return StepResult(dropped=reason, tracking_changed=tracking_changed, phase=self.phase)

    def _angle(self, sample: PoseSample) -> float | None:
        angles = [a for a in sample.pair(self.config.joint) if a is not None]
        return sum(angles) / len(angles) if angles else None

    def _advance(self, angle: float, sample: PoseSample) -> RepMetrics | None:
        cfg = self.config
        ts = sample.timestamp_ms

        if cfg.adaptive_standing:
            # In EVERY phase, not just while unarmed: with a resting angle just under the default threshold, camera
            # jitter alone can arm the counter and start phantom descents, so a quiet moment must be able to
            # lower the threshold whatever phase the noise has put us in.
            self._adapt_standing(angle)

        if self.phase is SquatPhase.UNARMED:
            if angle >= self._stand_deg:
                self.phase = SquatPhase.STANDING
            return None

        if self.phase is SquatPhase.STANDING:
            if angle < self._descent_deg:
                self.phase = SquatPhase.DESCENDING
                self._rep = _RepInProgress(start_ts=ts, min_angle=angle)
                self._track(sample, angle)
            return None

        rep = self._rep
        assert rep is not None  # every non-standing, armed phase has a rep in progress
        self._track(sample, angle)

        if self.phase is SquatPhase.DESCENDING:
            if angle <= cfg.bottom_deg:
                self.phase = SquatPhase.BOTTOM
                rep.bottom_ts = ts
            elif angle >= self._stand_deg:
                self._abort()
        elif self.phase is SquatPhase.BOTTOM:
            if angle > cfg.bottom_exit_deg:
                self.phase = SquatPhase.ASCENDING
                rep.ascent_start_ts = ts
        elif self.phase is SquatPhase.ASCENDING:
            if angle <= cfg.bottom_deg:
                # Bounce at the bottom: still the same rep.
                self.phase = SquatPhase.BOTTOM
                rep.ascent_start_ts = None
            elif angle >= self._stand_deg:
                return self._complete(ts)
        return None

    def _adapt_standing(self, angle: float) -> None:
        """Lower the standing threshold (and arm) if the knees have been still and fairly straight for a moment."""
        cfg = self.config
        self._settled.append(angle)
        window = self._settled
        if len(window) < window.maxlen:
            return
        level = statistics.median(window)
        if level < cfg.adapt_floor_deg or statistics.pstdev(window) > cfg.adapt_std_deg:
            return
        stand = max(cfg.adapt_floor_deg, min(cfg.standing_deg, level - cfg.adapt_margin_deg))
        if stand >= self._stand_deg:
            return  # only ever lower the threshold
        self._stand_deg = stand
        self._descent_deg = stand - (cfg.standing_deg - cfg.descent_start_deg)  # keep the same hysteresis gap
        if self.phase is SquatPhase.UNARMED:
            self.phase = SquatPhase.STANDING
        logger.info("standing angle adapted to %.1f deg (configured %.1f)", stand, cfg.standing_deg)

    def _track(self, sample: PoseSample, angle: float) -> None:
        rep = self._rep
        assert rep is not None
        rep.min_angle = min(rep.min_angle, angle)
        left, right = sample.pair(self.config.joint)
        if left is not None and right is not None:
            diff = abs(left - right)
            rep.max_asymmetry = diff if rep.max_asymmetry is None else max(rep.max_asymmetry, diff)

    def _abort(self) -> None:
        self.aborted_attempts += 1
        self._rep = None
        self.phase = SquatPhase.STANDING

    def _complete(self, ts: int) -> RepMetrics | None:
        rep = self._rep
        assert rep is not None and rep.bottom_ts is not None and rep.ascent_start_ts is not None
        duration = ts - rep.start_ts
        self._rep = None
        self.phase = SquatPhase.STANDING

        if duration < self.config.min_rep_ms:
            self.aborted_attempts += 1
            logger.info("rep discarded: %d ms is faster than min_rep_ms", duration)
            return None

        self.rep_count += 1
        metrics = RepMetrics(
            rep_number=self.rep_count,
            min_angle_deg=round(rep.min_angle, 1),
            descent_ms=rep.bottom_ts - rep.start_ts,
            ascent_ms=ts - rep.ascent_start_ts,
            duration_ms=duration,
            max_asymmetry_deg=None if rep.max_asymmetry is None else round(rep.max_asymmetry, 1),
            start_ts_ms=rep.start_ts,
            end_ts_ms=ts,
            joint=self.config.joint,
        )
        logger.info("rep %d completed: %s", self.rep_count, metrics)
        return metrics


@dataclass
class PushUpRepCounter(SquatRepCounter):
    """Counts push-ups from the elbow angle. Same machine as the squat counter, push-up thresholds."""

    config: SquatConfig = field(default_factory=PushUpConfig)
