-- The coach's end-of-workout report, saved once per workout.
--
-- Additive only: nothing in the existing tables is changed. Apply it like the earlier migrations (paste into the
-- dashboard SQL Editor, or `db push`); see SUPABASE_SETUP.md. Until it is applied the app still works: the report is
-- generated when asked for and kept in the server's memory only.
--
-- Access model (same as the workout tables): signed-in users can READ the report of their own workouts; only the
-- FastAPI backend (secret key) writes them, so a client cannot forge a "coach" report. Deleting a workout deletes its
-- report with it.

create table public.workout_reports (
    session_id uuid primary key references public.workout_sessions (id) on delete cascade,
    -- The report text shown in the History tab (a few sentences).
    summary    text not null check (char_length(btrim(summary)) > 0 and char_length(summary) <= 4000),
    -- 'agent': written by the AI coach (Gemini). 'rules': the deterministic report (AI coach off, unavailable, failed a
    -- safety check, or the user reported pain). A 'rules' report is replaced by an 'agent' one later if the AI coach
    -- becomes available.
    source     text not null check (source in ('agent', 'rules')),
    -- Which model wrote it (null for 'rules').
    model      text check (model is null or char_length(model) <= 120),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.workout_reports enable row level security;

revoke all on public.workout_reports from anon, authenticated;
grant select on public.workout_reports to authenticated;
grant all on public.workout_reports to service_role;

create policy "workout_reports: read own"
    on public.workout_reports for select to authenticated
    using (exists (
        select 1 from public.workout_sessions s
        where s.id = workout_reports.session_id
          and s.user_id = (select auth.uid())
    ));
