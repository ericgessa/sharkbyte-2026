# SharkByte 2026

An AI gym coach built from three layers:

- **CV layer (MediaPipe):** pose estimation from the camera turns video into body landmarks and
  joint angles such as knee angles.
- **Rep engine (FastAPI):** the backend counts squat and push-up reps in real time from those angles and
  measures each one: depth, tempo, and left/right asymmetry. A rule-based coach gives feedback.
- **Form IQ (deterministic):** attempts that don't reach the bottom (or are too quick) are not counted and the app
  is told why (`rep_rejected`); the spine wearable's section angles judge each counted rep's back shape (sagging or
  piked hips in push-ups, a rounding lower back in squats). See [`docs/form-iq.md`](docs/form-iq.md).
- **AI layer (Gemini, optional): the reporter.** After the workout it writes a short report: what went well, what
  came up, and one thing to focus on next time. Everything during the workout (counting, rejecting, cues) is
  deterministic and never waits on the model (see [`docs/AI_COACH.md`](docs/AI_COACH.md)).

A spine wearable (an ESP32 with four IMUs down the back, in [`hardware/`](hardware/)) measures upper-back tilt
and how much the upper, mid and lower back each bend. It reaches the `/pose` page over USB (directly, or through
a wireless base station); the page relays its readings to the backend as `sensor_update` messages, which are
stored. The coach doesn't use them yet (see [`hardware/README.md`](hardware/README.md)).

> **Status:** the backend is built and tested against **simulated** data. Nothing has been validated with a
> real camera or wearable yet, MediaPipe pose tracking exists **for the web only** (a developer page,
> `/pose`; see `docs/frontend-integration.md` section 6; never tried with a live camera), the end-of-workout report
> has been run against real Gemini (on simulated workouts), and the mobile app is otherwise still the Expo starter template.

## Repository layout

| Path | What it is |
| --- | --- |
| [`mobile/`](mobile/) | Expo / React Native client (Expo Router, TypeScript). Currently the starter template. |
| [`server/`](server/) | FastAPI backend: REST + WebSocket API, rep counter, rule-based coach, optional LLM layer. |
| [`supabase/`](supabase/) | Supabase config and SQL migrations (users, workouts, reps, coaching events, security rules). |
| [`tigerdata/`](tigerdata/) | Tiger Data (TimescaleDB) migration for the high-frequency sensor readings. |
| [`hardware/`](hardware/) | ESP32 spine wearable: BNO085 + 3x MPU-6050 firmware, ESP-NOW base station, laptop receiver, bench tests, wiring. |
| [`docs/`](docs/) | Message contracts, frontend guide, AI coach, deployment, how to add an exercise, [GRIT Games](docs/grit-games.md). |

Two databases with separate jobs: **Supabase** holds accounts and the product data; **Tiger Data** holds the
sensor time-series and answers time-series questions. Both share the workout's `session_id`.

## How it works

```
 camera ─▶ MediaPipe Pose ─▶ knee angles ─ pose_update ─▶ FastAPI ── rep counter ── rule coach
 (planned)   (planned)                                      │  /ws/workouts/{session_id}      │
                                                            ▼                                  ▼ (optional)
 client ◀── rep_completed / rep_rejected / coaching_feedback / tracking_status / error   (all deterministic)
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
5. The server runs a hysteresis state machine over the knee angles (elbow angles for push-ups). It ignores low-confidence
   frames, drops out-of-order frames, and discards reps faster than 800 ms as noise.
6. Each counted rep produces a `rep_completed` message with `min_angle_deg` (and `joint`), `descent_ms`,
   `ascent_ms`, `duration_ms`, and `max_asymmetry_deg`, and is saved to Supabase.
7. **Coaching:** when a problem repeats (for example depth dropping), the rule-based coach sends a
   `coaching_feedback` cue and saves it. Attempts that don't count get a `rep_rejected` with the reason. A pain
   report always stops technique coaching.
8. **Report:** after `POST /api/workouts/{id}/finish`, `GET /api/workouts/{id}/coach-summary` returns the
   end-of-workout report, written by Gemini when it is enabled, with a deterministic version as the fallback.

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
- **LLM coach** (`COACH_LLM_PROVIDER=gemini` + `GEMINI_API_KEY`): optional and off by
  default. Backend only, never in the mobile app. Details: [`docs/AI_COACH.md`](docs/AI_COACH.md).

Run the tests:

- Supabase login (JWT) on every API call; ownership enforced; row-level security on the tables.
- Live workouts over a WebSocket: squat and push-up rep counting, rule-based coaching, pause / resume / **pain-stop**, ESP32 data
  relay, reconnect takeover, flood and size limits.
- 7 exercises can be logged (squat, push-up, pull-up, deadlift, bench press, lunge, plank). **Squat and push-up are
  analyzed** (reps counted, coached; thresholds untuned, simulated data only); the others are logged and their data
  recorded.
- Workout history, per-workout summary, Tiger Data analytics, deleting a workout or a whole account from both databases.
- An optional Gemini end-of-workout report with a deterministic report as the guaranteed fallback. Run against real
  Gemini on simulated workouts (`python -m scripts.check_report_llm`); live cues never use an LLM.

## Roadmap

- [x] Squat rep counting from knee angles over WebSocket
- [x] Simulated pose stream for demos and tests
- [x] Phase 2: Supabase auth (JWT validation, session ownership) and persistence
- [x] `coaching_feedback` event (rule-based; optional LLM wording)
- [x] Form IQ: `rep_rejected` for attempts that don't count; back-shape cues from the spine wearable
- [x] ESP32 `sensor_update` messages accepted, stored in Tiger Data and used for back-shape coaching
- [x] Spine wearable firmware with calibration, relayed over USB by the `/pose` page (not yet tried end to end on hardware)
- [x] CV layer in the browser: MediaPipe pose capture and joint-angle computation (`/pose`; tested on a still image and a simulated stream, not a live camera)
- [ ] CV layer on phones (native camera + MediaPipe) and a real-camera accuracy check
- [ ] Mobile live rep UI
- [x] Gemini end-of-workout report verified against the real API (simulated workouts)
- [ ] Rep counting and coaching for the exercises other than squat and push-up
- [ ] Tune the squat and push-up thresholds on real recordings (they are untuned guesses)
- [ ] `set_finished` event
- [ ] A hosted deployment ([`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is untested)

```bash
cd server
python -m app.simulation --reps 5
```

- [`docs/frontend-integration.md`](docs/frontend-integration.md): how the mobile app connects (start here if you build the app)
- [`docs/event-contracts.md`](docs/event-contracts.md): every REST endpoint and WebSocket message
- [`SUPABASE_SETUP.md`](SUPABASE_SETUP.md), [`TIGERDATA_SETUP.md`](TIGERDATA_SETUP.md): setting up the databases (with what is verified and what is not)
- [`docs/AI_COACH.md`](docs/AI_COACH.md): the Gemini report, its guardrails, and how to enable it
- [`docs/form-iq.md`](docs/form-iq.md): rejected reps and back-shape judging (deterministic)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md): running it somewhere other than a laptop
- [`docs/adding-an-exercise.md`](docs/adding-an-exercise.md) and [`AGENTS.md`](AGENTS.md) (project spec and conventions)

## License

MIT. See [`LICENSE`](LICENSE).
