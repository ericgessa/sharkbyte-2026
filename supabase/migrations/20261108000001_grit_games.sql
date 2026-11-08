-- GRIT Games: results of Rep Racer rounds (solo, personal best, 1v1), and finished 1v1 races.
--
-- Additive only: nothing in the existing workout tables is changed or deleted.
-- Apply it like the first three migrations (paste into the dashboard SQL Editor, or `db push`); see SUPABASE_SETUP.md.
--
-- Access model (same as the workout tables): signed-in users can READ their own rows; every write is done by the
-- FastAPI backend with the secret key, so a client cannot forge a score, a rep count or a race result.
-- No raw video is stored anywhere: only counts, durations and rep timestamps.

-- A finished 1v1 race. Live rooms (lobby, countdown, racing) live in the server's memory and are only written
-- here once the race is over.
create table public.game_rooms (
    id          uuid primary key default gen_random_uuid(),
    code        text        not null check (code ~ '^[A-Z0-9]{4,8}$'),
    host_id     uuid        references auth.users (id) on delete set null,
    exercise    text        not null check (exercise in ('squat', 'push-up')),
    target_reps integer     not null check (target_reps between 1 and 100),
    time_limit_s integer    not null check (time_limit_s between 10 and 600),
    winner_id   uuid        references auth.users (id) on delete set null,
    -- how the race ended: someone reached the target, the timer ran out, or a player left/dropped out
    end_reason  text        not null check (end_reason in ('target_reached', 'time_up', 'forfeit')),
    started_at  timestamptz not null,
    finished_at timestamptz not null default now(),
    constraint finished_after_started check (finished_at >= started_at)
);

create table public.game_room_players (
    room_id      uuid    not null references public.game_rooms (id) on delete cascade,
    user_id      uuid    not null references auth.users (id) on delete cascade,
    display_name text    not null check (char_length(display_name) between 1 and 40),
    valid_reps   integer not null default 0 check (valid_reps >= 0),
    -- milliseconds from the start signal to this player's last counted rep (null: no reps)
    last_rep_ms  integer check (last_rep_ms is null or last_rep_ms >= 0),
    placement    integer not null check (placement in (1, 2)),
    result       text    not null check (result in ('win', 'loss', 'draw', 'forfeit')),
    primary key (room_id, user_id)
);

create table public.game_results (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid    not null references auth.users (id) on delete cascade,
    game        text    not null check (game in ('rep_racer')),
    mode        text    not null check (mode in ('solo', 'personal_best', 'multiplayer')),
    exercise    text    not null check (exercise in ('squat', 'push-up')),
    -- Only camera-tracked rounds are stored. Keyboard/mouse practice rounds are never saved, so they can never
    -- count towards history or personal bests.
    input_source text   not null default 'camera' check (input_source = 'camera'),
    -- Counted by the server's rep counter (never taken from the client).
    valid_reps  integer not null check (valid_reps >= 0),
    score       integer not null default 0 check (score >= 0),
    duration_ms integer not null check (duration_ms >= 0),
    target_reps integer check (target_reps is null or target_reps between 1 and 100),
    time_limit_s integer check (time_limit_s is null or time_limit_s between 10 and 600),
    -- the target was reached in time
    completed   boolean not null default false,
    -- e.g. {"Good rep!": 7, "Squat deeper.": 2}: how often each form message appeared.
    form_feedback jsonb not null default '{}'::jsonb check (jsonb_typeof(form_feedback) = 'object'),
    -- Milliseconds from the start of the round to each counted rep: drives the personal-best ghost runner.
    rep_times_ms jsonb  not null default '[]'::jsonb check (jsonb_typeof(rep_times_ms) = 'array'),
    workout_session_id uuid references public.workout_sessions (id) on delete set null,
    room_id     uuid    references public.game_rooms (id) on delete set null,
    created_at  timestamptz not null default now()
);

create index idx_game_results_user_created on public.game_results (user_id, created_at desc);
create index idx_game_results_best         on public.game_results (user_id, game, exercise, completed, duration_ms);
create index idx_game_room_players_user    on public.game_room_players (user_id);
-- One saved result per workout: saving the same round twice returns the first result instead of duplicating it.
create unique index uq_game_results_workout on public.game_results (workout_session_id) where workout_session_id is not null;

-- ------------------------------------------------------------------ row level security

alter table public.game_rooms        enable row level security;
alter table public.game_room_players enable row level security;
alter table public.game_results      enable row level security;

revoke all on public.game_rooms, public.game_room_players, public.game_results from anon, authenticated;
grant select on public.game_rooms, public.game_room_players, public.game_results to authenticated;
grant all on public.game_rooms, public.game_room_players, public.game_results to service_role;

-- Whether the caller played in a race. SECURITY DEFINER so the policy on game_room_players can ask about
-- game_room_players without recursing into itself.
create function public.is_game_room_member(room uuid)
returns boolean
language sql stable security definer
set search_path = ''
as $$
    select exists (
        select 1 from public.game_room_players p
        where p.room_id = room and p.user_id = (select auth.uid())
    );
$$;
revoke all on function public.is_game_room_member(uuid) from public, anon;
grant execute on function public.is_game_room_member(uuid) to authenticated, service_role;

create policy "game_results: read own"
    on public.game_results for select to authenticated
    using (user_id = (select auth.uid()));

create policy "game_rooms: read as a player"
    on public.game_rooms for select to authenticated
    using (public.is_game_room_member(id));

create policy "game_room_players: read in my races"
    on public.game_room_players for select to authenticated
    using (public.is_game_room_member(room_id));
