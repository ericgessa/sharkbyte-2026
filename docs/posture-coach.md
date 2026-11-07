# Posture coach (upper back tilt)

The wearable's top sensor (a BNO085 between the shoulder blades) sends the upper body's forward lean
(`imu_pitch_deg`, the device's `torso`) and side tilt (`imu_roll_deg`). During a squat workout the server turns repeated
lean patterns into short warnings, such as *"Your upper back tips well forward at the bottom of your squats. Try
keeping your chest a little taller."*

**Status:** live in the workout WebSocket, **deterministic**: warnings are the rule messages, sent at once
(`source: "rules"`). Gemini is not used during a workout; it only mentions what came up in the end-of-workout report
([AI_COACH.md](AI_COACH.md)). Checked with SIMULATED data only, never with a real wearable. The wearable's section
angles (back shape inside each rep) are handled separately by form IQ ([form-iq.md](form-iq.md)).

## How it works

```
sensor_update (squat workouts only)
      |
      v
websocket.py  _coach_posture   one PostureCoach per device_id; skips a warning if any cue went out < 6 s ago
      |
      v
posture.py    PostureTracker   finds each squat in the lean signal and summarizes it
              detect_issues    rules over the rep history  ->  issue_code + measured evidence
              PostureCoach     rate limits; picks the rule message
      |
      v
coaching_feedback to the app (source "rules"), saved to coaching_events -> counted in the end-of-workout report
```

| File | What it does |
| --- | --- |
| `server/app/sensor_simulation.py` | Generates a labelled, simulated dataset of upper-back lean readings |
| `server/app/services/posture.py` | Deterministic posture detection and rate-limited warnings |
| `server/app/api/websocket.py` | `_coach_posture`: feeds `sensor_update` to the posture coach and delivers its warnings |
| `server/tests/test_sensor_simulation.py`, `test_posture.py`, `test_posture_coaching.py` | Offline tests, including the live socket |

## 1. Mock sensor dataset

There's no real hardware data yet, so `sensor_simulation.py` generates **simulated** `sensor_update` messages
that match the contract in `docs/event-contracts.md`. Every message passes the server's `SensorUpdate` validation.

**Assumed mounting** (confirm with the hardware team): one IMU flat on the upper back, between the shoulder blades.

* `imu_pitch_deg`: forward tilt of the upper back from vertical (+ = leaning forward)
* `imu_roll_deg`: sideways tilt (+ = toward the user's right)
* `accel_x/y/z_g`: gravity plus a little squat motion, about 1 g at rest
* Not modelled: flex sensor (placement undecided) and yaw (drifts without a magnetometer)

Each scenario injects a known pattern, so detection can be checked against a correct answer:

| Scenario | What's injected | Expected issue |
| --- | --- | --- |
| `good_form` | upright standing, about 40° lean at the bottom, level shoulders | none |
| `forward_lean` | about 62° lean at the bottom of every rep | `excessive_forward_lean` |
| `lean_creep` | lean grows 40° → 60° and reps slow down (fatigue) | `forward_lean_increasing` |
| `lateral_shift` | shoulders tilt about 8° right at the bottom | `lateral_shift` |
| `slouched_standing` | upper back tilted about 20° forward while standing between reps | `rounded_standing_posture` |
| `sensor_dropout` | good form plus a 1.5 s Bluetooth gap during rep 3 | none (must not invent one) |

Output per scenario: `<name>.jsonl` (one message per line, 50 Hz, on the device's own clock) and
`<name>.labels.json` (the true values per rep, marked `"simulated": true`). The same seed always produces the
same data. The angles are plausible values, not measurements.

## 2. Posture detection (`posture.py`)

**Finding reps.** It works from the IMU alone and doesn't need the camera rep counter. A rep starts when pitch
rises 12° above the user's standing pitch and ends when it drops back within 6°. Excursions shorter than 0.8 s
are ignored as noise. "Standing pitch" is learned continuously from the samples between reps, so it adapts to
each user and to posture that changes during a set.

**Robustness:**

* Duplicate or out-of-order samples are dropped.
* A gap over 0.5 s abandons the rep in progress, so a rep is never guessed across a Bluetooth dropout.
* An ESP32 reboot (the clock jumping backwards) is handled.
* A "rep" longer than 8 s means the user's standing posture changed, so it's discarded and standing is re-learned.

**Issues.** Each one must show in 2 reps in a row before it's reported:

| `issue_code` | Rule (untuned starting values) |
| --- | --- |
| `forward_lean_increasing` | peak lean at least 8° more than the median of the first 3 reps |
| `excessive_forward_lean` | peak lean at least 55° from vertical |
| `lateral_shift` | mean tilt at the bottom at least 5°, same side both reps. **Off by default** until the wearable's roll sign is checked |
| `rounded_standing_posture` | standing tilt at least 15° from vertical |

When several issues show on the same rep, the first in this table wins. **Rate limiting** follows the same
scheme as `RuleCoach`: at least 2 reps between any two warnings, 4 reps before the same issue repeats, and at
most 6 per session. `PostureCoach.enabled = False` silences it (e.g. after a pain report) while it keeps tracking.

Every warning carries **evidence**, the numbers behind it. That is what Gemini receives:

```json
{"reps": [4, 5], "early_reps_peak_pitch_deg": 45.4, "recent_peak_pitch_deg": [55.8, 60.3],
 "recent_duration_ms": [2061, 2200], "early_duration_ms": [1621, 1760, 1999], "threshold_increase_deg": 8.0}
```

**Calibration matters.** A strap that sits tilted looks like a slouch. `PostureConfig(mounting_offset_deg=...)`
subtracts the pitch read while the user stands tall. Until there's a calibration step, only trust the relative
check (`forward_lean_increasing`), not the absolute ones (`excessive_forward_lean`, `rounded_standing_posture`).

**Limits.** One IMU measures tilt, not spinal shape: it can't tell a hip hinge from a rounded back. The
messages describe tilt only and never diagnose. The thresholds were chosen against simulated data and need
tuning on real recordings.

## Running it

From `server/`, with the venv active:

```powershell
python -m app.sensor_simulation --out mock_data/sensors             # write the dataset to files
python -m app.services.posture mock_data/sensors/lean_creep.jsonl   # replay one through the detector
python -m pytest tests/test_sensor_simulation.py tests/test_posture.py tests/test_posture_coaching.py -q
```

`mock_data/` regenerates exactly from the seed and is git-ignored.

## What's not done

* **Real data.** Everything so far is simulated; the thresholds need real, labelled squat recordings.
* **Roll sign.** Check it on the device (hardware/README.md), then switch `lateral_shift_enabled` on.
* **Calibration.** The wearable calibrates standing posture itself; `mounting_offset_deg` stays 0 unless that changes.
* **One rate limit for every coach.** Posture warnings wait 6 s after any cue; camera cues are never held back.
