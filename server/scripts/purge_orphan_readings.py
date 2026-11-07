"""Find Tiger Data readings whose workout no longer exists in Supabase, and optionally delete them.

    python -m scripts.purge_orphan_readings                 dry run: lists orphans, deletes nothing
    python -m scripts.purge_orphan_readings --apply         deletes them

Why: Supabase deletes cascade (delete a user in the dashboard and their workouts vanish), but Tiger Data is a
separate database with no link to it, so their sensor readings stay behind. The API's DELETE /api/workouts/{id}
removes both; this tool cleans up after deletions made directly in Supabase (dashboard, SQL, account removal).

A session counts as an orphan only if Supabase does not have it AND its newest reading is older than
--older-than-hours (default 1), as a safety margin. Needs both Supabase and Tiger Data configured in server/.env.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.services.database import DatabaseConfigError, get_client
from app.services.timeseries import TigerDataConfigError, TigerDataError, TigerStore, tigerdata_url


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    p.add_argument("--older-than-hours", type=float, default=1.0)
    args = p.parse_args()

    try:
        supabase = get_client()
        tigerdata_url()
    except (DatabaseConfigError, TigerDataConfigError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    tiger = TigerStore()
    try:
        sessions = tiger.query(
            "select session_id, count(*) as readings, min(time) as first_time, max(time) as last_time "
            "from sensor_readings group by session_id order by max(time)"
        )
        ids = [str(r["session_id"]) for r in sessions]
        known: set[str] = set()
        for i in range(0, len(ids), 100):  # chunked so the request URL stays short
            known |= {r["id"] for r in supabase.table("workout_sessions").select("id").in_("id", ids[i : i + 100]).execute().data}

        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.older_than_hours)
        orphans = [r for r in sessions if str(r["session_id"]) not in known and r["last_time"] < cutoff]
        print(f"{len(sessions)} workouts have readings in Tiger Data; {len(orphans)} are orphans (not in Supabase, older than {args.older_than_hours} h)")
        for r in orphans:
            print(f"  {r['session_id']}  {r['readings']:>7} readings  {r['first_time']:%Y-%m-%d %H:%M} .. {r['last_time']:%H:%M} UTC")
        if not orphans:
            return 0
        if not args.apply:
            print("\nDry run: nothing deleted. Re-run with --apply to delete these readings.")
            return 0
        removed = sum(tiger.delete_session_readings(r["session_id"]) for r in orphans)
        print(f"\nDeleted {removed} readings from {len(orphans)} orphaned workouts.")
        return 0
    except TigerDataError as exc:
        print(f"FAIL  {exc}")
        return 1
    finally:
        tiger.close()


if __name__ == "__main__":
    sys.exit(main())
