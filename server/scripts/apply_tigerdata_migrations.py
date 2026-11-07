"""Apply tigerdata/migrations/*.sql to the database in TIGERDATA_URL, once each, in filename order.

    python -m scripts.apply_tigerdata_migrations [--dry-run]       (run from server/)

Applied versions are recorded in a schema_migrations table, so re-running is safe. Each file runs in
its own transaction. Never prints the connection URL.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from app.services.timeseries import TigerDataConfigError, tigerdata_url

MIGRATIONS = Path(__file__).resolve().parents[2] / "tigerdata" / "migrations"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be applied, change nothing")
    dry = p.parse_args().dry_run

    try:
        url = tigerdata_url()
    except TigerDataConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print(f"No migration files in {MIGRATIONS}")
        return 1

    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            conn.execute("create table if not exists schema_migrations (version text primary key, applied_at timestamptz not null default now())")
            conn.commit()
            done = {r[0] for r in conn.execute("select version from schema_migrations")}
            pending = [f for f in files if f.name not in done]
            print(f"{len(done)} already applied, {len(pending)} pending")
            for f in pending:
                if dry:
                    print(f"  would apply {f.name}")
                    continue
                print(f"  applying {f.name} ...")
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (version) values (%s)", (f.name,))
                conn.commit()
            print("Dry run complete" if dry else "Migrations up to date")
    except psycopg.Error as exc:
        # str(exc) can include the host and database but never the password.
        print(f"DATABASE ERROR: {type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
