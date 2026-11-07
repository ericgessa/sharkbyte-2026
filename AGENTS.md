# AGENTS.md — SharkByte Personal Gym Coach

## Mission
Build a hackathon-ready personal gym coach that combines webcam pose estimates and optional ESP32 wearable measurements to count exercise repetitions, characterize observable movement, offer timely AI coaching cues, and save workout history. Prioritize a reliable end-to-end squat demonstration over adding extra features.

## Project status and decisions
- **Working concept:** Personal Gym Coach; final name undecided.
- **Observed whiteboard ideas:** Python computer vision, YOLO/MediaPipe, pretrained models/PyTorch, ESP32/Arduino C++, React and a frontend/backend split.
- **Recommended implementation (not all confirmed by the team):** React frontend; MediaPipe Pose Landmarker running in browser; FastAPI backend; WebSockets; Pydantic/NumPy; Supabase Auth and PostgreSQL; LLM via provider adapter for coaching.
- **Optional, not MVP blockers:** YOLO Pose, custom PyTorch classifier, Redis, MQTT, Jetson deployment, a 3D avatar, multi-exercise support.
- Treat a recommendation as a proposal until the codebase or team explicitly confirms it. Inspect existing files before creating/replacing anything.

## MVP acceptance criteria
1. A user can sign in or use an explicitly configured, restricted demo session.
2. The frontend starts a squat workout and shows live camera tracking with consent.
3. Pose-landmark measurements reach the backend via an authenticated WebSocket.
4. The backend tracks movement phases and counts repetitions using deterministic, testable logic.
5. Optionally incorporate ESP32 flex/IMU measurements when connected; gracefully degrade to camera-only mode when absent.
6. At least one repeated observable movement issue triggers a concise coaching message; avoid messages every frame.
7. Completed repetitions and coaching events persist in Supabase and appear in a workout summary.
8. A full local demo works with prerecorded/sample pose and sensor messages even if live hardware is unavailable.

## Architectural rules
- **Frontend (React, TypeScript):** Camera permission, MediaPipe inference, drawing landmarks, controls, displaying rep counts and coaching messages. Do not put database secret keys or LLM API keys in client code.
- **Backend (Python 3.11+, FastAPI):** Session ownership, request validation, WebSocket transport, timestamp alignment, rep state machines, coaching orchestration, Supabase persistence.
- **Hardware (ESP32, Arduino C++):** Send timestamped, device-identified flex/IMU readings using an agreed protocol. Sensor readings are not automatically anatomical angles; calibrate and label units.
- **Database (Supabase Postgres):** Persist users, exercises, sessions, completed repetitions, coaching events. Do not write every webcam frame or high-frequency sensor sample to PostgreSQL.
- **AI coach:** Receives summarized, structured exercise events and limited history. It chooses among approved coaching actions. It never performs frame-by-frame rep counting and never makes clinical diagnoses.
- Separate the fast motion-processing loop from slower LLM requests. A failed or slow LLM must not interrupt rep counting.

## Repository layout
The frontend is an **Expo / React Native app in `mobile/`** (it has its own `mobile/AGENTS.md` with Expo rules; follow it for anything under `mobile/`). The backend lives in **`server/`** (not `backend/`). Items marked *planned* do not exist yet.
```
mobile/                     Expo app (src/app routes, components, hooks)
  AGENTS.md                 Expo-specific rules
server/
  app/main.py
  app/api/routes.py
  app/api/websocket.py
  app/schemas/events.py
  app/services/session_manager.py   in-memory for now
  app/services/rep_counter.py
  app/simulation.py                 simulated pose data for tests/demo
  app/services/coaching.py          rule-based coach (LLM coach would sit on top, using it as fallback)
  app/services/auth.py              Supabase token verification
  app/services/persistence.py       retryable database writes
  app/services/ingest.py            sensor buffer + batch writer to Tiger Data, clock anchoring
  app/services/timeseries.py        Tiger Data (TimescaleDB) client: the SECOND database
  app/services/analytics.py         SQL time-series analytics; coach_context.py merges both databases
  app/schemas/sensors.py            ESP32 sensor_update + stored SensorReading models
  app/services/sensor_fusion.py     planned
  app/services/database.py          Supabase client + session queries (not yet used by the API routes)
  scripts/                          check_db.py, db_example.py
  tests/
  .env.example
firmware/esp32/             planned
supabase/                   config.toml + migrations (see SUPABASE_SETUP.md)
tigerdata/migrations/       sensor_readings hypertable (see TIGERDATA_SETUP.md); Tiger Data is optional at runtime
SUPABASE_SETUP.md
docs/event-contracts.md
AGENTS.md
CLAUDE.md
```
Note: the "browser" wording elsewhere in this file (e.g. MediaPipe in the browser) predates the Expo decision. Where pose estimation runs in the Expo app is an open decision.

## Data contracts
Maintain `docs/event-contracts.md` as the source of truth. Version message schemas and validate them on both ends.

Example incoming pose event (illustrative angles, not validated ground truth):
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

Example sensor event:
```json
{
  "schema_version": 1,
  "type": "sensor_update",
  "session_id": "UUID",
  "device_id": "esp32-01",
  "sequence": 56,
  "timestamp_ms": 1794000000012,
  "data": {
    "flex_raw": 1780,
    "imu_pitch_deg": 12.4
  }
}
```

Example outgoing feedback event:
```json
{
  "schema_version": 1,
  "type": "coaching_feedback",
  "session_id": "UUID",
  "rep_count": 5,
  "issue_code": "reduced_depth",
  "message": "Your depth has decreased compared with earlier reps. Try your previous comfortable range.",
  "source": "rules_or_agent"
}
```

- Authenticate users before accepting session messages; do not trust client-provided `session_id`, `user_id`, or rep counts for authorization.
- Define timestamp clock origin, synchronization policy, and units. Client clock skew must be tolerated; align using a bounded buffer/server arrival times or estimated offsets.
- Discard or flag missing/low-confidence landmarks. Avoid claiming reliable spinal alignment from a single camera angle.
- Throttle or batch updates; persist only rep/issue/session summaries for MVP.

## Supabase data model
- `profiles(id UUID PK -> auth.users.id, display_name, created_at)`
- `exercises(id, name, config JSONB)`
- `workout_sessions(id UUID PK, user_id FK, exercise_id FK, started_at, ended_at, status, total_reps)`
- `repetitions(id UUID PK, session_id FK, rep_number, metrics JSONB, created_at, UNIQUE(session_id, rep_number))`
- `coaching_events(id UUID PK, session_id FK, rep_number NULL, issue_code, message, action, created_at)`
- Use migrations checked into source control. Index sessions by user/time and child records by session.
- Enable Row Level Security and write explicit ownership policies (including child-table ownership through parent session). Set table privileges appropriately; do not rely on RLS enablement alone.
- Keep Supabase secret/service-role keys only in backend environment variables. Authenticated backend requests must check user identity and session ownership, particularly if using a secret key that bypasses RLS. Do not commit `.env` or credentials.
- Store optional raw recordings in private object storage only with explicit consent, retention limits, and access controls. No recordings are required for MVP.

## API contract
- `GET /health` — backend readiness.
- `POST /api/workouts` — create a session for authenticated user.
- `GET /api/workouts/{session_id}` — session metadata and summary, ownership checked.
- `POST /api/workouts/{session_id}/finish` — finalize workout and persist summary.
- `WS /ws/workouts/{session_id}` — authenticated live events (pose updates, possibly sensor updates, rep events, coaching feedback).
- Optional `POST /api/devices/{device_id}/readings` or separate authenticated hardware WebSocket as the ESP32 transport is finalized.
- Document requests, responses, auth method, errors, and reconnection behavior.

## Exercise analysis requirements
- Begin with **squats only**. Build a deterministic state machine (standing -> descending -> bottom -> ascending -> standing), with hysteresis, confidence thresholds, and minimum phase durations to prevent double counts.
- Use calibrated joint angles and change over time; do not hardcode a universal medically 'correct' squat angle.
- Form observations are descriptive (e.g., shorter depth relative to own baseline, asymmetry in visible joint angles), not diagnoses or guarantees of safety.
- Allow users to pause/stop and report pain; when pain is reported, stop technique coaching and recommend stopping the activity and seeking appropriate professional advice.
- Log confidence and missing-data states; do not give confident corrections when the camera cannot see relevant joints.

## Agentic coaching workflow
1. Deterministic analysis emits `rep_completed`, `repeated_issue`, `set_finished`, `tracking_low_confidence`, or `user_requested_help` events.
2. Agent context builder fetches a bounded summary (latest reps, recurring issues, stated goal, last feedback, sensor confidence), not raw frames.
3. LLM produces a schema-validated decision and brief message; allow `no_action`.
4. Authorized backend tool performs an action: `send_feedback`, `recommend_rest`, `propose_target_adjustment`, or `save_summary`. Never let LLM output invoke arbitrary code, SQL, or network URLs.
5. Record coaching action and compare later reps with the stated target to see whether observable motion changed.

Agent guardrails:
- Rate-limit cues and avoid repeating the same instruction too frequently.
- Respect pain/discomfort and low-tracking-confidence signals; prefer a pause or no feedback over speculative advice.
- Do not instruct users to push through pain or provide medical advice.
- Never let an LLM independently change stored sensor observations or rep counts.
- Use a configurable provider adapter. Do not assume an OpenAI/Gemini model is selected until it is configured.
- Handle provider errors with a safe rule-based fallback.

## Development sequence
**Phase 1 — Vertical slice:** mock pose messages -> WebSocket -> deterministic squat rep counter -> UI rep display.
**Phase 2 — Persistence:** Supabase migrations, auth/ownership, session lifecycle, save repetitions and session summary.
**Phase 3 — Vision:** MediaPipe camera integration, confidence filtering, data timestamps, calibrated exercises.
**Phase 4 — Hardware:** ESP32 telemetry, calibration, sync, camera-only fallback.
**Phase 5 — Agent:** LLM provider integration, structured tools, context selection, event-based coaching, TTS if available.
**Phase 6 — Demo quality:** error recovery, summary display, test scripts, latency measurement, prerecorded fallback.

## Engineering and testing standards
- Read existing code and dependencies before editing. Make the smallest cohesive change and preserve teammates' work.
- Prefer straightforward Python and TypeScript over introducing extra brokers, queues, frameworks, or training pipelines.
- Use Pydantic for incoming/outgoing backend schemas and type annotations in Python; use shared TypeScript types or schema generation for client contracts.
- Every change to an event shape requires updating `docs/event-contracts.md` and tests.
- Provide unit tests for rep state transitions, duplicate/out-of-order readings, low-confidence frames, sensor absence, and coaching cooldown.
- Provide integration tests with mocked Supabase and mocked LLM. Include one scripted 5-rep demo sequence.
- Avoid blocking the async event loop with synchronous database/LLM calls. Use supported async clients or isolated execution.
- Never fabricate test pass results, live sensor measurements, or model accuracy. Clearly label simulated demo data.
- Add basic logs for session creation, dropped events, rep transitions, agent tool decisions, and database failures, without logging raw private video by default.

## How coding agents should respond
1. Inspect the repository and briefly state which relevant files already exist.
2. State the smallest deliverable being implemented and its test criteria.
3. Implement that slice end-to-end rather than scaffolding many unused services.
4. Run available tests and report actual results, including failures.
5. Summarize changed files, assumptions, and next integration dependencies.
6. Ask for credentials only when needed; never print, commit, or request secret values in chat.

## Open team decisions
- Is MediaPipe running in the React browser or Python service? (Browser recommended.)
- How does ESP32 connect: browser BLE, direct WebSocket, or MQTT bridge? (Choose one for MVP.)
- Which LLM provider and model does the team have access to?
- Will demo accounts use Supabase Auth or an isolated, explicitly secured test identity?
- Which camera angles, exercises, and sensor attachment locations can be reliably demonstrated?
