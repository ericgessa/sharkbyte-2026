# Supabase setup (Phase 1)

Database + auth foundation for SharkByte. The API routes and WebSocket now use it: sessions and reps
are saved in Supabase and requests are authenticated with Supabase access tokens (see
`docs/event-contracts.md`). One command checks the whole path against your project:
`python -m scripts.e2e_live` (run from `server/`; uses a throwaway user and cleans up).

## Verification status (read this first)

| Item | Status |
| --- | --- |
| Migration SQL is syntactically valid PostgreSQL | **Checked** with a PostgreSQL parser |
| Python database module: unit tests (offline, fake client) | **Passing** (`pytest`) |
| Migrations applied to the cloud project `sharkbyte-2026` | **Done** on 2026-11-07, by pasting the three files into the dashboard SQL Editor (not via the CLI) |
| RLS policies, profile trigger, constraints behaving as intended | **Checked live** with a rolled-back test: owner sees only own rows, a second user sees none, `anon` blocked, client writes blocked, profile trigger fires, bad data rejected |
| Backend connected to the project (`check_db`) | **Passing** (all 5 tables reachable, `squat` seeded) |
| `db_example` end to end (throwaway user, trigger, create/fetch session, ownership check, cleanup) | **Passing** |
| `supabase-py` accepting the new `sb_secret_...` key format | **Confirmed** for reads, auth admin and inserts |

Because the migrations were applied through the SQL Editor, the CLI's migration history for this project
is empty. Don't run `db push --remote` against it later without first reconciling (a re-run would try to
re-create existing objects). Use new migration files for future changes and apply them one way only.

Behavior tests were run once by hand against the live project. They are not an automated test in the repo.

## What you must do by hand

| # | Where | Action |
| --- | --- | --- |
| 1 | supabase.com dashboard | Create project `sharkbyte-gym-coach`, choose a region near you, set a **database password** and save it in a password manager |
| 2 | Dashboard > Settings > API Keys | Copy the **Project URL**, **publishable key** and **secret key** |
| 3 | Your machine | Create `server/.env` (git-ignored) and paste the values |
| 4 | Terminal | `npx supabase@3.0.0 login`, `link`, and `db push --remote` (below). Or paste the SQL files into the dashboard SQL Editor |
| 5 | Dashboard > Authentication | Decide on email confirmation. For a hackathon demo you may turn "Confirm email" off so test sign-ups work instantly |
| 6 | Terminal | Run the verification commands |

Nobody else, including Claude, can do these for you; they need your Supabase account.

## 1. Create the cloud project

1. Sign in at <https://supabase.com/dashboard> and click **New project**.
2. Name it `sharkbyte-gym-coach`, pick a region, and generate/enter a strong database password.
   Store it in a password manager; it is only needed for `db push`, not by the app.
3. Wait for provisioning to finish. The project **ref** is the 20-letter id in the URL
   (`https://supabase.com/dashboard/project/<ref>`) and the subdomain of your project URL.

## 2. Keys and environment variables

Dashboard > **Settings > API Keys**:

| Key | Looks like | Who may hold it |
| --- | --- | --- |
| Project URL | `https://<ref>.supabase.co` | anyone |
| Publishable key | `sb_publishable_...` | **frontend and backend** (low privilege) |
| Secret key | `sb_secret_...` | **backend only.** Bypasses Row Level Security |

Older projects may show legacy `anon` / `service_role` JWT keys instead. `service_role` is the
legacy equivalent of the secret key; the same rules apply. Supabase is phasing the legacy keys out.

```bash
cp server/.env.example server/.env
```

Edit `server/.env` and fill in `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY`.

Rules:

* `server/.env` is git-ignored. Confirm with `git check-ignore -v server/.env`. Never commit it, paste
  it in chat, or print it in logs.
* The secret key must never appear in `mobile/` or any frontend code, nor in an `EXPO_PUBLIC_*`
  variable (those are embedded in the app bundle).
* If a secret key leaks, rotate it in Settings > API Keys immediately.

## 3. Install Python dependencies

```bash
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux
```

## 4. Apply the migrations

The schema lives in `supabase/migrations/` and must be applied in filename order:

1. `20261107000001_create_tables.sql`: tables, constraints, indexes
2. `20261107000002_row_level_security.sql`: RLS, grants, policies
3. `20261107000003_profile_trigger_and_seed.sql`: profile-on-signup trigger, `squat` exercise

### Option A: Supabase CLI to the cloud project (recommended)

The CLI runs through `npx` (Node 20+); nothing to install. From the repo root:

```bash
npx supabase@3.0.0 login
npx supabase@3.0.0 link --project-ref <your-project-ref>
npx supabase@3.0.0 db push --remote --dry-run
npx supabase@3.0.0 db push --remote
```

`link` and `db push` may prompt for the database password from step 1. **In CLI 3.x, `db push` targets the
*local* database unless you pass `--remote`.** Forgetting it will not touch your cloud project.

### Option B: paste into the dashboard

Dashboard > **SQL Editor** > New query. Paste and run each of the three files, in order, one at a time.
This works but skips the CLI's migration history, so later `db push` runs may try to re-apply them.
Pick one method per project.

### Option C: local stack (needs Docker running)

```bash
npx supabase@3.0.0 start        # first run downloads several GB of images
npx supabase@3.0.0 status       # prints local URL and keys for server/.env
npx supabase@3.0.0 db reset     # re-applies all migrations from scratch
npx supabase@3.0.0 stop
```

Local Studio (the dashboard) is shown by `status`. Use the local URL and keys in `server/.env` while
developing against it. Keep local and cloud values in separate env files so they are not mixed up.

## 5. Verify the connection

From `server/`:

```bash
.venv/Scripts/python -m scripts.check_db
```

Expected: five `ok` lines (one per table) and `Database check passed`. It is read-only.

End-to-end example (creates a throwaway auth user, a session, reads it back, deletes the user):

```bash
.venv/Scripts/python -m scripts.db_example
```

This writes to the project in `server/.env`, so run it against a dev project. It also proves the
profile trigger works, because the throwaway user must get a profile row automatically.

Unit tests (offline) and the opt-in live test:

```bash
.venv/Scripts/python -m pytest                                  # offline, no credentials needed
RUN_SUPABASE_TESTS=1 .venv/Scripts/python -m pytest tests/test_database.py   # live; PowerShell: $env:RUN_SUPABASE_TESTS=1
```

### Using the module from FastAPI later

```python
from app.services.database import create_workout_session, get_workout_session

# user_id must come from the validated JWT, never from the request body.
session = create_workout_session(user_id, "squat")
same = get_workout_session(session["id"], user_id)       # None if it belongs to someone else
```

The client is synchronous. Call these from plain `def` endpoints, or wrap them in
`asyncio.to_thread(...)` inside `async` code and WebSocket handlers, so a slow query cannot stall rep
counting.

## 6. Inspect tables in the dashboard

* **Table Editor**: browse `profiles`, `exercises`, `workout_sessions`, `repetitions`,
  `coaching_events`. After applying migrations `exercises` should contain one row, `squat`.
  Each table shows whether RLS is enabled and which policies exist.
* **Authentication > Users**: users you created. Creating one should add a matching row in `profiles`.
* **SQL Editor**: ad-hoc queries, e.g. `select * from public.workout_sessions order by started_at desc;`.
  The editor runs as a privileged role, so it ignores RLS and does not prove your policies work.

## Design summary

| Table | Written by | Readable by signed-in user |
| --- | --- | --- |
| `profiles` | trigger on sign-up; user may edit `display_name` only | own row |
| `exercises` | migrations | all rows |
| `workout_sessions` | backend | own rows |
| `repetitions` | backend | rows of own sessions |
| `coaching_events` | backend | rows of own sessions |

Clients cannot insert or modify sessions, reps or coaching events directly. That keeps rep counts and
coaching records server-authored, so a modified client cannot forge workout history. The backend uses the
secret key, which bypasses RLS, so `database.py` always filters on `user_id` explicitly.

Not decided yet: the scale of `repetitions.form_score` (so no range check), and whether `status` needs
values beyond `active` / `completed`.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `CONFIG ERROR: Missing SUPABASE_URL...` | `server/.env` missing, or run from the wrong directory. Run from `server/` |
| `still contains the placeholder values` | You copied `.env.example` but did not edit it |
| `holds a publishable key` / `legacy anon key` | Wrong key in `SUPABASE_SECRET_KEY`. Use the **secret** key |
| `PGRST205` / `42P01` "table not found" | Migrations not applied to *this* project. Re-check step 4 and which project `.env` points at |
| `Invalid API key`, `401` | Key copied wrongly, or from another project; if you rotated keys, update `.env` |
| `42501 permission denied` | Key is not the secret/service key, or migration 2 did not run |
| `getaddrinfo failed` / timeouts | Wrong `SUPABASE_URL` or no network |
| `db push` says nothing to push but cloud is empty | You forgot `--remote` (it pushed to local) |
| `db push` asks for a password / fails to connect | Use the *database* password from project creation (reset it in Settings > Database) |
| Migration says relation/trigger already exists | Part was applied before (e.g. via SQL Editor). Don't mix methods; for a scratch project reset it |
| Sign-up fails after migration 3 | The `handle_new_user` trigger raised. Check the Auth logs; the function must stay trivial |
| `Cannot connect to the Docker daemon` (Option C) | Start Docker Desktop and wait until it says it is running |
| `exercises has no 'squat' row` | Migration 3 did not run (the seed is part of it) |
