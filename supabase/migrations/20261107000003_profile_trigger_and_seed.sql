-- SharkByte schema, part 3/3: profile creation on sign-up, and reference data.

-- Create a profile row whenever a Supabase Auth user is created.
-- SECURITY DEFINER because the auth system, not the user, performs the insert;
-- search_path is emptied so nothing can be hijacked via a shadowing schema.
-- Keep this function trivial: if it raises, the user's sign-up fails.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (id, display_name)
    values (
        new.id,
        left(
            coalesce(
                nullif(btrim(new.raw_user_meta_data ->> 'display_name'), ''),
                nullif(split_part(coalesce(new.email, ''), '@', 1), '')
            ),
            80  -- matches the profiles.display_name length check
        )
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

-- Only the trigger should run this; it must not be callable through the API.
revoke execute on function public.handle_new_user() from public, anon, authenticated;

create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Users that already exist (e.g. created before this migration) get a profile too.
insert into public.profiles (id, display_name)
select u.id, left(nullif(split_part(coalesce(u.email, ''), '@', 1), ''), 80)
from auth.users u
on conflict (id) do nothing;

-- Reference data. Lives in a migration (not seed.sql) so it also reaches the
-- cloud project. Thresholds mirror SquatConfig in server/app/services/rep_counter.py.
insert into public.exercises (name, config) values
    ('squat', '{"standing_deg":160,"descent_start_deg":150,"bottom_deg":110,"bottom_exit_deg":120,"min_rep_ms":800}')
on conflict (name) do nothing;
