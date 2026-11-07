# SharkByte 2026

An AI personal gym coach. A phone camera (pose tracking) and an optional ESP32 wearable stream measurements to a
backend that counts reps, gives short form feedback, and keeps the workout history.

> Hackathon project. **Status:** the backend is built and tested with simulated data; nothing has been validated
> with a real camera or real wearable yet, and the AI (LLM) coach is not built. See "What works" below.

## Layout

| Folder | What |
| --- | --- |
| `mobile/` | Expo / React Native app (frontend; see `mobile/AGENTS.md`) |
| `server/` | FastAPI backend: REST + one authenticated WebSocket per workout |
| `supabase/` | Supabase config and SQL migrations (users, workouts, reps, coaching events) |
| `tigerdata/` | Tiger Data (TimescaleDB) migration for the sensor time-series |
| `docs/` | Message contracts, frontend guide, deployment, how to add an exercise |

Two databases with separate jobs: **Supabase** holds accounts and the product data; **Tiger Data** holds the
high-frequency sensor readings and answers time-series questions. Both share the workout's `session_id`.

## Run the backend

```bash
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # macOS/Linux: .venv/bin/python
cp .env.example .env            # then fill in the keys: see SUPABASE_SETUP.md and TIGERDATA_SETUP.md
.venv/Scripts/python -m pytest -q                                # offline tests; need no credentials
.venv/Scripts/python -m scripts.dev_server                       # serves on all interfaces, port 8000
```

Useful checks (each says what it needs): `scripts.check_db`, `scripts.check_tigerdata`, `scripts.e2e_live`,
`scripts.e2e_exercises`, `scripts.e2e_tigerdata`, `scripts.e2e_delete`, `scripts.check_remote --base-url <url>`.
The live ones use a throwaway user and clean up after themselves.

## What works

* Supabase login (JWT) on every API call; ownership enforced; row-level security on the tables.
* Live workouts over a WebSocket: squat rep counting, rule-based coaching, pause / resume / **pain-stop**, ESP32 data
  relay, reconnect takeover, flood and size limits.
* 7 exercises can be logged (squat, push-up, pull-up, deadlift, bench press, lunge, plank). **Only squat is analyzed**
  (reps counted, coached); the others are logged and their data recorded.
* Workout history, per-workout summary, Tiger Data analytics, deleting a workout or a whole account from both databases.
* An optional AI (LLM) coach layer that words the rule engine's feedback and writes workout recaps, with the rules as a
  guaranteed fallback (`docs/AI_COACH.md`). Built and tested offline; **not yet run against a real model**; off by default.

## What does not (yet)

* A verified live LLM run (needs a key), analysis for the non-squat exercises, ESP32 firmware and sensor calibration, anything validated on
  real recordings (the squat thresholds are untuned guesses), a hosted deployment.

## Docs

* `docs/frontend-integration.md`: how the mobile app connects (start here if you build the app)
* `docs/event-contracts.md`: every REST endpoint and WebSocket message
* `SUPABASE_SETUP.md`, `TIGERDATA_SETUP.md`: setting up the databases (with what is verified and what is not)
* `docs/DEPLOYMENT.md`: running it somewhere other than a laptop
* `docs/AI_COACH.md`: the LLM coach layer, its guardrails, and how to enable it
* `docs/adding-an-exercise.md`, `AGENTS.md` (project spec and conventions)
