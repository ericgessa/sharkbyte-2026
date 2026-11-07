# SharkByte 2026

An AI gym coach built from three layers:

- **CV layer (MediaPipe):** pose estimation from the camera turns video into body landmarks and
  joint angles such as knee angles.
- **Rep engine (FastAPI):** the backend counts squat reps in real time from those angles and
  measures each one: depth, tempo, and left/right asymmetry.
- **AI layer (Gemini API):** turns the per-rep metrics into natural-language coaching feedback.

Sensor input from an ESP32 is also planned.

> **Status: early development.** Phase 1 (rep counting over WebSocket, no auth, in-memory sessions)
> works against simulated data. The MediaPipe and Gemini layers are **planned and not integrated
> yet**, and the mobile app is still the Expo starter template. Persistence and auth (Phase 2) are
> not built. Do not expose the server publicly.

## Repository layout

| Path | What it is |
| --- | --- |
| [`mobile/`](mobile/) | Expo / React Native client (Expo Router, TypeScript). Currently the starter template. |
| [`server/`](server/) | FastAPI backend: REST + WebSocket API and the squat rep counter. |
| [`supabase/`](supabase/) | Postgres schema migration for Phase 2 persistence. Not applied to any project yet. |
| [`docs/event-contracts.md`](docs/event-contracts.md) | Source of truth for REST and WebSocket message formats. |

## How it works

```
 camera ─▶ MediaPipe Pose ─▶ knee angles ─ pose_update ─▶ FastAPI ── RepCounter
 (planned)   (planned)                                      │  /ws/workouts/{session_id}
                                                            ▼
 client ◀── rep_completed / tracking_status / error ────────┤
 client ◀── coaching_feedback (planned) ◀── Gemini API ◀────┘  (planned)
```

1. **Capture (planned):** [MediaPipe](https://ai.google.dev/edge/mediapipe) pose landmarks are
   used to compute left and right knee angles (hip, knee, ankle) and a landmark confidence per
   frame. The event contract already expects the client to send these angles, and to send `null`
   for angles whose landmarks aren't visible.
2. The client creates a session with `POST /api/workouts`.
3. It opens `/ws/workouts/{session_id}` and sends `pose_update` frames (about 30 per second).
4. The server runs a hysteresis state machine over the knee angles. It ignores low-confidence
   frames, drops out-of-order frames, and discards reps faster than 800 ms as noise.
5. Each counted rep produces a `rep_completed` message with `min_knee_angle_deg`, `descent_ms`,
   `ascent_ms`, `duration_ms`, and `max_asymmetry_deg`.
6. **Coaching (planned):** rep metrics are sent to the [Gemini API](https://ai.google.dev/gemini-api/docs),
   which generates feedback (for example, "go deeper" or "left side is lagging") that is delivered
   to the client as a `coaching_feedback` event. The rep count itself stays deterministic and never
   depends on the model.

Full message schemas are in [`docs/event-contracts.md`](docs/event-contracts.md). The Pydantic models
are in `server/app/schemas/events.py`. Change both together. `coaching_feedback` isn't defined in
the contract yet.

## Getting started

### Server

Requires Python 3.10 or newer.

```bash
cd server
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

The API is then at `http://127.0.0.1:8000`. Check `GET /health`, or open `/docs` for the
interactive OpenAPI UI.

Run the tests:

```bash
pytest
```

### Try it without a camera

With the server running, stream simulated squats against it from a second terminal:

```bash
cd server
python -m app.simulation --reps 5
```

This sends **synthetic** pose data. It is not real camera or sensor output. The demo script also
needs the `websockets` package, which `uvicorn[standard]` installs.

### Mobile app

Requires Node.js. See [`mobile/README.md`](mobile/README.md) for Expo details.

```bash
cd mobile
npm install
npx expo start
```

Then press `i` (iOS simulator), `a` (Android emulator), or `w` (web), or scan the QR code with a
development build.

Lint and type-check before committing:

```bash
npx expo lint
npx tsc --noEmit
```

## Configuration

`server/.env.example` lists the Supabase variables planned for Phase 2. **The code does not read
them yet.** When persistence lands, copy it to `server/.env`. Keep `SUPABASE_SECRET_KEY` on the
backend only and never ship it in the mobile app.

The same applies to the Gemini API key. Once the AI layer is added, call Gemini from the server
only, with the key in `server/.env` (for example `GEMINI_API_KEY`), and never embed it in the
mobile app. That variable isn't in `.env.example` yet, so add it when the integration is built.

## Roadmap

- [x] Squat rep counting from knee angles over WebSocket
- [x] Simulated pose stream for demos and tests
- [ ] CV layer: MediaPipe pose capture and knee-angle computation on the client
- [ ] Mobile live rep UI
- [ ] AI layer: Gemini-generated coaching from rep metrics (`coaching_feedback` event)
- [ ] Phase 2: Supabase auth (JWT validation, session ownership) and persistence
- [ ] ESP32 `sensor_update` messages
- [ ] `set_finished` event

## License

MIT. See [`LICENSE`](LICENSE).
