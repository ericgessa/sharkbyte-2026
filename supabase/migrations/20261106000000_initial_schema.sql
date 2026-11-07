-- SharkByte initial schema. NOT YET APPLIED to any Supabase project.
-- Not wired into the server yet (Phase 2).
--
-- Access model: the FastAPI server writes with the secret/service key (bypasses RLS)
-- after validating the user's JWT and session ownership. Signed-in users may only
-- READ their own rows directly. There are deliberately no insert/update/delete
-- policies or grants for clients.

create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    display_name text,
    created_at timestamptz not null default now()
);

create table public.exercises (
    id bigint generated always as identity primary key,
    name text unique not null,
    config jsonb not null default '{}'::jsonb
);

create table public.workout_sessions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    exercise_id bigint not null references public.exercises(id),
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    total_reps integer not null default 0,
    status text not null default 'active' check (status in ('active', 'completed'))
);

create table public.repetitions (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references public.workout_sessions(id) on delete cascade,
    rep_number integer not null,
    metrics jsonb not null default '{}'::jsonb,
    form_score numeric,
    created_at timestamptz not null default now(),
    unique (session_id, rep_number)
);

create table public.coaching_events (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references public.workout_sessions(id) on delete cascade,
    rep_number integer,
    issue_code text,            -- AGENTS.md calls this issue_code (the pasted plan said "issue")
    message text not null,
    action text,
    created_at timestamptz not null default now()
);

create index idx_sessions_user on public.workout_sessions (user_id, started_at desc);
create index idx_reps_session on public.repetitions (session_id, rep_number);
create index idx_coaching_session on public.coaching_events (session_id, created_at);

-- Row Level Security -------------------------------------------------------
alter table public.profiles enable row level security;
alter table public.exercises enable row level security;
alter table public.workout_sessions enable row level security;
alter table public.repetitions enable row level security;
alter table public.coaching_events enable row level security;

-- Table privileges: RLS alone is not enough. Clients get SELECT only.
revoke all on public.profiles, public.exercises, public.workout_sessions,
              public.repetitions, public.coaching_events from anon, authenticated;
grant select on public.profiles, public.exercises, public.workout_sessions,
                public.repetitions, public.coaching_events to authenticated;

create policy "own profile" on public.profiles
    for select to authenticated using (id = (select auth.uid()));

create policy "exercises readable" on public.exercises
    for select to authenticated using (true);

create policy "own sessions" on public.workout_sessions
    for select to authenticated using (user_id = (select auth.uid()));

create policy "own repetitions" on public.repetitions
    for select to authenticated using (
        exists (select 1 from public.workout_sessions s
                where s.id = repetitions.session_id and s.user_id = (select auth.uid()))
    );

create policy "own coaching events" on public.coaching_events
    for select to authenticated using (
        exists (select 1 from public.workout_sessions s
                where s.id = coaching_events.session_id and s.user_id = (select auth.uid()))
    );

-- Seed ---------------------------------------------------------------------
-- Detection thresholds mirror SquatConfig defaults in server/app/services/rep_counter.py.
insert into public.exercises (name, config) values
    ('squat', '{"standing_deg":160,"descent_start_deg":150,"bottom_deg":110,"bottom_exit_deg":120,"min_rep_ms":800}');
