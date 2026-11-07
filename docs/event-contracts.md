# Event contracts (schema_version 1)

Source of truth for messages between the mobile/web client, the FastAPI server
(`server/`), and later the ESP32. Pydantic models live in
`server/app/schemas/events.py`; any change here needs a change there plus tests.

All messages are JSON text frames. Unknown fields are rejected (`extra="forbid"`).

## REST

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | `{"status":"ok"}` (no auth) |
| GET | `/api/exercises` | 200 list of supported exercises (**no login needed**): `slug`, `display_name`, `aliases`, `analysis_supported`, `primary_angles`. Build the exercise picker from this |
| POST | `/api/workouts` | body `{"exercise":"<any spelling>"}` (optional; defaults to `"squat"`) → 201 `WorkoutOut`. Unknown exercise → 422 listing the supported ones |
| GET | `/api/workouts?limit=20` | 200 list of your `WorkoutOut`, newest first (`limit` 1-100) |
| GET | `/api/workouts/{session_id}` | 200 `WorkoutOut`, or 404 |
| DELETE | `/api/workouts/{session_id}` | **Permanently** deletes the workout from both databases: its reps and coaching events (Supabase) and its sensor readings (Tiger Data). 204 on success; 404 if it is not yours or does not exist (so deleting twice is a 404); 503 if sensor data could not be deleted right now, in which case **nothing** was deleted and it is safe to retry. A socket that is still streaming that workout is closed with `4409`. There is no undo, so the app should confirm first |
| GET | `/api/workouts/{session_id}/summary` | 200 `WorkoutSummaryOut`, or 404. Works on active workouts too (reps saved so far) |
| GET | `/api/workouts/{session_id}/coach-summary` | 200 `{"summary": "<3-4 sentence recap>", "source": "agent"\|"rules"}`: a plain-language recap of the workout for the end-of-workout screen. `source: "rules"` is a deterministic recap (used when the AI coach is off, unavailable, or the user reported pain). Finished workouts are cached. At most ~6 per minute per user (429 beyond that) |
| GET | `/api/workouts/{session_id}/analytics` | 200 compact analytics: `supabase` (rep stats, coaching events) + `timeseries` (SQL summaries from Tiger Data, per-rep depth/range trends). `timeseries.available` is `false` with a `reason` (`not_configured` / `unavailable`) when Tiger Data is off or down |
| GET | `/api/workouts/{session_id}/readings?start=&end=&metric=&bucket_seconds=&limit=` | 200 `{available, readings[]}`: sensor measurements in `[start, end)` (ISO times, naive = UTC), optional `metric` repeats, SQL-downsampled when `bucket_seconds` is set, max 5000 rows. 422 for a bad window |
| POST | `/api/workouts/{session_id}/finish` | 200 `WorkoutOut` (idempotent), 404, or 503 if some reps could not be saved yet (retry) |

**Exercises.** Supported: `squat`, `push-up`, `pull-up`, `deadlift`, `bench-press`, `lunge`, `plank`. The
`exercise` value is matched loosely and returned in its canonical form: `"Push Up"`, `"pushup"`, `"PUSH_UP"` and
`"push-up"` all create a `push-up` workout. A bad value gets a 422 such as
`Unsupported exercise 'burpee'. Supported: squat, push-up, ...` (with a "Did you mean" for near misses).

**Two levels of support, do not confuse them.** _Logging_ works for every exercise: the workout is created,
streamed to (camera/ESP32 readings are recorded in Tiger Data if configured), finished, shown in history, and the
user can pause or report pain. _Analysis_ (the server counting reps and the rule-based coach giving feedback) exists
for **squat only**. For every other exercise `analysis_supported` is `false`: the server does **not** count reps
or coach, `total_reps` stays 0, and the app should say tracking isn't available yet rather than show a rep counter.

`WorkoutOut`: `session_id`, `exercise` (canonical), `analysis_supported`, `status` (`active`\|`completed`), `started_at`, `ended_at`
(null while active), `total_reps`, `dropped_frames`, `aborted_attempts` (the last two are live
counters, 0 when the session isn't streaming).

`WorkoutSummaryOut` (for the end-of-workout and history screens):

```json
{
  "workout": { "...": "a WorkoutOut" },
  "stats": {
    "rep_count": 5,
    "avg_min_knee_angle_deg": 87.6,
    "deepest_knee_angle_deg": 85.0,
    "shallowest_knee_angle_deg": 98.0,
    "depth_change_deg": 8.7,
    "avg_rep_duration_ms": 1716,
    "max_asymmetry_deg": 2.0
  },
  "reps": [
    {
      "rep_number": 1,
      "metrics": {
        "min_knee_angle_deg": 85.0,
        "duration_ms": 1716,
        "...": "..."
      },
      "form_score": null,
      "created_at": "ISO time"
    }
  ],
  "coaching_events": [
    {
      "rep_number": 5,
      "issue_code": "reduced_depth",
      "message": "...",
      "action": "send_feedback",
      "created_at": "ISO time"
    }
  ]
}
```

Knee angle: smaller = deeper. `depth_change_deg` = average of the last 3 reps minus the first 3;
positive means later reps were shallower. Stats fields are `null` when there is nothing to compute
them from (no reps; `depth_change_deg` needs at least 4 reps).

**Auth:** every `/api/*` call needs `Authorization: Bearer <Supabase access token>` (the `access_token`
from Supabase Auth sign-in). Missing/invalid → 401. A session that belongs to another user returns 404,
same as one that doesn't exist.

**Request limits.** Each user may make about 10 `/api/*` requests per second sustained (burst 60), and start about
20 workouts per minute (burst 10). Beyond that you get `429 too many requests` with a `Retry-After` header (seconds);
wait that long and retry. Real app usage is far below these. `/health`, `/` and `/api/exercises` are not limited.

Sessions, reps and totals are stored in Supabase. `total_reps` on finish is the number of repetition rows
actually saved. Starting a workout does not require the WebSocket to stay connected: if the socket drops,
reconnect and the rep count continues from what was saved.

## WebSocket `/ws/workouts/{session_id}`

**Handshake.** After connecting, the _first_ message must be (within 10 s):

```json
{
  "schema_version": 1,
  "type": "auth",
  "access_token": "<Supabase access token>"
}
```

The token is sent in a message, not the URL, so it doesn't end up in logs. On success the server replies
`{"type":"session_ready","session_id":"UUID","rep_count":0}`; start sending `pose_update` after that.
Otherwise the server closes the socket with an application close code:

| Code | Meaning                                                                                                                                                                                                                                                                         |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 4401 | first message missing/not `auth`, bad or expired token, or timeout. Also sent on a healthy connection that has been open for **3 hours**, so the server re-checks the login: refresh the token (`supabase.auth.getSession()`) and reconnect; the workout continues where it was |
| 4404 | session doesn't exist **or belongs to someone else**                                                                                                                                                                                                                            |
| 4409 | session is already completed (or was finished while streaming)                                                                                                                                                                                                                  |
| 4410 | a newer connection took this session over (see below). Don't reconnect automatically on this code                                                                                                                                                                               |
| 1009 | a message was larger than 16 KB                                                                                                                                                                                                                                                 |

**One socket per session.** If the same session connects again (e.g. the phone dropped Wi-Fi and reconnected
while the server still thinks the old connection is alive), the newest authenticated connection wins and the
old one is closed with `4410`. Rep counting continues from where it was, and nothing is counted twice.

**Limits.** Messages over 16 KB close the socket (`1009`). Measurements (`pose_update`, `sensor_update`) are limited
to about 300/s sustained (burst 600); excess ones are dropped and you get one `error` event with code
`rate_limited` (at most once per 5 s); the socket stays open. `user_signal` messages have their own separate
limit (5/s, burst 20), so a flood of measurements can never prevent a **pain** report getting through.
A workout whose app disappears is forgotten by the server after 30 minutes without a socket (its saved reps
are safe in Supabase and the workout can be resumed by connecting again).

The `session_id` in the URL decides which session is used; it must equal `session_id` in every message body.
Each rep is saved to Supabase in the background right after `rep_completed` is sent. A database outage
never stops counting; failed saves are retried when `finish` is called.

### Client → server: `pose_update`

```json
{
  "schema_version": 1,
  "type": "pose_update",
  "session_id": "UUID",
  "sequence": 103,
  "timestamp_ms": 1794000000000,
  "data": {
    "left_knee_angle_deg": 94.2,
    "right_knee_angle_deg": 93.0,
    "landmark_confidence": 0.95
  }
}
```

- `sequence`: integer ≥ 0, strictly increasing per session. Repeats and lower values are dropped.
- `timestamp_ms`: integer ≥ 0, client clock (epoch ms or any monotonic origin, but consistent
  within a session). Only differences are used. A decrease drops the frame; a gap over 2 s disarms
  the counter until a standing frame is seen.

* Joint angles: degrees, 0–180, 180 ≈ straight. Fields: `left_knee_angle_deg`, `right_knee_angle_deg`,
  `left_elbow_angle_deg`, `right_elbow_angle_deg`, `left_hip_angle_deg`, `right_hip_angle_deg`. Send only joints that
  are visible (omit or `null` the rest); at least one is required. **Only the knee angles are analyzed (squat).**
  Elbow and hip angles are accepted and recorded in Tiger Data for the other exercises but nothing counts or
  coaches from them yet.
* `landmark_confidence`: 0–1. Below 0.6 the frame is ignored (and tracking is reported as lost).
* Send knee angles only when the relevant landmarks are actually visible; send `null` otherwise.

### Client → server: `sensor_update` (ESP32 wearable data, relayed over this same socket)

```json
{
  "schema_version": 1,
  "type": "sensor_update",
  "session_id": "UUID",
  "device_id": "esp32-01",
  "sequence": 56,
  "timestamp_ms": 123456,
  "data": {
    "flex_raw": 1780,
    "imu_pitch_deg": 12.4,
    "imu_roll_deg": 1.0,
    "accel_z_g": 0.98
  }
}
```

`data` fields (all optional, at least one required; units are what the sensor reports, not calibrated angles):
`flex_raw` (raw ADC counts, ≥ 0), `imu_pitch_deg` / `imu_roll_deg` / `imu_yaw_deg` (-360..360),
`accel_x_g` / `accel_y_g` / `accel_z_g` (g, -64..64). `device_id`: 1-64 chars of `A-Za-z0-9_.:-`.
`timestamp_ms` is the **device's own clock** (any origin); the server anchors it to server time on the first
message from that device. Do not try to match it to the camera's timestamps.

Sensor and camera readings are stored in Tiger Data (see `TIGERDATA_SETUP.md`), not Supabase, and nothing is
sent back to the client for them. If Tiger Data is not configured the message is still accepted and ignored.
Ignored while the workout is paused. Invalid messages get an `error` event like any other.

### Client → server: `user_signal` (the user's own buttons)

```json
{
  "schema_version": 1,
  "type": "user_signal",
  "session_id": "UUID",
  "signal": "pain"
}
```

`signal` is one of:

- `pause`: the server ignores `pose_update`s until `resume`. Use for rests or when the user steps away.
- `resume`: continue. The rep counter forgets any half-finished movement and waits for the user to stand up,
  so resuming mid-squat never counts a bogus rep.
- `pain`: the user reports pain or discomfort. The server immediately sends a `coaching_feedback`
  (`issue_code: "pain_reported"`) telling them to stop and to talk to a qualified health professional, turns
  **technique coaching off for the rest of the session** (counting continues), and saves a `coaching_events`
  row (`action: "recommend_rest"`). The "off" state is remembered across server restarts and reconnects.
  The app should make this button easy to reach and should stop prompting the user to continue.

Every signal is answered with a `session_state` message (below).

### Server → client

`rep_completed` — one per counted rep:

```json
{
  "schema_version": 1,
  "type": "rep_completed",
  "session_id": "UUID",
  "rep_count": 5,
  "metrics": {
    "min_knee_angle_deg": 85.0,
    "descent_ms": 363,
    "ascent_ms": 396,
    "duration_ms": 1716,
    "max_asymmetry_deg": 2.0
  }
}
```

`max_asymmetry_deg` is `null` if both knees were never visible in the same frame.
`duration_ms` runs from leaving standing (< 150°) to returning to standing (≥ 160°).

**Personal standing angle.** Those 150°/160° are the defaults. If someone's knees read lower than 160° when they stand (camera angle, slightly bent knees) and hold still for about a third of a second, the server lowers the standing threshold to just under their resting angle (never below 145°, never above 160°) and keeps the same 10° gap, so their reps are counted instead of silently ignored. Depth (≤ 110°) is not adapted. Checked on synthetic noisy data only (all 360 noisy runs counted exactly; fidgeting produced no false reps); not checked on real camera data.

`coaching_feedback` — sent right after the `rep_completed` that triggered it, and saved to
`coaching_events`:

```json
{
  "schema_version": 1,
  "type": "coaching_feedback",
  "session_id": "UUID",
  "rep_count": 5,
  "issue_code": "reduced_depth",
  "message": "Your depth has decreased compared with earlier reps. Try your previous comfortable range.",
  "source": "rules"
}
```

`source` is `"rules"` (the deterministic coach's message) or `"agent"` (an LLM worded it; only when the server has the
AI coach enabled, see `docs/AI_COACH.md`). Show `message` the same way either way. With the AI coach on, a cue may
arrive a few seconds **after** its `rep_completed` (the model takes a moment, and the app keeps counting meanwhile),
or not at all if the model judged it unhelpful. Never assume a cue is immediate.
Issue codes: `reduced_depth` (last 2 reps clearly shallower than the median of the first 3),
`asymmetry` (left/right knee angles differ a lot in 2 reps in a row). Feedback is rate-limited: none
until 3 reps exist, at least 2 reps between messages, 4 reps before repeating an issue, max 6 per
session. Messages describe measurements only; they never give medical advice. The thresholds are
untuned starting values (`CoachConfig` in `server/app/services/coaching.py`).
Clients should treat `issue_code` as an open set and show `message` as-is.

`session_state` — the reply to every `user_signal`:
`{"type":"session_state","session_id":"UUID","paused":false,"coaching_enabled":true}`
(`coaching_enabled` is `false` after a pain report). `session_ready` also carries `exercise` and
`analysis_supported` (false = this workout is logged but reps are not counted), and the same two fields
(`paused`, `coaching_enabled`) so a reconnecting app can restore its UI.

`tracking_status` — sent only on change:
`{"type":"tracking_status","session_id":"UUID","tracking":"lost"|"recovered", ...}`

`error` — the socket stays open:
`{"type":"error","code":"invalid_message"|"session_mismatch"|"rate_limited","detail":"..."}`

### Not yet defined

`set_finished` and sensor calibration: see AGENTS.md; add here when built.

## Simulated data

From `server/`: `python -m scripts.e2e_live` runs the full flow against your Supabase project with a
throwaway user (and cleans up). `python -m app.simulation --reps 5 --token <access_token>` streams
generated pose updates to a running server. This is synthetic data, never real camera or sensor output.
