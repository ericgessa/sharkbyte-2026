# AI posture coach (upper-back IMU + Gemini)

This feature turns the ESP32's upper-back sensor readings into short, plain-language posture warnings, such as
*"You are tilting to the right at the bottom of your squat. Focus on keeping your shoulders level as you rise."*

**Status:** built and tested on its own, with simulated data and live Gemini calls. It is **not connected to
the live workout WebSocket yet** (see [What's not done](#whats-not-done)), so the app doesn't receive these
warnings today.

## How it works

```
sensor_update (ESP32 pitch/roll)
      │
      ▼
posture.py   PostureTracker   finds each squat in the torso-tilt signal and summarizes it
             detect_issues    rules over the rep history  →  issue_code + measured evidence
             PostureCoach     rate limits; picks a template message
      │
      ▼
ai_coach.py  AICoach          asks Gemini to reword the warning from the evidence
             GeminiLLM        gemini-3.8-flash via google-genai
      │                        └─ timeout, API error or invalid reply → template message instead
      ▼
warning text (source "agent" if Gemini wrote it, "rules" if the template was used)
```

The key design rule: **code decides whether to warn and what about; Gemini only decides how to say it.**
Gemini never counts reps, never decides an issue exists, and can't change any numbers. If it fails in any way,
the user still gets the template warning. This follows AGENTS.md: the fast loop never waits on the LLM, and
the rule-based path is the fallback.

| File | What it does |
| --- | --- |
| `server/app/sensor_simulation.py` | Generates a labelled mock dataset of upper-back sensor readings |
| `server/app/services/posture.py` | Deterministic posture detection and rate-limited warnings |
| `server/app/services/ai_coach.py` | Gemini wording layer: prompt, reply validation, fallback, debug logging |
| `server/tests/test_sensor_simulation.py`, `test_posture.py`, `test_ai_coach.py` | 136 tests, no network needed |

## 1. Mock sensor dataset

There's no real hardware data yet, so `sensor_simulation.py` generates **simulated** `sensor_update` messages
that match the contract in `docs/event-contracts.md`. Every message passes the server's `SensorUpdate` validation.

**Mounting**: one IMU flat on the upper back, between the shoulder blades. The hardware team confirmed this: it's
the BNO085 in [`hardware/README.md`](../hardware/README.md). The wearable also sends per-section spine bend
(`spine_upper_deg`, `spine_mid_deg`, `spine_lumbar_deg`), which this coach doesn't use or simulate yet. Its pitch is
measured from the wearer's calibrated standing posture, not from true vertical.

* `imu_pitch_deg`: forward tilt of the upper back from vertical (+ = leaning forward)
* `imu_roll_deg`: sideways tilt (+ = toward the user's right)
* `accel_x/y/z_g`: gravity plus a little squat motion, about 1 g at rest
* Not modelled: the spine section angles, and yaw (drifts without a magnetometer). The project has no flex sensors.

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

## 3. Gemini wording (`ai_coach.py`)

**What's sent.** A fixed system prompt with the rules plus a JSON prompt like this:

```json
{
  "exercise": "squat",
  "issue": "lateral_shift",
  "what_it_means": "At the bottom of the latest reps the shoulders tilted to one side (see evidence.side).",
  "evidence": {"reps": [1, 2], "bottom_roll_deg": [7.5, 7.4], "side": "right", "threshold_deg": 5.0},
  "default_cue": "Your shoulders tilt to the right at the bottom of your squats. Try to keep your weight even on both feet.",
  "recent_cues": []
}
```

Only summarized numbers are sent: no raw sensor stream, no user identity. `recent_cues` holds the last 3
messages, so Gemini avoids repeating itself.

**What's expected back.** JSON `{"message": "..."}` (structured output). The reply is rejected, and the
template used instead, if it:

* isn't valid JSON of that shape, or is empty;
* is over 160 characters or 30 words;
* contains any digits (the raw angles are uncalibrated and shouldn't be shown to users);
* contains a link;
* mentions the spine, injury, diagnosis, pain, doctors, danger, or the sensor;
* for `lateral_shift`, doesn't name the measured side, or names the other side.

**Model call.** `gemini-3.8-flash` (override with `GEMINI_MODEL`) through the official `google-genai` SDK
(version 3.0 or newer), using the async client, low thinking level, and no automatic retries (a retried cue
would arrive too late).

**Timeouts.** We wait at most **4 s**, enforced in our own code (`asyncio.wait_for`). The SDK itself is given a
10 s deadline, because Gemini rejects deadlines under 10 s with `400 INVALID_ARGUMENT`. We found that on the
first live run. Live replies took **2.1–2.6 s**.

**Swappable.** The model sits behind a small `LLM` protocol (`generate(system, prompt, schema) -> str`).
`FakeLLM` runs the whole flow offline, and another provider would only need a new class.

### Live results (simulated input, real Gemini calls)

| Issue | Template | Gemini |
| --- | --- | --- |
| `forward_lean_increasing` | You're leaning further forward than you did in your first reps. If you're getting tired, it's fine to take a short rest. | You are leaning further forward as you squat. Focus on keeping your chest up on the way down. |
| `lateral_shift` | Your shoulders tilt to the right at the bottom of your squats. Try to keep your weight even on both feet. | You are tilting to the right at the bottom of your squat. Focus on keeping your shoulders level as you rise. |
| `rounded_standing_posture` | Your upper back is tilted forward while you stand between reps. Try standing tall before the next rep. | Your upper back stayed tilted forward while standing between reps. Try standing tall and opening your chest before starting the next rep. |

## Running it

Setup, once, from `server/`:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt     # includes google-genai>=3.0
```

Put your key in `server/.env` (git-ignored, **backend only, never in the mobile app**):

```
GEMINI_API_KEY=...
# optional
GEMINI_MODEL=gemini-3.8-flash
AI_COACH_DEBUG=1
```

Then:

```powershell
python -m app.sensor_simulation --out mock_data/sensors                        # generate the dataset
python -m app.services.posture mock_data/sensors/lean_creep.jsonl              # detection only, no API call
python -m app.services.ai_coach mock_data/sensors/lean_creep.jsonl --debug     # detection + Gemini
python -m app.services.ai_coach mock_data/sensors/lean_creep.jsonl --fake      # whole flow, offline
python -m pytest tests/test_sensor_simulation.py tests/test_posture.py tests/test_ai_coach.py -q
```

`--debug` (or `AI_COACH_DEBUG=1`) prints exactly what is sent to the model and what it returns, cut at 800
characters, plus the latency. The API key is never printed. When the template is used instead of Gemini, the
output says why, e.g. `(fallback: ClientError: 400 INVALID_ARGUMENT ...)` or `(fallback: timed out after 4 s)`.

`mock_data/` regenerates exactly from the seed, so don't commit it.

## What's not done

* **Live wiring.** Nothing in `websocket.py` calls this yet. The plan:
  * keep a `PostureCoach` and an `AICoach` per workout in the live session;
  * feed each `sensor_update` to the posture coach;
  * phrase warnings in a background task (`asyncio.create_task`), so rep counting never waits on Gemini;
  * send them as `coaching_feedback` (which already accepts `source: "agent"`) and save them to `coaching_events`.

  This changes shared server files, so it needs agreement first.
* **One rate limit across coaches.** Posture warnings and the camera `RuleCoach` currently limit themselves
  separately. Wired up as-is, the user could get two messages on the same rep.
* **Pain report.** It should switch off posture warnings too (`PostureCoach.enabled = False`).
* **Contract.** The four new issue codes aren't listed in `docs/event-contracts.md` yet. The app already treats
  `issue_code` as an open set and shows `message` as-is.
* **Calibration step.** A "stand tall for 3 seconds" step at workout start, to set `mounting_offset_deg`.
* **Real data.** Everything so far is simulated. The thresholds and the mounting assumptions need checking
  against real ESP32 recordings.
* **`server/.env.example`** should list `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`, `AI_COACH_DEBUG`).

## Questions for the team

* **Hardware:** answered in `hardware/README.md`. The BNO085 sits between the shoulder blades; calibration learns
  each sensor's forward direction, so axis orientation doesn't matter; no flex sensor; 20 samples/s over USB.
  Still open: confirming the `imu_roll_deg` sign on a real wearer.
* **App:** how should a posture warning look compared with a camera tip (banner, toast, haptic, spoken)?
* **Everyone:** OK to wire this into the live WebSocket, and who should review that change?
