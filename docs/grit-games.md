# GRIT Games

Exercise-controlled games inside the app, reachable from **Home → GRIT Games** (and the **Games** link in the web
header, or `/games`). They use the exercises the server can count today: **squats** and **push-ups**.

| Game | What it is |
| --- | --- |
| **Rep Racer** | "Race your friends or beat your personal best." **Solo Goal** (target reps and a time limit), **Live 1v1** (room code, two webcams), **Personal Best** (race a ghost of your own best recorded attempt) and **Local 2-Player** (two people, one camera, one computer). |

(GRIT Bird, a body-position-controlled flying game, was built and then removed: it was too buggy and too hard to play.)

## How it fits together

```
camera (MediaPipe, in the browser)            nothing but joint ANGLES leaves the device, never video
        │  PoseCamera.onFrame (existing)
        ▼
ExerciseController  (mobile/src/games/exercise-controller.ts)   ← shared by Racer solo and Personal Best
   ├─ body position 0..1, instantly, locally
   ├─ form hints, tracking loss ("Move into camera view.")
   └─ angles ──► the EXISTING workout WebSocket ──► the server's rep counter (the same one workouts use)
                                                         └─ rep_completed ──► one ExerciseRepEvent per counted rep
```

* **One pose pipeline.** There is a single MediaPipe instance (the shared detector); games use the existing
  `PoseCamera`, `angles.ts`, `PoseStreamer` and `WorkoutSocket`. A round is a normal workout, so it also appears in
  workout history and the AI recap works on it.
* **The game never counts reps.** The server does, so a rep on screen is a rep in the database. Duplicate rep
  messages are ignored by rep number; half reps, bouncing and standing-still never count (existing counter rules).
* **Rendering is separate from rules.** `racer-engine.ts` is pure and unit-tested; the screens in
  `mobile/src/components/games/` glue it to the camera.
* **You do not have to be in frame to start.** The race starts once the camera is on; the 3-2-1 countdown is the time
  to get into position. Reps only count after the server sees you standing (squat) or at the top (push-up).

### Local 2-Player (two people, one camera)

MediaPipe looks for two bodies (`numPoses` is raised to 2 only while this camera is on). Players are told apart by
position, not by the detector's order: **Player 1 is whoever is on the left of the screen as you see yourselves
(the preview is a mirror), Player 2 the right** (`mobile/src/games/people.ts`); one person plays the side of the
screen they stand on, so stepping out and back in keeps your side. Each person's angles go to the server's rep counter
through their own workout, so reps are counted exactly as in every other mode, and `FaceOff` (in `racer-engine.ts`)
applies the same winner rules as the online 1v1. Both workouts belong to the signed-in account, so they are **deleted
afterwards**: nothing from a local face-off is saved. It needs both people fully in view (stand 2-3 m back, about a
metre apart); two overlapping bodies can confuse the tracker.

## Verified vs practice

Only camera-tracked rounds are saved (Rep Racer's keyboard practice mode, Space = one rep, is labelled PRACTICE and never saved) (`input_source = 'camera'` is a database constraint). They are scored by the
server from the workout its own counter scored:

* `valid_reps`, the rep timestamps (the ghost), and "target reached in time" come from the saved repetitions, never from
  the client's claim. The completion time is the moment the server counted the target-th rep.
* A client can only save `solo` or `personal_best` rounds; `multiplayer` results are written by the server itself.
* A result can only be attached to the caller's own, finished workout of the same exercise; the same workout can only be
  saved once (a second save returns the first result).

## REST (all need `Authorization: Bearer <Supabase access token>`)

| Call | Purpose |
| --- | --- |
| `POST /api/games/results` | Save a finished camera round. Body: `game` (`rep_racer`), `mode` (`solo`\|`personal_best`), `exercise`, `workout_session_id` (finish the workout first), `duration_ms`, `target_reps` + `time_limit_s`, `form_feedback` (`{message: count}`). Returns the stored result (201; 200 if that workout was already saved) with the `personal_best` comparison. |
| `GET /api/games/results?game=&exercise=&limit=` | Your history, newest first. |
| `GET /api/games/results/{id}` | One of your results (404 if it is not yours). |
| `GET /api/games/best?game=&exercise=&target_reps=` | The ghost: your fastest finished race with that target, else your attempt with the most reps. `best` is `null` on a first attempt. |
| `POST /api/games/rooms` | Create a 1v1 room: `exercise`, `target_reps` (1-100), `time_limit_s` (10-600), `display_name`. Returns a 6-letter `code`. |
| `POST /api/games/rooms/{code}/join` | Take the second seat. 409 `room_full` / `race_in_progress`; 404 for an unknown code. |
| `GET /api/games/rooms/{code}` | Current state (members only; 404 otherwise). |
| `POST /api/games/rooms/{code}/leave` | Leave (a forfeit during a race). |
| `GET /api/games/races/{room_id}` | A finished, saved race (players only). |
| AI recap | The existing `GET /api/workouts/{workout_session_id}/coach-summary` (every game result carries its `workout_session_id`). Gemini/Claude if configured, else the rule-based text; one request when the player asks, never per rep. |

Errors: 401 no/invalid token, 404 "not found or not yours" (the same answer for both), 409 conflicts (unfinished workout,
wrong exercise, room full), 422 validation, 429 rate limit, 503 `GRIT Games tables are missing` until the migration is applied.

## 1v1 WebSocket `WS /ws/games/rooms/{code}`

First message must be `{"schema_version":1,"type":"auth","access_token":"…"}` and the user must already be in the room
(joined over REST). Close codes: `4401` bad token, `4404` no such room / not yours, `4410` your seat was taken over by a
newer connection (a reload or second tab), `1009` message too big.

Client → server (all `schema_version: 1`, unknown fields rejected):

| `type` | Fields |
| --- | --- |
| `pose_update` | `sequence` (strictly increasing), `timestamp_ms`, `data` (same joint angles as a workout `pose_update`). No `session_id`. |
| `ready` | `ready: true\|false` |
| `rematch` | after a race, both players must send it |
| `leave` | leave on purpose |

Server → client:

| `type` | Meaning |
| --- | --- |
| `room_state` | The full room: `status` (`lobby`\|`countdown`\|`racing`\|`finished`), `players` (`name`, `ready`, `connected`, `reps`, `tracking`), `starts_in_ms`, `remaining_ms`, `elapsed_ms`, `winner_id`, `end_reason`, `results`, `you`. Times are **relative**, so two laptops never need synchronised clocks. |
| `race_started` | The countdown ended. |
| `rep_counted` | `user_id`, `reps` (one per rep the server counted). |
| `coaching_feedback` | Private to that player (same rules coach as workouts). |
| `race_finished` | `winner_id`, `end_reason` (`target_reached`\|`time_up`\|`forfeit`), `results`. |
| `error` | `code` (`invalid_message`, `rate_limited`, …) and `detail`. |

**Server authority.** The server owns room membership, the countdown (3 s), the start time, the race timer, rep counting
(from the angles, with the same counter as workouts), scoring, the winner and the saved result. There is no "I did a
rep" message to forge or replay; frames before the start or after the finish are ignored; a replayed frame cannot count
twice. Winner: first to the target; at time-up the most reps, then the earlier last rep; nothing counted is a draw. A
dropped player has 15 seconds to reconnect to the same seat (their progress is kept); leaving or timing out is a forfeit.
Limits: 120 pose messages/s and 5 control messages/s per connection, 16 KB messages, room creation shares the workout-start limit (20 a minute per user).
Finished races are saved once (`game_rooms`, `game_room_players`, and a `game_results` row plus a workout for each player).
A failure to save never reaches the players.

### Racing a friend on another laptop

Both laptops must reach **the same running backend**. `localhost` on two machines is two different servers. Run the API
on a network both can reach (or a tunnel/hosted URL), start the app with `EXPO_PUBLIC_API_URL=https://that-address`, and
note a browser page on `https` can only call an `https`/`wss` API. The 1v1 screen prints the server address in use and
warns when it is `localhost`. Until two real laptops have been used against one shared endpoint, treat two-device racing as
**tested with simulated players only** (see below).

## Database

`supabase/migrations/20261108000001_grit_games.sql` (additive; nothing existing is changed). **Apply it** (Dashboard →
SQL Editor → paste → Run, same as the first three) before results can be saved:

* `game_results`: one row per saved camera round (game, mode, exercise, valid reps, score, duration, target, time limit,
  completed, `form_feedback`, `rep_times_ms` for the ghost, links to the workout and the race). Unique per workout.
* `game_rooms` / `game_room_players`: a finished 1v1 race and each player's line. Live rooms are in the server's memory only.
* RLS: signed-in users can only read their own results and the races they played; every write is done by the backend with
  the secret key, so a client cannot forge a score. No video or images are stored anywhere.

## Run it

```bash
# 1. backend (from server/; see the README for the venv)
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 2. app (from mobile/)
npx expo start --web --port 8081
# open http://localhost:8081, sign in, then Home -> GRIT Games
```

## Tests

```bash
server/.venv/Scripts/python -m pytest -q                 # includes test_game_rooms.py and test_games_api.py
cd mobile && npm run test:games && npm run test:pose     # game logic, controller, room client, wiring (fake network)
cd server && python -m scripts.e2e_games                 # LIVE, after the migration: real Supabase, two throwaway users
```

What the automated tests prove: the rep-to-game contract, the physics and scoring, the race rules and the winner logic,
duplicate and out-of-order protection, reconnects, who can see or write what, request validation and rate limits, and that
a missing migration gives a clear 503. They use **simulated** pose data and fake stores.

What they do not prove (needs a person, a camera, or two computers): MediaPipe accuracy on a real body (push-up
thresholds are still untuned guesses), the webcam permission flow, and a race
across two machines. Desktop browsers are the supported target.
