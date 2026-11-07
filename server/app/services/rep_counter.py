"""Deterministic squat repetition counter.

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

The default thresholds are detection thresholds, not a definition of a
"correct" squat. They are meant to be overridden per exercise/user
(``exercises.config``) once calibration exists.
"""

from __future__ import annotations

import logging
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
    min_confidence: float = 0.6
    standing_deg: float = 160.0  # at/above: standing
    descent_start_deg: float = 150.0  # below: left standing (10 deg hysteresis)
    bottom_deg: float = 110.0  # at/below: depth reached
    bottom_exit_deg: float = 120.0  # above: left the bottom (10 deg hysteresis)
    min_rep_ms: int = 800  # faster than this is treated as noise
    max_gap_ms: int = 2000  # longer silence disarms the counter


@dataclass(frozen=True)
class PoseSample:
    sequence: int
    timestamp_ms: int
    confidence: float
    left_knee_deg: float | None = None
    right_knee_deg: float | None = None


@dataclass(frozen=True)
class RepMetrics:
    rep_number: int
    min_knee_angle_deg: float
    descent_ms: int
    ascent_ms: int
    duration_ms: int
    max_asymmetry_deg: float | None  # None if both knees were never visible together


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
        self._last_ts = sample.timestamp_ms

        angle = self._knee_angle(sample)
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

    @staticmethod
    def _knee_angle(sample: PoseSample) -> float | None:
        angles = [a for a in (sample.left_knee_deg, sample.right_knee_deg) if a is not None]
        return sum(angles) / len(angles) if angles else None

    def _advance(self, angle: float, sample: PoseSample) -> RepMetrics | None:
        cfg = self.config
        ts = sample.timestamp_ms

        if self.phase is SquatPhase.UNARMED:
            if angle >= cfg.standing_deg:
                self.phase = SquatPhase.STANDING
            return None

        if self.phase is SquatPhase.STANDING:
            if angle < cfg.descent_start_deg:
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
            elif angle >= cfg.standing_deg:
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
            elif angle >= cfg.standing_deg:
                return self._complete(ts)
        return None

    def _track(self, sample: PoseSample, angle: float) -> None:
        rep = self._rep
        assert rep is not None
        rep.min_angle = min(rep.min_angle, angle)
        if sample.left_knee_deg is not None and sample.right_knee_deg is not None:
            diff = abs(sample.left_knee_deg - sample.right_knee_deg)
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
            min_knee_angle_deg=round(rep.min_angle, 1),
            descent_ms=rep.bottom_ts - rep.start_ts,
            ascent_ms=ts - rep.ascent_start_ts,
            duration_ms=duration,
            max_asymmetry_deg=None if rep.max_asymmetry is None else round(rep.max_asymmetry, 1),
        )
        logger.info("rep %d completed: %s", self.rep_count, metrics)
        return metrics
