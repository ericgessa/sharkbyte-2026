"""Tiger Data (TimescaleDB) access: a SECOND database, separate from Supabase.

Holds high-frequency sensor readings. Supabase remains the source of truth for users, sessions,
completed reps and coaching events; both use the same ``session_id`` UUID but no transaction spans
the two. Everything here is optional: if TIGERDATA_URL is unset or the database is down, the live
workout still runs (see services/ingest.py).

The connection URL contains the database password: it is read from the environment, never logged,
and never included in exception messages.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.schemas.sensors import SensorReading

load_dotenv()
logger = logging.getLogger(__name__)

INSERT_SQL = """
    insert into sensor_readings (time, session_id, source, sensor_id, metric_name, metric_value, metadata)
    values (%s, %s, %s, %s, %s, %s, %s)
    on conflict do nothing
"""
DEFAULT_QUERY_TIMEOUT_MS = 10_000


class TigerDataConfigError(RuntimeError):
    """TIGERDATA_URL missing or unusable. The message never contains the URL."""


class TigerDataError(RuntimeError):
    """A query or insert failed. Carries only the error class name, never connection details."""


def tigerdata_url() -> str:
    url = os.environ.get("TIGERDATA_URL", "").strip()
    if not url:
        raise TigerDataConfigError("TIGERDATA_URL is not set. See TIGERDATA_SETUP.md.")
    if not url.startswith(("postgres://", "postgresql://")):
        raise TigerDataConfigError("TIGERDATA_URL must be a postgres:// service URL from the Tiger Console.")
    if "PASSWORD@" in url or "your-" in url:
        raise TigerDataConfigError("TIGERDATA_URL still contains placeholder values.")
    return url


def is_configured() -> bool:
    try:
        tigerdata_url()
        return True
    except TigerDataConfigError:
        return False


class TigerStore:
    """Insert and query sensor_readings. Blocking (psycopg); call via ``asyncio.to_thread`` from async code."""

    def __init__(self, url: str | None = None, pool_max: int | None = None) -> None:
        self._url = url
        self._pool_max = pool_max or int(os.environ.get("TIGERDATA_POOL_MAX", "4"))
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._url) or is_configured()

    def _get_pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None:
                # min_size=0: no background connection attempts while idle or when the DB is down.
                self._pool = ConnectionPool(
                    self._url or tigerdata_url(),
                    min_size=0,
                    max_size=self._pool_max,
                    timeout=5,  # fail fast: a dead database must not hold a worker thread for long
                    kwargs={"connect_timeout": 5},
                    open=True,
                    name="tigerdata",
                )
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    # ------------------------------------------------------------------ writes

    def insert_readings(self, readings: Sequence[SensorReading]) -> int:
        """Insert a batch in one transaction. Idempotent: duplicates (same session, metric, sensor,
        instant) are skipped, so retrying a batch is safe. Returns the number of rows actually inserted."""
        if not readings:
            return 0
        rows = [
            (r.time, r.session_id, r.source, r.sensor_id, r.metric_name, r.metric_value, Jsonb(r.metadata))
            for r in readings
        ]
        try:
            with self._get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, rows)
                    return max(cur.rowcount, 0)
        except TigerDataConfigError:
            raise
        except Exception as exc:  # psycopg.Error, PoolTimeout, ...
            raise TigerDataError(f"insert failed: {type(exc).__name__}") from None

    # ------------------------------------------------------------------ reads

    def query(self, sql: str, params: Any = None, timeout_ms: int = DEFAULT_QUERY_TIMEOUT_MS) -> list[dict[str, Any]]:
        """Run a read query; rows come back as dicts. A per-query statement timeout bounds slow queries."""
        try:
            with self._get_pool().connection() as conn:
                conn.execute("select set_config('statement_timeout', %s, true)", (str(timeout_ms),))
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())
        except TigerDataConfigError:
            raise
        except Exception as exc:
            raise TigerDataError(f"query failed: {type(exc).__name__}") from None

    def ping(self) -> dict[str, Any]:
        """Connectivity and setup check: server version, TimescaleDB version, is sensor_readings a hypertable."""
        server = self.query("select version() as server")[0]["server"]
        ext = self.query("select extversion from pg_extension where extname = 'timescaledb'")
        is_hypertable = False
        if ext:
            is_hypertable = bool(
                self.query("select 1 from timescaledb_information.hypertables where hypertable_name = 'sensor_readings'")
            )
        return {
            "server": server,
            "timescaledb": ext[0]["extversion"] if ext else None,
            "sensor_readings_is_hypertable": is_hypertable,
        }
