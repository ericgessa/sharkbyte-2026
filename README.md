# SharkByte 2026

An AI gym coach built from three layers:

- **CV layer (MediaPipe):** pose estimation from the camera turns video into body landmarks and
  joint angles such as knee angles.
- **Rep engine (FastAPI):** the backend counts squat reps in real time from those angles and
  measures each one: depth, tempo, and left/right asymmetry. A rule-based coach gives feedback.
- **AI layer (LLM, optional):** words that feedback in natural language and writes workout recaps.
  The rep count and the decision to coach stay deterministic and never depend on the model. The
  providers implemented are **Gemini** (the team's plan) and Anthropic's Claude, chosen with one setting
  (see [`docs/AI_COACH.md`](docs/AI_COACH.md)). Neither has been run against a real key yet.

Sensor input from an ESP32 is accepted by the backend (`sensor_update`) and stored; the firmware and
sensor calibration are not built yet.

> **Status:** the backend is built and tested against **simulated** data. Nothing has been validated with a
> real camera or wearable yet, MediaPipe pose tracking exists **for the web only** (a developer page,
> `/pose`; see `docs/frontend-integration.md` section 6; never tried with a live camera), the AI layer has
> **never been run against a real model**, and the mobile app is otherwise still the Expo starter template.

## Repository layout

| Path | What it is |
| --- | --- |
| [`mobile/`](mobile/) | Expo / React Native client (Expo Router, TypeScript). Currently the starter template. |
| [`server/`](server/) | FastAPI backend: REST + WebSocket API, rep counter, rule-based coach, optional LLM layer. |
| [`supabase/`](supabase/) | Supabase config and SQL migrations (users, workouts, reps, coaching events, security rules). |
| [`tigerdata/`](tigerdata/) | Tiger Data (TimescaleDB) migration for the high-frequency sensor readings. |
| [`docs/`](docs/) | Message contracts, frontend guide, AI coach, deployment, how to add an exercise. |

Two databases with separate jobs: **Supabase** holds accounts and the product data; **Tiger Data** holds the
sensor time-series and answers time-series questions. Both share the workout's `session_id`.

## How it works

```
 camera ─▶ MediaPipe Pose ─▶ knee angles ─ pose_update ─▶ FastAPI ── rep counter ── rule coach
 (planned)   (planned)                                      │  /ws/workouts/{session_id}      │
                                                            ▼                                  ▼ (optional)
 client ◀── rep_completed / coaching_feedback / tracking_status / error ◀─────────── LLM wording (off by default)
                                                            │
                                  reps + coaching events ──▶ Supabase      raw readings ──▶ Tiger Data
```

1. **Capture (web only so far):** [MediaPipe](https://ai.google.dev/edge/mediapipe) pose landmarks are
   used to compute knee, elbow and hip angles and a landmark confidence per frame (`mobile/src/pose/`). The event contract already expects the client to send these angles, and to send `null`
   for angles whose landmarks aren't visible.
2. The user signs in with Supabase; every `/api/*` call carries their access token.
3. The client creates a workout with `POST /api/workouts`.
4. It opens `/ws/workouts/{session_id}`, sends an `auth` message first, then `pose_update` frames
   (about 30 per second).
5. The server runs a hysteresis state machine over the knee angles. It ignores low-confidence
   frames, drops out-of-order frames, and discards reps faster than 800 ms as noise.
6. Each counted rep produces a `rep_completed` message with `min_knee_angle_deg`, `descent_ms`,
   `ascent_ms`, `duration_ms`, and `max_asymmetry_deg`, and is saved to Supabase.
7. **Coaching:** when a problem repeats (for example depth dropping), the rule-based coach sends a
   `coaching_feedback` cue and saves it. If the optional LLM layer is enabled, it may reword the cue
   or stay quiet; if it fails, the rule message is used. A pain report always stops technique coaching.

Full message schemas are in [`docs/event-contracts.md`](docs/event-contracts.md). The Pydantic models
are in `server/app/schemas/events.py`. Change both together.

## Getting started

### Server

Requires Python 3.11 or newer.

```bash
cd server
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # then fill it in; see SUPABASE_SETUP.md and TIGERDATA_SETUP.md
uvicorn app.main:app --reload
```

The API is then at `http://127.0.0.1:8000`. Check `GET /health`, or open `/docs` for the
interactive OpenAPI UI. To let a phone or teammate on the same network reach it, run
`python -m scripts.dev_server` instead: it listens on all interfaces and prints the URLs to use.
(With the Windows Store build of Python, `--reload` can keep serving stale code; see that script's notes.)

Run the tests (offline, no credentials needed):

```bash
pytest
```

### Try it without a camera

The server needs Supabase configured (login and storage), so the simulated stream signs in as a throwaway
user. The one-command check runs a full workout end to end and cleans up after itself:

```bash
cd server
python -m scripts.e2e_live
```

To stream simulated squats to a running server with a token you already have:
`python -m app.simulation --reps 5 --token <access_token>`.

This sends **synthetic** pose data. It is not real camera or sensor output. The demo script also
needs the `websockets` package, which `uvicorn[standard]` installs.

Other checks (each says what it needs; the live ones use throwaway users and clean up): `scripts.check_db`,
`scripts.check_tigerdata`, `scripts.check_rls`, `scripts.e2e_exercises`, `scripts.e2e_tigerdata`,
`scripts.e2e_delete`, `scripts.e2e_account`, `scripts.check_remote --base-url <url>`.

### Mobile app

Requires Node.js. See [`mobile/README.md`](mobile/README.md) for Expo details.

```bash
cd mobile
npm install
npx expo start
```

Then press `i` (iOS simulator), `a` (Android emulator), or `w` (web), or scan the QR code with a
development build. To connect it to the backend, follow
[`docs/frontend-integration.md`](docs/frontend-integration.md).

Lint and type-check before committing:

```bash
npx expo lint
npx tsc --noEmit
```

## Configuration

`server/.env.example` lists every setting; copy it to `server/.env` (git-ignored). The backend reads:

- **Supabase** (`SUPABASE_URL`, `SUPABASE_SECRET_KEY`): required. Keep the secret key on the backend only and
  never ship it in the mobile app. Setup: [`SUPABASE_SETUP.md`](SUPABASE_SETUP.md).
- **Tiger Data** (`TIGERDATA_URL`, `TIGERDATA_PASSWORD`): optional; without it workouts work and sensor readings are
  simply not stored. Setup: [`TIGERDATA_SETUP.md`](TIGERDATA_SETUP.md).
- **LLM coach** (`COACH_LLM_PROVIDER=gemini` + `GEMINI_API_KEY`, or `anthropic` + `ANTHROPIC_API_KEY`): optional and off by
  default. Backend only, never in the mobile app. Details: [`docs/AI_COACH.md`](docs/AI_COACH.md).

## What works

- Supabase login (JWT) on every API call; ownership enforced; row-level security on the tables.
- Live workouts over a WebSocket: squat rep counting, rule-based coaching, pause / resume / **pain-stop**, ESP32 data
  relay, reconnect takeover, flood and size limits.
- 7 exercises can be logged (squat, push-up, pull-up, deadlift, bench press, lunge, plank). **Only squat is analyzed**
  (reps counted, coached); the others are logged and their data recorded.
- Workout history, per-workout summary, Tiger Data analytics, deleting a workout or a whole account from both databases.
- An optional LLM coach layer with the rules as a guaranteed fallback. Built and tested offline; **not yet run
  against a real model**.

## Roadmap

- [x] Squat rep counting from knee angles over WebSocket
- [x] Simulated pose stream for demos and tests
- [x] Phase 2: Supabase auth (JWT validation, session ownership) and persistence
- [x] `coaching_feedback` event (rule-based; optional LLM wording)
- [x] ESP32 `sensor_update` messages accepted and stored (firmware and calibration still to do)
- [x] CV layer in the browser: MediaPipe pose capture and joint-angle computation (`/pose`; tested on a still image and a simulated stream, not a live camera)
- [ ] CV layer on phones (native camera + MediaPipe) and a real-camera accuracy check
- [ ] Mobile live rep UI
- [ ] AI layer verified against a real model (needs a Gemini API key; the Gemini provider is built but unrun)
- [ ] Rep counting and coaching for the exercises other than squat
- [ ] Tune the squat thresholds on real recordings (they are untuned guesses)
- [ ] `set_finished` event
- [ ] A hosted deployment ([`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is untested)

## Docs

- [`docs/frontend-integration.md`](docs/frontend-integration.md): how the mobile app connects (start here if you build the app)
- [`docs/event-contracts.md`](docs/event-contracts.md): every REST endpoint and WebSocket message
- [`SUPABASE_SETUP.md`](SUPABASE_SETUP.md), [`TIGERDATA_SETUP.md`](TIGERDATA_SETUP.md): setting up the databases (with what is verified and what is not)
- [`docs/AI_COACH.md`](docs/AI_COACH.md): the LLM coach layer, its guardrails, and how to enable it
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md): running it somewhere other than a laptop
- [`docs/adding-an-exercise.md`](docs/adding-an-exercise.md) and [`AGENTS.md`](AGENTS.md) (project spec and conventions)

## License

MIT. See [`LICENSE`](LICENSE).
