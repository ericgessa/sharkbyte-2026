"""Workout statistics from saved repetitions. Pure functions; no I/O."""

from __future__ import annotations

from statistics import fmean
from typing import Any

MIN_REPS_FOR_TREND = 4  # need a few reps before "later vs earlier" means anything
TREND_WINDOW = 3


def compute_stats(reps: list[dict[str, Any]]) -> dict[str, Any]:
    """``reps`` are repetition rows ordered by rep_number, each with a ``metrics`` dict.

    Knee angle: smaller = deeper. ``depth_change_deg`` is (average of the last 3 reps)
    minus (average of the first 3): positive means later reps were shallower.
    Fields are None when there is nothing to compute them from.
    """
    metrics = [r.get("metrics") or {} for r in reps]
    angles = [m["min_knee_angle_deg"] for m in metrics if m.get("min_knee_angle_deg") is not None]
    durations = [m["duration_ms"] for m in metrics if m.get("duration_ms") is not None]
    asym = [m["max_asymmetry_deg"] for m in metrics if m.get("max_asymmetry_deg") is not None]

    depth_change = None
    if len(angles) >= MIN_REPS_FOR_TREND:
        depth_change = fmean(angles[-TREND_WINDOW:]) - fmean(angles[:TREND_WINDOW])

    def r1(x: float | None) -> float | None:
        return None if x is None else round(x, 1)

    return {
        "rep_count": len(reps),
        "avg_min_knee_angle_deg": r1(fmean(angles)) if angles else None,
        "deepest_knee_angle_deg": r1(min(angles)) if angles else None,
        "shallowest_knee_angle_deg": r1(max(angles)) if angles else None,
        "depth_change_deg": r1(depth_change),
        "avg_rep_duration_ms": round(fmean(durations)) if durations else None,
        "max_asymmetry_deg": r1(max(asym)) if asym else None,
    }
