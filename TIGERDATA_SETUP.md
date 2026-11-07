# Tiger Data setup (sensor time-series)

SharkByte uses **two databases with separate jobs**. Supabase keeps the product data; Tiger Data
(TimescaleDB) keeps the high-frequency sensor readings and answers time-series questions about them.
Both refer to the same workout by its `session_id` UUID.

## Verification status (read this first)

| Item | Status |
| --- | --- |
| Everything works with Tiger Data **absent or down** (workout, coaching, Supabase saves) | **Tested** (offline tests simulate both) |
| Existing Supabase behavior unchanged | **Tested**: all earlier tests pass, and the live Supabase end-to-end run still passes (18/18 checks) |
| Sensor validation, clock anchoring, batching, retry/backoff, buffer overflow | **Tested** with a mock sink |
| Insert code sends one batched, parameterized, idempotent statement and never leaks the URL | **Tested** against a mock psycopg pool; a real driver run against an unreachable host confirmed a 5 s fail-fast and no password in the error |
| Migration and analytics SQL are valid PostgreSQL syntax | **Checked** with the PostgreSQL parser |
| **Migration applied to a real Tiger Data service** | **NOT DONE.** No service exists yet and I cannot create the account for you |
| **Analytics SQL returns correct numbers on real TimescaleDB** | **NOT VERIFIED.** The live tests are written (`tests/test_tigerdata_live.py`, with known-answer data) but have never been run |
| Real connection, hypertable, dedupe index, `time_bucket` / `regr_slope` behavior | **NOT VERIFIED** until you run the steps in section 4 |

Treat any failure in section 4 as a bug in this setup and tell the backend dev.

## What you must do by hand

1. Create a Tiger Cloud account and a service (section 1).
2. Put its **service URL** in `server/.env` as `TIGERDATA_URL` (section 2).
3. Run the migration and the verification commands (sections 3 and 4).

## 1. Create a Tiger Data project

1. Sign up / log in at <https://console.cloud.timescale.com> (Tiger Cloud, powered by TimescaleDB).
2. Create a new **service**. Choose the plain Postgres + TimescaleDB (time-series) type, a region near you, and
   the smallest/free-trial size that is offered. The console's labels change over time; what you need is a
   running service with the TimescaleDB extension (TimescaleDB **2.20 or newer** is required for the
   `CREATE TABLE ... WITH (tsdb.hypertable)` syntax; new Tiger Cloud services are current).
3. Save the generated **password** straight away (the console typically shows it once; store it in a password
   manager).
4. Open the service's connection details and copy the **service URL**. It looks like
   `postgres://tsdbadmin:PASSWORD@HOST:PORT/tsdb?sslmode=require`. The database name is `tsdb` and Tiger Cloud's
   default SSL mode is `require`.

## 2. Environment variables

In `server/.env` (git-ignored; never commit it, never paste it in chat):

```
TIGERDATA_URL=postgres://tsdbadmin:YOUR_PASSWORD@xxxxxxxx.xxxxxx.tsdb.cloud.timescale.com:PORT/tsdb?sslmode=require
```

Optional: `TIGERDATA_POOL_MAX=4` (connection pool size). The existing `SUPABASE_*` variables are untouched.
The URL contains the database password, so it is **backend only** and must never appear in the mobile app. The
server never logs it or includes it in error messages. If `TIGERDATA_URL` is unset the app runs normally and
sensor readings are simply not stored.

Install the new Python dependencies (`psycopg`, `psycopg-pool`) if you haven't:
`.venv/Scripts/python -m pip install -r requirements-dev.txt` (from `server/`).

## 3. Apply the migration

From `server/`:

```bash
.venv/Scripts/python -m scripts.apply_tigerdata_migrations --dry-run
.venv/Scripts/python -m scripts.apply_tigerdata_migrations
```

This runs `tigerdata/migrations/001_sensor_readings.sql` once (applied versions are recorded in a
`schema_migrations` table, so re-running is safe). Alternatively paste the SQL file into the Tiger Console's SQL
editor, but pick one method per service.

The migration creates the `sensor_readings` hypertable:

| Column | Meaning |
| --- | --- |
| `time` (timestamptz) | when the measurement happened, in **server time** (see "Clocks" below) |
| `session_id` (uuid) | the Supabase `workout_sessions.id`; **no foreign key** because it is a different database |
| `source` | `camera`, `flex_sensor` or `imu` |
| `sensor_id` | hardware id such as `esp32-01` (empty for the camera) |
| `metric_name` | `left_knee_angle`, `right_knee_angle`, `flex_value`, `imu_pitch`, `imu_roll`, `imu_yaw`, `accel_x/y/z` |
| `metric_value` | the number |
| `metadata` (jsonb) | camera rows carry `{"confidence": 0.0-1.0}` so analytics can ignore unreliable frames |

Design choices: 1-day chunks; columnstore (compression) layout segmented by `(session_id, metric_name)` and ordered
by time, with Tiger Data's automatic columnstore policy; a **unique index** on
`(session_id, metric_name, sensor_id, time)` so a retried batch cannot create duplicates (unique indexes on a
hypertable must include `time`); and an index on `(session_id, metric_name, time desc)` for per-session queries.

## 4. Verify (please run these)

All from `server/`:

```bash
# 1. Connection + schema (read-only)
.venv/Scripts/python -m scripts.check_tigerdata

# 2. The real test suite: known-answer data checks every analytics query on real TimescaleDB
#    (writes under random session ids and deletes them). PowerShell: $env:RUN_TIGERDATA_TESTS=1
RUN_TIGERDATA_TESTS=1 .venv/Scripts/python -m pytest tests/test_tigerdata_live.py -v

# 3. The whole two-database flow (throwaway user, simulated camera + ESP32 data, cleans up)
.venv/Scripts/python -m scripts.e2e_tigerdata

# Same, but leave the data in place so you can show it (prints the session id)
.venv/Scripts/python -m scripts.e2e_tigerdata --keep
.venv/Scripts/python -m scripts.check_tigerdata --session <session-uuid>
```

You can also look at the rows yourself in the Tiger Console's SQL editor:

```sql
select hypertable_name, num_chunks from timescaledb_information.hypertables;
select metric_name, source, count(*) from sensor_readings
where session_id = '<session-uuid>' group by 1, 2 order by 1;
```

## How the two databases interact

```
 phone camera (MediaPipe) ──pose_update──┐
 ESP32 via phone (BLE) ────sensor_update─┤   ONE authenticated WebSocket (unchanged)
                                         ▼
                          FastAPI: rep counter + coach  (live loop, never waits on a database)
                           │                     │
        once per rep /     │                     │  every measurement, buffered in memory,
        coaching event     ▼                     ▼  written in batches of up to 500 / 1 s
                      Supabase               Tiger Data
              users, auth, sessions,      sensor_readings hypertable
              reps, coaching events       (camera angles, flex, IMU)
                           └────── same session_id UUID ──────┘
                                         │
              GET /api/workouts/{id}/analytics: Supabase facts + Tiger Data SQL summaries
              (the compact context the AI coach receives; never the raw stream)
```

* **Supabase is the source of truth** for who did what: the workout session, completed reps (with their
  start/end time), coaching events. It never receives the raw measurement stream.
* **Tiger Data holds the raw measurements** and answers "how did the movement change over time" in SQL.
* **No distributed transactions.** The two writes are independent: a rep is saved to Supabase when it completes;
  sensor readings are batched to Tiger Data. They are tied only by `session_id`. The backend writes readings only
  for a session it already verified in Supabase (the WebSocket checks ownership first).
* **Tiger Data is optional at runtime.** `add()` on the in-memory buffer never blocks or raises. If Tiger Data is
  down, batches stay buffered (up to 20,000 readings, oldest dropped first and counted) and retry with
  exponential backoff (1 s up to 30 s). Rep counting, coaching feedback and the Supabase saves are unaffected, and
  buffered readings are flushed when the workout finishes (waiting at most 1 s if an outage is already known).
* **Rep time windows** (`started_at` / `ended_at`, server time) are saved inside each Supabase rep's `metrics`
  JSON. That is how Tiger Data queries slice a session into reps.
* **Clocks.** Camera frames and each ESP32 have unrelated clocks. The first message of each source anchors it to
  server time, which keeps spacing between that source's samples exact. Alignment *between* sources is only as
  good as network latency (tens of ms), so join them by time bucket (see the correlation query), never by
  equal timestamps. A source whose clock jumps by more than 60 s (e.g. an ESP32 reboot) is re-anchored.
* **ESP32 transport.** AGENTS.md left this open. The app accepts `sensor_update` on the *same* authenticated
  workout WebSocket, so a phone relaying ESP32 data over Bluetooth needs no extra endpoint or credentials. Message
  format: `docs/event-contracts.md`.
* Sensor values are stored **as measured** (raw flex ADC counts, degrees, g). They are not anatomical angles
  until calibrated.

## New API endpoints

* `GET /api/workouts/{id}/analytics`: `supabase` stats + `timeseries` summaries (`available: false` with a reason
  if Tiger Data is not configured or unreachable).
* `GET /api/workouts/{id}/readings?start=&end=&metric=&bucket_seconds=&limit=`: measurements in a time window,
  optionally downsampled in SQL (max 5,000 rows).

## Example SQL for squat analysis

Replace `<sid>` with a session UUID. Knee angle: 180 = straight leg, smaller = deeper. These mirror what
`app/services/analytics.py` runs. They have **not** been run against a live service yet (see status table).

```sql
-- Average / deepest / top knee angle for the workout, ignoring unreliable landmark frames
select count(*) as samples, round(avg(metric_value)::numeric, 1) as avg_deg,
       min(metric_value) as deepest_deg, max(metric_value) as top_deg
from sensor_readings
where session_id = '<sid>' and metric_name in ('left_knee_angle', 'right_knee_angle')
  and coalesce((metadata->>'confidence')::float8, 1) >= 0.6;

-- Range of motion across the set, in 5-second buckets (is it shrinking as the user tires?)
select time_bucket('5 seconds', time) as bucket,
       min(metric_value) as deepest, max(metric_value) - min(metric_value) as range_of_motion
from sensor_readings
where session_id = '<sid>' and metric_name = 'left_knee_angle'
group by 1 order by 1;

-- Per-rep depth and range. Get the windows from Supabase:
--   select rep_number, metrics->>'started_at', metrics->>'ended_at' from repetitions where session_id = '<sid>';
with reps(rep_number, t0, t1) as (values
  (1, '2026-11-07T12:00:00Z'::timestamptz, '2026-11-07T12:00:02Z'::timestamptz),
  (2, '2026-11-07T12:00:04Z'::timestamptz, '2026-11-07T12:00:06Z'::timestamptz))
select reps.rep_number, min(r.metric_value) as deepest, max(r.metric_value) - min(r.metric_value) as range_of_motion
from reps join sensor_readings r
  on r.session_id = '<sid>' and r.metric_name = 'left_knee_angle' and r.time between reps.t0 and reps.t1
group by 1 order by 1;

-- Descent/ascent speed in degrees per second (negative = bending)
select time, metric_value,
       (metric_value - lag(metric_value) over w) / nullif(extract(epoch from time - lag(time) over w), 0) as deg_per_sec
from sensor_readings
where session_id = '<sid>' and metric_name = 'left_knee_angle'
window w as (order by time) order by time;

-- Seconds spent below 110 degrees (the "bottom" of the squat)
select count(*) filter (where avg_deg < 110) as seconds_at_bottom
from (select time_bucket('1 second', time) as b, avg(metric_value) as avg_deg
      from sensor_readings where session_id = '<sid>' and metric_name = 'left_knee_angle' group by 1) s;

-- Does the wearable flex sensor agree with the camera? (joined by 100 ms bucket, not exact timestamps)
with cam as (select time_bucket('100 milliseconds', time) as b, avg(metric_value) as knee
             from sensor_readings where session_id = '<sid>' and metric_name = 'left_knee_angle' group by 1),
     flex as (select time_bucket('100 milliseconds', time) as b, avg(metric_value) as flex
              from sensor_readings where session_id = '<sid>' and metric_name = 'flex_value' group by 1)
select count(*) as paired_buckets, corr(cam.knee, flex.flex) as correlation from cam join flex using (b);

-- First and last angle of the workout (TimescaleDB first()/last() aggregates)
select first(metric_value, time) as start_deg, last(metric_value, time) as end_deg
from sensor_readings where session_id = '<sid>' and metric_name = 'left_knee_angle';

-- Is old data being compressed? (columnstore)
select chunk_name, is_compressed from timescaledb_information.chunks where hypertable_name = 'sensor_readings';
```

## Demonstrating Tiger Data to hackathon judges

I don't have the MLH "Best Use of Tiger Data" judging criteria; check the challenge page and adapt. These are
the honest talking points and a demo that can all be shown:

1. **Why time-series.** A 30 fps pose stream plus ESP32 flex/IMU is ~100+ rows/second per workout: the wrong
   shape for the product database, the right shape for a hypertable.
2. **Run `python -m scripts.e2e_tigerdata --keep`.** It drives the real app with simulated camera and ESP32
   data and prints each check: Supabase holds the 5 reps and coaching event; Tiger Data holds the raw readings
   under the same `session_id`.
3. **Show the SQL** in the Tiger Console: `time_bucket` range-of-motion buckets, the per-rep query, and the
   flex-vs-camera correlation. Point out that the AI coach receives a ~1 KB summary from these queries, not
   the stream.
4. **Show resilience.** Stop or misconfigure `TIGERDATA_URL` and run a workout: counting and coaching still work
   (the offline tests cover this too).
5. **Show the TimescaleDB features actually used:** hypertable with 1-day chunks, columnstore layout and policy,
   `time_bucket`, `first()/last()`, regression aggregates (`regr_slope`) for per-rep trends, and a dedupe unique
   index. The `timescaledb_information` queries above show chunks and compression.

Not built (so don't claim them): continuous aggregates, retention policies, real sensor calibration, and any
LLM calling these summaries yet (the AI coach itself isn't built).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `CONFIG ERROR: TIGERDATA_URL is not set` | Not in `server/.env`, or run from the wrong directory (run from `server/`) |
| `still contains placeholder values` | You pasted the template with `PASSWORD` in it |
| `PoolTimeout` / `query failed: PoolTimeout` | Wrong host/port, service paused or still starting, or your network blocks it; the URL itself is never printed |
| `sensor_readings is not a hypertable` / `relation does not exist` | Migration not applied to *this* service: run `apply_tigerdata_migrations` |
| `syntax error ... tsdb.hypertable` during migration | The service's TimescaleDB is older than 2.20; create a new service or upgrade |
| `password authentication failed` | Wrong password in the URL (special characters in the password must be URL-encoded) |
| Workouts work but `analytics` says `available: false` | Tiger Data not configured or unreachable; the `reason` field says which |
| Few or no rows after a workout | Readings flush every second or at 500 rows and on finish; check server logs for "Tiger Data write failed" |
| Server log: "sensor buffer full" | Tiger Data has been unreachable long enough to overflow 20,000 buffered readings |
