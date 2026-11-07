"""Verify the backend can reach Supabase and the migrations were applied.

    python -m scripts.check_db        (run from server/)

Read-only: it only counts rows. Exit code 0 = all good, 1 = problem, 2 = config.
"""

from __future__ import annotations

import sys

from postgrest.exceptions import APIError

from app.services.database import DatabaseConfigError, get_client

TABLES = ("profiles", "exercises", "workout_sessions", "repetitions", "coaching_events")


def main() -> int:
    try:
        client = get_client()
    except DatabaseConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    failed = False
    for table in TABLES:
        try:
            res = client.table(table).select("*", count="exact").limit(0).execute()
            print(f"ok    {table:<18} {res.count} rows")
        except APIError as exc:
            failed = True
            hint = ""
            if exc.code in {"PGRST205", "42P01"}:
                hint = "  -> table missing: apply the migrations (see SUPABASE_SETUP.md)"
            elif exc.code in {"42501", "PGRST301"} or "JWT" in (exc.message or ""):
                hint = "  -> permission/key problem: is SUPABASE_SECRET_KEY the secret key?"
            print(f"FAIL  {table:<18} {exc.code}: {exc.message}{hint}")
        except Exception as exc:  # network, DNS, bad URL...
            failed = True
            print(f"FAIL  {table:<18} {type(exc).__name__}: {exc}")
            print("      -> check SUPABASE_URL and your network connection")
            break

    if not failed:
        names = {r["name"] for r in client.table("exercises").select("name").execute().data}
        if "squat" not in names:
            failed = True
            print("FAIL  exercises has no 'squat' row: the seed part of the migrations did not run")

    print("\nDatabase check FAILED" if failed else "\nDatabase check passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
