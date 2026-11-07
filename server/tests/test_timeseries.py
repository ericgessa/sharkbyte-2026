"""TigerStore against a MOCK psycopg pool: verifies what is sent to the database and how errors are handled.
(Real-database behavior is covered by the opt-in live tests in test_tigerdata_live.py.)"""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.schemas.sensors import SensorReading
from app.services import timeseries as ts

SID = uuid4()
T = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
SECRET_URL = "postgres://tsdbadmin:S3cr3tPassw0rd@abc123.tsdb.cloud.timescale.com:5432/tsdb?sslmode=require"


class FakeCursor:
    def __init__(self, conn, rows=None):
        self.conn, self.rows, self.rowcount = conn, rows or [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        self.conn.executemany_calls.append((sql, list(rows)))
        self.rowcount = self.conn.rowcount_to_report

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows=None, rowcount=0):
        self.executemany_calls, self.executed, self.rows = [], [], rows or []
        self.rowcount_to_report = rowcount

    def cursor(self, row_factory=None):
        return FakeCursor(self, self.rows)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class FakePool:
    def __init__(self, conn=None, error=None):
        self.conn, self.error, self.closed = conn or FakeConn(), error, False

    @contextmanager
    def connection(self):
        if self.error:
            raise self.error
        yield self.conn

    def close(self):
        self.closed = True


def store_with(pool):
    s = ts.TigerStore(url=SECRET_URL)
    s._pool = pool
    return s


def reading(i, session=SID):
    return SensorReading(time=T.replace(microsecond=i), session_id=session, source="camera", sensor_id="",
                         metric_name="left_knee_angle", metric_value=90.0 + i, metadata={"confidence": 0.9})


# ---- configuration ---------------------------------------------------------


def test_url_validation_messages_never_contain_the_url(monkeypatch):
    monkeypatch.delenv("TIGERDATA_URL", raising=False)
    with pytest.raises(ts.TigerDataConfigError, match="not set"):
        ts.tigerdata_url()
    assert ts.is_configured() is False

    monkeypatch.setenv("TIGERDATA_URL", "mysql://secret-user:SecretPw@host/db")
    with pytest.raises(ts.TigerDataConfigError) as exc:
        ts.tigerdata_url()
    assert "SecretPw" not in str(exc.value)

    monkeypatch.setenv("TIGERDATA_URL", "postgres://tsdbadmin:PASSWORD@host:5432/tsdb")
    with pytest.raises(ts.TigerDataConfigError, match="placeholder"):
        ts.tigerdata_url()

    monkeypatch.setenv("TIGERDATA_URL", SECRET_URL)
    assert ts.tigerdata_url() == SECRET_URL and ts.is_configured() is True


# ---- batch insert ----------------------------------------------------------


def test_batch_insert_sends_all_rows_in_one_call_with_session_id():
    conn = FakeConn(rowcount=3)
    n = store_with(FakePool(conn)).insert_readings([reading(i) for i in range(3)])
    assert n == 3
    ((sql, rows),) = conn.executemany_calls  # ONE batched call, not one call per reading
    assert "insert into sensor_readings" in sql and "on conflict do nothing" in sql
    assert len(rows) == 3
    time_, session_id, source, sensor_id, metric, value, meta = rows[0]
    assert (time_, session_id, source, sensor_id, metric, value) == (T.replace(microsecond=0), SID, "camera", "", "left_knee_angle", 90.0)
    assert isinstance(meta, Jsonb)
    assert {r[1] for r in rows} == {SID}  # every row carries the workout's session UUID


def test_insert_uses_parameters_not_string_formatting():
    conn = FakeConn()
    evil = SensorReading(time=T, session_id=SID, source="camera", sensor_id="x'); drop table sensor_readings;--",
                         metric_name="left_knee_angle", metric_value=1.0)
    store_with(FakePool(conn)).insert_readings([evil])
    ((sql, rows),) = conn.executemany_calls
    assert "drop table" not in sql and rows[0][3].startswith("x')")  # travels as data


def test_empty_batch_does_not_touch_the_database():
    pool = FakePool(error=RuntimeError("must not connect"))
    assert store_with(pool).insert_readings([]) == 0


def test_duplicates_skipped_by_the_database_are_not_counted():
    conn = FakeConn(rowcount=2)  # the DB reports only 2 of 5 rows were new
    assert store_with(FakePool(conn)).insert_readings([reading(i) for i in range(5)]) == 2


def test_failures_become_a_tigerdata_error_without_connection_details():
    secret = RuntimeError(f"could not connect using {SECRET_URL}")
    with pytest.raises(ts.TigerDataError) as exc:
        store_with(FakePool(error=secret)).insert_readings([reading(0)])
    assert "S3cr3tPassw0rd" not in str(exc.value) and SECRET_URL not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


# ---- queries ---------------------------------------------------------------


def test_query_applies_a_statement_timeout_then_runs_with_params():
    conn = FakeConn(rows=[{"n": 1}])
    rows = store_with(FakePool(conn)).query("select %(x)s as n", {"x": 1}, timeout_ms=2500)
    assert rows == [{"n": 1}]
    assert "statement_timeout" in conn.executed[0][0] and conn.executed[0][1] == ("2500",)
    assert conn.executed[1] == ("select %(x)s as n", {"x": 1})


def test_query_errors_are_wrapped_too():
    with pytest.raises(ts.TigerDataError) as exc:
        store_with(FakePool(error=ConnectionError(SECRET_URL))).query("select 1")
    assert "S3cr3tPassw0rd" not in str(exc.value)


def test_close_closes_the_pool():
    pool = FakePool()
    s = store_with(pool)
    s.close()
    assert pool.closed and s._pool is None


def test_unconfigured_store_reports_not_configured(monkeypatch):
    monkeypatch.delenv("TIGERDATA_URL", raising=False)
    assert ts.TigerStore().configured is False
    with pytest.raises(ts.TigerDataConfigError):
        ts.TigerStore().insert_readings([reading(0)])
