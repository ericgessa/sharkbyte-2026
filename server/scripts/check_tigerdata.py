"""Verify the Tiger Data connection and (optionally) look at the readings stored for one workout.

    python -m scripts.check_tigerdata                       connection + schema check (read-only)
    python -m scripts.check_tigerdata --session <uuid>      also summarize that workout's stored readings

Exit code: 0 ok, 1 problem, 2 not configured. Never prints the connection URL.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from app.services import analytics
from app.services.timeseries import TigerDataConfigError, TigerDataError, TigerStore, tigerdata_url


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--session", type=UUID, help="workout session UUID to summarize")
    args = p.parse_args()

    try:
        tigerdata_url()
    except TigerDataConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    store = TigerStore()
    try:
        info = store.ping()
        print(f"ok    connected: {info['server'].split(',')[0]}")
        if info["timescaledb"]:
            print(f"ok    TimescaleDB extension {info['timescaledb']}")
        else:
            print("FAIL  TimescaleDB extension is not installed on this database")
            return 1
        if info["sensor_readings_is_hypertable"]:
            print("ok    sensor_readings is a hypertable")
        else:
            print("FAIL  sensor_readings is not a hypertable -> run: python -m scripts.apply_tigerdata_migrations")
            return 1

        total = store.query("select count(*) as n, max(time) as latest from sensor_readings")[0]
        print(f"ok    {total['n']} readings stored" + (f", latest {total['latest'].isoformat()}" if total["latest"] else ""))

        if args.session:
            o = analytics.sensor_overview(store, args.session)
            print(f"\nSession {args.session}: {o['readings']} readings")
            for name, m in o["metrics"].items():
                print(f"  {name:<18} {m['source']:<12} n={m['samples']:<6} avg={m['avg']}  min={m['min']}  max={m['max']}")
            if o["readings"]:
                print(f"  knee angle (confident frames): {analytics.angle_stats(store, args.session)}")
    except TigerDataError as exc:
        print(f"FAIL  {exc}  -> check TIGERDATA_URL, that the service is running, and your network")
        return 1
    finally:
        store.close()
    print("\nTiger Data check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
