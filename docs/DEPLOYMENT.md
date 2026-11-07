# Running the backend somewhere other than your laptop

**Status: nothing here has been deployed or built.** The `Dockerfile` has never been built (Docker would not start
on the dev machine) and no hosting account has been used. What *is* verified: the app starts with
`uvicorn app.main:app`, the offline test suite passes with no credentials, and `/health` answers. Treat the steps
below as a checklist to try, and fix what breaks.

## Do you need this?

On the same Wi-Fi, the laptop dev server is enough (`python -m scripts.dev_server`, see `frontend-integration.md`).
You need a public address when a teammate is on another network, when the event Wi-Fi blocks device-to-device
traffic, for an Android release build (it blocks plain `http://`), or for judging when the laptop may be asleep.

## Option A: a tunnel to your laptop (fastest, demo only)

A tunnel tool (Cloudflare Tunnel, ngrok, ...) gives your running dev server a public `https://` address. Nothing is
installed or configured for this yet. It exposes your laptop's API to the internet while it runs: every route except
`/health`, `/` and `/api/exercises` needs a valid login, but only do it for the duration of a demo. The app then
uses `https://...` for REST and `wss://...` for the WebSocket.

## Option B: a container host (Render, Fly.io, Railway, ...)

1. Build and test the image locally if you can:
   ```bash
   docker build -t sharkbyte-server server/
   docker run --rm --env-file server/.env -p 8000:8000 sharkbyte-server
   curl http://localhost:8000/health
   ```
2. Create a web service from `server/Dockerfile` (build context: `server/`). It listens on `$PORT` (default 8000).
3. Set **secrets as the platform's environment variables**, never in the image or git:

   | Variable | Required | Notes |
   | --- | --- | --- |
   | `SUPABASE_URL` | yes | project URL |
   | `SUPABASE_SECRET_KEY` | yes | backend only |
   | `SUPABASE_PUBLISHABLE_KEY` | no | not used by the server |
   | `TIGERDATA_URL` + `TIGERDATA_PASSWORD` | no | without them the app runs and sensor data is not stored |
   | `CORS_ORIGINS` | no | default `*` is fine for a native app; list your web origin(s) if you ship Expo web |

4. Health check path: `/health`.
5. Point the mobile app's `EXPO_PUBLIC_API_URL` at the `https://` address the platform gives you.

## Constraints that must hold

* **Exactly one instance and one worker.** Live workout state (rep counters, coach, sensor buffer, the one-socket-
  per-session rule) is in memory. Two instances or several worker processes would split one workout's frames
  between them and miscount. Do not enable autoscaling or multiple workers. (Scaling out would need that state moved
  to shared storage; that is not built.)
* **WebSockets must be supported** by the host, with idle timeouts longer than a workout, or at least than the
  gaps between frames. Workouts that lose the socket reconnect and continue.
* **Restarts lose only in-flight state.** Saved reps are in Supabase, and a reconnecting app resumes from them. A
  restart mid-workout is survivable but can lose a rep that was being counted at that moment.
* **Run the Tiger Data migration once** from a machine with the credentials:
  `python -m scripts.apply_tigerdata_migrations` (it is not part of the image).
* Supabase auth: nothing to change for a new backend address; the mobile app talks to Supabase directly for login.

## Before you point real users at it

Rotate any keys that ever appeared in chat or a screenshot; keep `server/.env` out of git (it is ignored); and read
the "Not built" list in `README.md` (no monitoring, no per-IP limits, no multi-instance support).
