# Posture coach (upper-back IMU + Gemini)

The ESP32 on the user's upper back streams tilt readings during a squat workout. The server turns repeated posture
patterns into short warnings, such as *"Your upper back tilts to the right at the bottom. Keep your weight even."*

**Status:** live in the workout WebSocket since 2026-11-07, through the same AI coach layer as the camera cues
(`server/app/services/agent.py`, [AI_COACH.md](AI_COACH.md)). Checked with SIMULATED sensor data and live Gemini
calls; never with a real ESP32.

## How it works

```
sensor_update (ESP32 pitch/roll, squat workouts only)
      |
      v
websocket.py  _coach_posture   one PostureCoach per device_id; skips a cue if any cue went out < 6 s ago
      |
      v
posture.py    PostureTracker   finds each squat in the torso-tilt signal and summarizes it
              detect_issues    rules over the rep history  ->  issue_code + measured evidence
              PostureCoach     rate limits; picks the rule message
      |
      v
agent.py      CoachAgent       Gemini rewords it from build_posture_context (or stays quiet)
      |                         timeout, API error or a failed check -> the rule message instead
      v
coaching_feedback to the app (source "agent" or "rules"), saved to coaching_events
```

The key design rule: **code decides whether to warn and what about; Gemini only decides how to say it** (or to
stay quiet). It never counts reps, never decides an issue exists, and can't change any numbers. This is the same
path, guardrails and fallback as the camera cues.

| File | What it does |
| --- | --- |
| `server/app/sensor_simulation.py` | Generates a labelled, simulated dataset of upper-back sensor readings |
| `server/app/services/posture.py` | Deterministic posture detection and rate-limited warnings |
| `server/app/services/agent.py` | `build_posture_context` + the shared Gemini layer (checks, fallback, debug logging) |
| `server/app/api/websocket.py` | `_coach_posture`: feeds `sensor_update` to the posture coach and delivers its cues |
| `server/scripts/check_posture_llm.py` | Live check: simulated posture data through the real Gemini path |
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
| `lateral_shift` | mean tilt at the bottom at least 5°, same side both reps |
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

## 3. Gemini wording (through `agent.py`)

**What's sent** (`build_posture_context`): only server-computed numbers, no raw stream, no user identity.

```json
{
  "exercise": "squat",
  "source": "upper_back_imu",
  "reps_completed": 2,
  "detected_issue": {"code": "lateral_shift", "meaning": "At the bottom of the latest reps the shoulders tilted to one side (see side)."},
  "rule_message": "Your shoulders tilt to the right at the bottom of your squats. Try to keep your weight even on both feet.",
  "evidence": {"reps": [1, 2], "bottom_roll_deg": [7.5, 7.4], "side": "right", "threshold_deg": 5.0},
  "numbers_allowed": false,
  "side": "right",
  "feedback_already_given": []
}
```

The cue being worded is never listed under `feedback_already_given`. On the first live run it was, and Gemini
treated 3 of 4 cues as repeats and stayed quiet.

**What comes back:** `{"action": "send_feedback" | "no_action" | "recommend_rest", "message": "..."}`. The reply is
rejected, and the rule message used instead, if it fails the checks every cue gets (empty, over 200 characters,
medical or spine words, links or markup) or the two posture-only ones:

* `numbers_allowed: false`: any number at all (the tilt angles aren't calibrated, so users never see them);
* `side`: a lateral shift must name the measured side, and never the other one.

`recommend_rest` is only allowed after 5 reps. A pain report stops posture cues for the rest of the workout.

**Model and timing.** `COACH_LLM_MODEL` (default `gemini-3.8-flash`). The cue waits at most
`COACH_LLM_TIMEOUT_S` (default 10 s). The SDK's own deadline is never under 10 s, because Gemini rejects shorter
deadlines with `400 INVALID_ARGUMENT`.

### Live results (2026-11-07, simulated input, real Gemini calls)

| Issue | `gemini-3.5-flash-lite` (0.7-2.1 s) | `gemini-3.8-flash` (3.8-5.3 s) |
| --- | --- | --- |
| `excessive_forward_lean` | Your upper back tilted far forward at the bottom of your squats. Try keeping your chest taller. | Your upper back is tilting far forward at the bottom of your squat. Focus on keeping your chest proud! |
| `forward_lean_increasing` | Your upper back is tilting further forward and your reps are slowing down. Take a short rest. *(recommend_rest)* | Your upper back is tilting further forward as your reps slow down. Take a quick rest before your next set! *(recommend_rest)* |
| `lateral_shift` | Your upper back tilts to the right at the bottom. Keep your weight even. | Your upper back is tilting to the right at the bottom. Focus on keeping your shoulders level as you stand up. |
| `rounded_standing_posture` | Your upper back is tilted forward while you stand between reps. Try standing tall before the next rep. | Your upper back is tilted forward while standing between reps. Stand tall and upright before your next rep. |

Both fit inside the 10 s default timeout. With `gemini-3.8-flash` a posture cue can reach the app up to about 5 s after the rep.

## Running it

In `server/.env` (git-ignored, **backend only, never in the mobile app**):

```
COACH_LLM_PROVIDER=gemini
GEMINI_API_KEY=...
# optional
COACH_LLM_MODEL=gemini-3.8-flash
COACH_LLM_DEBUG=1        # log each request sent to Gemini and its reply (cut at 800 characters; never the key)
```

Without `COACH_LLM_PROVIDER` the server still sends posture cues, with the rule messages. From `server/`, with the
venv active:

```powershell
python -m scripts.check_posture_llm                          # live: every warning scenario through Gemini
python -m scripts.check_posture_llm lateral_shift --debug    # one scenario, showing what is sent and returned
python -m app.sensor_simulation --out mock_data/sensors       # write the dataset to files
python -m app.services.posture mock_data/sensors/lean_creep.jsonl   # detection only, no API call
python -m pytest tests/test_sensor_simulation.py tests/test_posture.py tests/test_posture_coaching.py -q
```

`mock_data/` regenerates exactly from the seed, so don't commit it.

## What's not done

* **Calibration step.** A "stand tall for 3 seconds" step at workout start, to set `mounting_offset_deg`. Until then
  trust the relative check (`forward_lean_increasing`) more than the absolute ones.
* **Real data.** Everything so far is simulated. The thresholds and mounting assumptions need real ESP32 recordings.
* **App UI.** Posture cues arrive as ordinary `coaching_feedback`; the app shows `message` as-is. A distinct look for
  posture warnings is the app team's call.
* **One rate limit for both coaches.** Posture cues wait 6 s after any cue; camera cues are never held back by posture
  cues. A camera cue can still follow a posture cue closely.

## Questions for the team

* **Hardware:** is the IMU between the shoulder blades, flat? Which way are its axes? Is there a flex sensor,
  and where? What sample rate will the ESP32 actually send?
* **App:** how should a posture warning look compared with a camera tip (banner, toast, haptic, spoken)?
* **Everyone:** OK to wire this into the live WebSocket, and who should review that change?
