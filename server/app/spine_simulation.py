"""SIMULATED spine-wearable section angles (upper / mid / lumbar), for tests of services/spine_form.py.

PROVISIONAL: the shapes come from the hardware README's descriptions (+ = bending forward; rounding the lower back
raises ``lumbar``; a flat back keeps the sections near 0) and one short push-up log, not from labelled recordings.
Replace the scenario numbers with values measured on real labelled sets (to-do task 5) before tuning thresholds on them.
Nothing here is real sensor output.

Each scenario yields samples ``(t_ms, upper, mid, lumbar)`` at ``rate_hz`` on a server-time axis starting at 0, plus
the rep windows ``(start_ms, end_ms)`` a camera counter would report for the same reps.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class SpineScenario:
    exercise: str
    description: str
    expected_issues: tuple[str, ...]
    lumbar_peak_deg: float  # reached at the bottom of each rep (sign = direction of the bend)
    lumbar_rest_deg: float = 0.0
    upper_peak_deg: float = 2.0
    mid_peak_deg: float = 1.5
    hold_fraction: float = 0.6  # share of the rep spent near the peak (a held shape, not a spike)


SPINE_SCENARIOS: dict[str, SpineScenario] = {
    "pushup_good": SpineScenario("push-up", "Flat back: sections stay within a few degrees.", (), 2.0),
    "pushup_hips_sagging": SpineScenario("push-up", "Lower back arches about 12 deg: hips drop.", ("hips_sagging",), -12.0),
    "pushup_hips_piking": SpineScenario("push-up", "Lower back flexes about 14 deg: hips rise.", ("hips_piking",), 14.0),
    "squat_good": SpineScenario("squat", "Lower back bends a little at the bottom (about 6 deg).", (), 6.0),
    "squat_lower_back_rounding": SpineScenario(
        "squat", "Lower back rounds about 22 deg at the bottom of each rep.", ("lower_back_rounding",), 22.0
    ),
}


def generate_spine(
    name: str, *, reps: int = 5, rep_ms: int = 2000, rest_ms: int = 1000, rate_hz: int = 20, seed: int = 0
) -> tuple[list[tuple[int, float, float, float]], list[tuple[int, int]]]:
    """Samples and rep windows for one scenario. Deterministic per seed."""
    sc = SPINE_SCENARIOS[name]
    rng = random.Random(f"{name}:{seed}")
    period = 1000 / rate_hz
    windows: list[tuple[int, int]] = []
    t = rest_ms
    for _ in range(reps):
        windows.append((t, t + rep_ms))
        t += rep_ms + rest_ms
    samples = []
    for k in range(int(t / period) + 1):
        now = k * period
        shape = 0.0
        for start, end in windows:
            if start <= now <= end:
                phase = (now - start) / (end - start)
                # Rises over the first part of the rep, holds near the peak, returns at the end.
                ramp = (1 - sc.hold_fraction) / 2
                if phase < ramp:
                    shape = 0.5 * (1 - math.cos(math.pi * phase / ramp))
                elif phase > 1 - ramp:
                    shape = 0.5 * (1 - math.cos(math.pi * (1 - phase) / ramp))
                else:
                    shape = 1.0
        noise = lambda: rng.gauss(0, 0.6)  # noqa: E731
        lumbar = sc.lumbar_rest_deg + (sc.lumbar_peak_deg - sc.lumbar_rest_deg) * shape + noise()
        samples.append(
            (round(now), round(sc.upper_peak_deg * shape + noise(), 2), round(sc.mid_peak_deg * shape + noise(), 2), round(lumbar, 2))
        )
    return samples, windows
