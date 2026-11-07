# Event contracts (schema_version 1)

Source of truth for messages between the mobile/web client, the FastAPI server
(`server/`), and later the ESP32. Pydantic models live in
`server/app/schemas/events.py`; any change here needs a change there plus tests.

All messages are JSON text frames. Unknown fields are rejected (`extra="forbid"`).

## REST

| Method | Path                         | Notes                                          |
| ------ | ---------------------------- | ---------------------------------------------- |
| GET    | `/health`                    | `{"status":"ok"}`                              |
| POST   | `/api/workouts`              | body `{"exercise":"squat"}` → 201 `WorkoutOut` |
| GET    | `/api/workouts/{session_id}` | 200 `WorkoutOut` or 404                        |

`WorkoutOut`: `session_id`, `exercise`, `status` (`active`\|`completed`), `started_at`,
`total_reps`, `dropped_frames`, `aborted_attempts`.

**Auth: not implemented yet (Phase 2).** Until then anyone who can reach the server can create and
use sessions. Do not expose it publicly.

## WebSocket `/ws/workouts/{session_id}`

The session must already exist, otherwise the server closes with code **4404**. The `session_id`
in the URL decides which session is used; it must equal `session_id` in every message body.

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
- Knee angles: degrees, 0–180, 180 ≈ straight leg. Either may be `null` but not both.
- `landmark_confidence`: 0–1. Below 0.6 the frame is ignored (and tracking is reported as lost).
- Send knee angles only when the relevant landmarks are actually visible; send `null` otherwise.

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

`tracking_status` — sent only on change:
`{"type":"tracking_status","session_id":"UUID","tracking":"lost"|"recovered", ...}`

`error` — the socket stays open:
`{"type":"error","code":"invalid_message"|"session_mismatch","detail":"..."}`

### Not yet defined

`sensor_update` (ESP32), `coaching_feedback`, `set_finished`: see AGENTS.md; add here when built.

## Simulated data

`python -m app.simulation --reps 5` (from `server/`) streams generated pose updates to a running
server. This is synthetic data, never real camera or sensor output.
