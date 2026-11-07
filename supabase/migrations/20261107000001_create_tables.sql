-- SharkByte schema, part 1/3: tables, constraints, indexes.
-- RLS/policies are in 20261107000002, profile trigger + seed in 20261107000003.

create table public.profiles (
    id           uuid primary key references auth.users (id) on delete cascade,
    display_name text check (display_name is null or char_length(display_name) <= 80),
    created_at   timestamptz not null default now()
);

create table public.exercises (
    id     bigint generated always as identity primary key,
    name   text not null unique check (char_length(btrim(name)) > 0),
    -- Exercise-specific thresholds/measurements (e.g. squat depth angles).
    config jsonb not null default '{}'::jsonb check (jsonb_typeof(config) = 'object')
);

create table public.workout_sessions (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid   not null references public.profiles (id) on delete cascade,
    -- restrict: an exercise definition in use cannot be deleted.
    exercise_id bigint not null references public.exercises (id) on delete restrict,
    started_at  timestamptz not null default now(),
    ended_at    timestamptz,
    total_reps  integer not null default 0 check (total_reps >= 0),
    status      text not null default 'active' check (status in ('active', 'completed')),
    constraint ended_after_started check (ended_at is null or ended_at >= started_at),
    -- A completed session must have an end time; an active one must not.
    constraint status_matches_ended_at check ((status = 'completed') = (ended_at is not null))
);

create table public.repetitions (
    id          uuid primary key default gen_random_uuid(),
    session_id  uuid    not null references public.workout_sessions (id) on delete cascade,
    rep_number  integer not null check (rep_number > 0),
    -- Joint-angle metrics, e.g. {"min_knee_angle_deg": 85.0, "duration_ms": 1716}.
    metrics     jsonb   not null default '{}'::jsonb check (jsonb_typeof(metrics) = 'object'),
    -- Scale not decided yet (team to agree, e.g. 0-100), so no range check.
    form_score  numeric,
    created_at  timestamptz not null default now(),
    -- Also serves as the (session_id, rep_number) lookup index, and makes
    -- backend upserts on this pair idempotent.
    unique (session_id, rep_number)
);

create table public.coaching_events (
    id          uuid primary key default gen_random_uuid(),
    session_id  uuid not null references public.workout_sessions (id) on delete cascade,
    rep_number  integer check (rep_number is null or rep_number > 0),
    issue_code  text,   -- detected form issue, e.g. 'reduced_depth'
    message     text not null check (char_length(btrim(message)) > 0),
    action      text,   -- what the coach did, e.g. 'send_feedback'
    created_at  timestamptz not null default now()
);

create index idx_sessions_user_started on public.workout_sessions (user_id, started_at desc);
create index idx_sessions_exercise     on public.workout_sessions (exercise_id);
create index idx_coaching_session      on public.coaching_events (session_id, created_at);
