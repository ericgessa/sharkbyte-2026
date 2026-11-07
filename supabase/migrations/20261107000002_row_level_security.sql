-- SharkByte schema, part 2/3: Row Level Security and grants.
--
-- Access model
--   * Signed-in users (role "authenticated") can READ their own profile, sessions,
--     repetitions and coaching events, and read exercise definitions.
--   * Users can update only their own display_name.
--   * Everything else (creating sessions, saving reps/coaching events, finishing
--     workouts) is done by the FastAPI backend with the secret key, which bypasses
--     RLS. That keeps rep counts and coaching records server-authored: a client
--     cannot forge them.
--   * "anon" (not signed in) gets nothing.
-- RLS alone is not enough: the table privileges below matter just as much.

alter table public.profiles         enable row level security;
alter table public.exercises        enable row level security;
alter table public.workout_sessions enable row level security;
alter table public.repetitions      enable row level security;
alter table public.coaching_events  enable row level security;

-- Start from zero privileges for client roles, then grant back only what is needed.
revoke all on public.profiles, public.exercises, public.workout_sessions,
              public.repetitions, public.coaching_events
    from anon, authenticated;

grant select on public.profiles, public.exercises, public.workout_sessions,
                public.repetitions, public.coaching_events
    to authenticated;
grant update (display_name) on public.profiles to authenticated;

-- The backend's secret key maps to service_role. Grant explicitly rather than
-- depending on a project's default privileges.
grant all on public.profiles, public.exercises, public.workout_sessions,
             public.repetitions, public.coaching_events
    to service_role;

-- (select auth.uid()) is evaluated once per statement instead of once per row.

create policy "profiles: read own"
    on public.profiles for select to authenticated
    using (id = (select auth.uid()));

create policy "profiles: update own"
    on public.profiles for update to authenticated
    using (id = (select auth.uid()))
    with check (id = (select auth.uid()));

create policy "exercises: read all"
    on public.exercises for select to authenticated
    using (true);

create policy "workout_sessions: read own"
    on public.workout_sessions for select to authenticated
    using (user_id = (select auth.uid()));

create policy "repetitions: read own"
    on public.repetitions for select to authenticated
    using (exists (
        select 1 from public.workout_sessions s
        where s.id = repetitions.session_id
          and s.user_id = (select auth.uid())
    ));

create policy "coaching_events: read own"
    on public.coaching_events for select to authenticated
    using (exists (
        select 1 from public.workout_sessions s
        where s.id = coaching_events.session_id
          and s.user_id = (select auth.uid())
    ));
