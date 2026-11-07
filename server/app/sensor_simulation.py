"""SIMULATED ESP32 sensor data: a labelled mock dataset for building posture detection and coaching.

Nothing here is real hardware output. Each scenario injects a known posture pattern so detection logic
(and later the AI coach) can be tested against a known answer.

Mounting model (an ASSUMPTION until the hardware team confirms it):
  * One ESP32 + IMU strapped flat on the UPPER back, between the shoulder blades.
  * ``imu_pitch_deg``: forward lean of the upper torso from vertical (+ = leaning forward). It mixes hip
    hinge and upper-back rounding; a single IMU cannot tell those apart.
  * ``imu_roll_deg``: sideways tilt (+ = tilting toward the user's right).
  * ``accel_*_g``: device frame, z up along the spine, x out of the chest, y toward the user's left.
    At rest the reading is about (-sin pitch, sin roll * cos pitch, cos roll * cos pitch) g; squatting adds a
    small vertical acceleration.
  * No flex sensor and no yaw: where a flex strip goes is undecided, and yaw drifts without a magnetometer.
    Add them here once the hardware is known.

Angles are illustrative values, not measurements, and the issue codes in the labels are proposals that are not
part of docs/event-contracts.md.

Run from ``server/``:
    python -m app.sensor_simulation --out mock_data/sensors                      # every scenario
    python -m app.sensor_simulation --out mock_data/sensors --scenario lean_creep --reps 8

Each scenario writes ``<name>.jsonl`` (one ``sensor_update`` per line, valid against
``app.schemas.sensors.SensorUpdate``) and ``<name>.labels.json`` (ground truth per rep, device-clock ms).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SESSION_ID = "00000000-0000-4000-8000-000000000001"  # placeholder; replace with a real session id
DEFAULT_DEVICE_ID = "esp32-01"

REST_MS = 1000  # standing still before the first rep and after every rep
DROPOUT_MS = 1500  # length of the Bluetooth gap in the sensor_dropout scenario
PITCH_NOISE_DEG = 0.5
ROLL_NOISE_DEG = 0.4
ACCEL_NOISE_G = 0.02
SQUAT_ACCEL_G = 0.12  # peak vertical acceleration of a 2.4 s squat; grows with speed

ASSUMPTIONS = {
    "placement": "one ESP32 + IMU flat on the upper back, between the shoulder blades",
    "imu_pitch_deg": "forward lean of the upper torso from vertical, + = forward (hip hinge and rounding combined)",
    "imu_roll_deg": "sideways tilt, + = toward the user's right",
    "accel_g": "device frame: z up the spine, x out of the chest, y toward the user's left; 1 g at rest",
    "not_simulated": "flex sensor, yaw, sensor calibration, real noise characteristics",
}


@dataclass(frozen=True)
class Scenario:
    description: str
    expected_issues: tuple[str, ...]  # what a correct posture detector should report for this recording
    standing_pitch_deg: float = 5.0  # between reps
    bottom_pitch_deg: tuple[float, float] = (40.0, 40.0)  # at the bottom of the first rep -> of the last rep
    bottom_roll_deg: float = 0.0
    rep_ms: tuple[int, int] = (2400, 2400)  # first rep -> last rep
    dropout: bool = False


SCENARIOS: dict[str, Scenario] = {
    "good_form": Scenario(
        "Upright between reps, moderate forward lean at the bottom (about 40 deg), level shoulders.",
        expected_issues=(),
    ),
    "forward_lean": Scenario(
        "Chest drops well forward at the bottom of every rep (about 62 deg).",
        expected_issues=("excessive_forward_lean",),
        bottom_pitch_deg=(62.0, 62.0),
    ),
    "lean_creep": Scenario(
        "Fatigue: each rep leans further forward (40 -> 60 deg) and gets slower (2.4 -> 3.1 s).",
        expected_issues=("forward_lean_increasing",),
        bottom_pitch_deg=(40.0, 60.0),
        rep_ms=(2400, 3100),
    ),
    "lateral_shift": Scenario(
        "Good lean, but the shoulders tilt about 8 deg to the right at the bottom of every rep.",
        expected_issues=("lateral_shift",),
        bottom_roll_deg=8.0,
    ),
    "slouched_standing": Scenario(
        "Rounded upper back while standing between reps (about 20 deg forward), normal squat depth.",
        expected_issues=("rounded_standing_posture",),
        standing_pitch_deg=20.0,
        bottom_pitch_deg=(45.0, 45.0),
    ),
    "sensor_dropout": Scenario(
        "Good form, but the Bluetooth link drops for 1.5 s around the bottom of rep 3. A detector should "
        "report no posture issue and must not invent one from the gap.",
        expected_issues=(),
        dropout=True,
    ),
}


@dataclass
class MockRecording:
    messages: list[dict[str, Any]]
    labels: dict[str, Any]


def _lerp(first: float, last: float, i: int, n: int) -> float:
    return first if n <= 1 else first + (last - first) * i / (n - 1)


def _depth(phase: float) -> float:
    """0 standing -> 1 at the bottom (phase 0.5) -> 0 standing. Same curve as the pose simulation."""
    return 0.5 * (1 - math.cos(2 * math.pi * phase))


def generate(
    name: str,
    *,
    reps: int = 5,
    rate_hz: int = 50,
    seed: int = 0,
    session_id: str = DEFAULT_SESSION_ID,
    device_id: str = DEFAULT_DEVICE_ID,
) -> MockRecording:
    """Build one scenario's ``sensor_update`` messages and their ground-truth labels. Deterministic per seed."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; choose from: {', '.join(SCENARIOS)}")
    if reps < 1:
        raise ValueError("reps must be at least 1")
    if not 1 <= rate_hz <= 200:
        raise ValueError("rate_hz must be between 1 and 200")

    sc = SCENARIOS[name]
    rng = random.Random(f"{name}:{seed}")
    clock0 = rng.randint(10_000, 5_000_000)  # the ESP32's own clock (uptime ms), unrelated to the phone's

    # Timeline in ms from the start of the recording: rest, rep, rest, rep, ..., rest.
    windows: list[tuple[float, float, float]] = []  # (start, end, bottom pitch)
    t = float(REST_MS)
    for i in range(reps):
        duration = round(_lerp(*sc.rep_ms, i, reps) * rng.uniform(0.95, 1.05))
        windows.append((t, t + duration, _lerp(*sc.bottom_pitch_deg, i, reps)))
        t += duration + REST_MS
    total_ms = t

    dropout: tuple[float, float] | None = None
    if sc.dropout:
        start, end, _ = windows[min(2, reps - 1)]
        mid = (start + end) / 2
        dropout = (mid - DROPOUT_MS / 2, mid + DROPOUT_MS / 2)

    period = 1000 / rate_hz
    messages: list[dict[str, Any]] = []
    sequence = missing = rep_idx = 0
    for k in range(int(total_ms / period) + 1):
        t_ms = k * period
        while rep_idx < len(windows) and t_ms >= windows[rep_idx][1]:
            rep_idx += 1

        pitch, roll, a_vert = sc.standing_pitch_deg, 0.0, 0.0
        if rep_idx < len(windows) and t_ms >= windows[rep_idx][0]:
            start, end, bottom = windows[rep_idx]
            phase = (t_ms - start) / (end - start)
            d = _depth(phase)
            pitch += (bottom - sc.standing_pitch_deg) * d
            roll = sc.bottom_roll_deg * d
            a_vert = -SQUAT_ACCEL_G * (2400 / (end - start)) ** 2 * math.cos(2 * math.pi * phase)

        if dropout and dropout[0] <= t_ms < dropout[1]:
            sequence += 1  # the device keeps counting; the packets just never arrive
            missing += 1
            continue

        pitch += rng.gauss(0, PITCH_NOISE_DEG)
        roll += rng.gauss(0, ROLL_NOISE_DEG)
        th, ph = math.radians(pitch), math.radians(roll)
        g = 1 + a_vert
        messages.append(
            {
                "schema_version": 1,
                "type": "sensor_update",
                "session_id": session_id,
                "device_id": device_id,
                "sequence": sequence,
                "timestamp_ms": clock0 + round(t_ms + rng.uniform(-1, 1)),
                "data": {
                    "imu_pitch_deg": round(pitch, 2),
                    "imu_roll_deg": round(roll, 2),
                    "accel_x_g": round(-math.sin(th) * g + rng.gauss(0, ACCEL_NOISE_G), 3),
                    "accel_y_g": round(math.sin(ph) * math.cos(th) * g + rng.gauss(0, ACCEL_NOISE_G), 3),
                    "accel_z_g": round(math.cos(ph) * math.cos(th) * g + rng.gauss(0, ACCEL_NOISE_G), 3),
                },
            }
        )
        sequence += 1

    labels = {
        "simulated": True,
        "scenario": name,
        "description": sc.description,
        "expected_issues": list(sc.expected_issues),
        "assumptions": ASSUMPTIONS,
        "generator": {"seed": seed, "reps": reps, "rate_hz": rate_hz, "session_id": session_id, "device_id": device_id},
        "message_count": len(messages),
        "times": "device clock ms, the same clock as each message's timestamp_ms",
        "reps": [
            {
                "rep_number": i + 1,
                "start_ms": clock0 + round(start),
                "bottom_ms": clock0 + round((start + end) / 2),
                "end_ms": clock0 + round(end),
                "standing_pitch_deg": sc.standing_pitch_deg,
                "bottom_pitch_deg": round(bottom, 2),
                "bottom_roll_deg": sc.bottom_roll_deg,
            }
            for i, (start, end, bottom) in enumerate(windows)
        ],
        "dropouts": (
            [{"start_ms": clock0 + round(dropout[0]), "end_ms": clock0 + round(dropout[1]), "missing_messages": missing}]
            if dropout
            else []
        ),
    }
    return MockRecording(messages, labels)


def write_dataset(out_dir: Path | str, names: list[str] | None = None, **kwargs: Any) -> list[Path]:
    """Write ``<name>.jsonl`` and ``<name>.labels.json`` for each scenario (all by default)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in names or list(SCENARIOS):
        rec = generate(name, **kwargs)
        data_path, labels_path = out / f"{name}.jsonl", out / f"{name}.labels.json"
        data_path.write_text("".join(json.dumps(m, separators=(",", ":")) + "\n" for m in rec.messages), encoding="utf-8")
        labels_path.write_text(json.dumps(rec.labels, indent=2) + "\n", encoding="utf-8")
        written += [data_path, labels_path]
    return written


if __name__ == "__main__":  # pragma: no cover - manual tool
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="mock_data/sensors", help="output directory (default: mock_data/sensors)")
    p.add_argument("--scenario", action="append", choices=list(SCENARIOS), help="repeat to pick several (default: all)")
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--rate", type=int, default=50, help="samples per second (default 50)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    for path in write_dataset(args.out, args.scenario, reps=args.reps, rate_hz=args.rate, seed=args.seed):
        print(f"wrote {path} (SIMULATED)")
