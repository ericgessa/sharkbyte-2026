"""Time-series analytics over Tiger Data. Every number comes from a SQL aggregate, and every function
returns a SMALL summary (a handful of rows or a dict): raw high-frequency streams are never handed to
the coach/LLM, only these summaries.

``tiger`` is anything with ``query(sql, params) -> list[dict]`` (TigerStore, or a fake in tests).
Knee angle: 180 = straight leg, smaller = deeper squat.

Camera readings carry ``metadata.confidence``; knee statistics ignore frames below ``min_confidence``
so a momentary tracking glitch (landmarks jumping) doesn't pollute averages or ranges.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

KNEE_METRICS: tuple[str, ...] = ("left_knee_angle", "right_knee_angle")
DEFAULT_MIN_CONFIDENCE = 0.6
MAX_WINDOW_ROWS = 5_000

# Frames without a confidence value (all non-camera sensors) count as fully trusted.
_CONFIDENT = "coalesce((metadata->>'confidence')::float8, 1.0) >= %(min_conf)s"


class Queryable(Protocol):
    def query(self, sql: str, params: Any = None, timeout_ms: int = ...) -> list[dict[str, Any]]: ...


def _round(v: Any, nd: int = 2) -> float | None:
    return None if v is None else round(float(v), nd)


# ------------------------------------------------------------------ 1. angle statistics


def angle_stats(
    tiger: Queryable,
    session_id: UUID,
    metrics: Sequence[str] = KNEE_METRICS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Average / minimum / maximum / spread of the given joint-angle metrics over a whole workout."""
    rows = tiger.query(
        f"""
        select count(*) as n,
               avg(metric_value) as avg_deg, min(metric_value) as min_deg,
               max(metric_value) as max_deg, stddev_samp(metric_value) as stddev_deg
        from sensor_readings
        where session_id = %(sid)s and metric_name = any(%(metrics)s) and {_CONFIDENT}
        """,
        {"sid": session_id, "metrics": list(metrics), "min_conf": min_confidence},
    )
    r = rows[0]
    return {
        "samples": int(r["n"]),
        "avg_deg": _round(r["avg_deg"]),
        "min_deg": _round(r["min_deg"]),  # deepest point reached
        "max_deg": _round(r["max_deg"]),
        "stddev_deg": _round(r["stddev_deg"]),
    }


# ------------------------------------------------------------------ 2. movement range across a set


def movement_range_over_time(
    tiger: Queryable,
    session_id: UUID,
    metrics: Sequence[str] = KNEE_METRICS,
    bucket_seconds: float = 5.0,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Range of motion (max - min angle) in fixed time buckets, plus its linear trend.

    ``trend_deg_per_min`` < 0 means the range is shrinking over the workout (fatigue / shallower reps).
    Rest periods between reps appear as small-range buckets, so prefer ``rep_trends`` for per-rep
    comparisons and use this one for a time-axis chart.
    """
    rows = tiger.query(
        f"""
        with b as (
            select time_bucket(make_interval(secs => %(bucket)s), time) as bucket,
                   min(metric_value) as min_deg, max(metric_value) as max_deg,
                   max(metric_value) - min(metric_value) as range_deg
            from sensor_readings
            where session_id = %(sid)s and metric_name = any(%(metrics)s) and {_CONFIDENT}
            group by 1
        )
        select bucket, min_deg, max_deg, range_deg,
               regr_slope(range_deg, extract(epoch from bucket)) over () * 60 as trend_deg_per_min
        from b order by bucket
        """,
        {"sid": session_id, "metrics": list(metrics), "bucket": float(bucket_seconds), "min_conf": min_confidence},
    )
    return {
        "bucket_seconds": bucket_seconds,
        "trend_deg_per_min": _round(rows[0]["trend_deg_per_min"]) if rows else None,
        "buckets": [
            {"time": r["bucket"].isoformat(), "min_deg": _round(r["min_deg"]), "max_deg": _round(r["max_deg"]), "range_deg": _round(r["range_deg"])}
            for r in rows
        ],
    }


# ------------------------------------------------------------------ 3. measurements in a time window


def readings_in_window(
    tiger: Queryable,
    session_id: UUID,
    start: datetime,
    end: datetime,
    metrics: Sequence[str] | None = None,
    bucket_seconds: float | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Measurements with ``start <= time < end``, oldest first.

    With ``bucket_seconds`` the window is downsampled in SQL (average per bucket per metric) so a chart
    or summary never pulls the raw stream. The row count is capped at MAX_WINDOW_ROWS either way.
    """
    limit = max(1, min(int(limit), MAX_WINDOW_ROWS))
    params: dict[str, Any] = {"sid": session_id, "t0": start, "t1": end, "metrics": list(metrics) if metrics else None, "limit": limit}
    metric_filter = "and (%(metrics)s::text[] is null or metric_name = any(%(metrics)s::text[]))"
    if bucket_seconds:
        params["bucket"] = float(bucket_seconds)
        rows = tiger.query(
            f"""
            select time_bucket(make_interval(secs => %(bucket)s), time) as time, source, sensor_id, metric_name,
                   avg(metric_value) as metric_value, count(*) as samples
            from sensor_readings
            where session_id = %(sid)s and time >= %(t0)s and time < %(t1)s {metric_filter}
            group by 1, source, sensor_id, metric_name
            order by 1, metric_name limit %(limit)s
            """,
            params,
        )
    else:
        rows = tiger.query(
            f"""
            select time, source, sensor_id, metric_name, metric_value, 1 as samples
            from sensor_readings
            where session_id = %(sid)s and time >= %(t0)s and time < %(t1)s {metric_filter}
            order by time, metric_name limit %(limit)s
            """,
            params,
        )
    return [
        {
            "time": r["time"].isoformat(),
            "source": r["source"],
            "sensor_id": r["sensor_id"],
            "metric": r["metric_name"],
            "value": _round(r["metric_value"], 3),
            "samples": int(r["samples"]),
        }
        for r in rows
    ]


# ------------------------------------------------------------------ 4. trends across repetitions


def rep_trends(
    tiger: Queryable,
    session_id: UUID,
    windows: Sequence[tuple[int, datetime, datetime]],
    metrics: Sequence[str] = KNEE_METRICS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Per-rep depth and range of motion from the raw readings, plus linear trends across reps.

    ``windows`` are (rep_number, start, end) from the reps stored in Supabase: this is where the two
    databases meet. ``depth_trend_deg_per_rep`` > 0 means the deepest point of each rep is getting
    shallower; ``range_trend_deg_per_rep`` < 0 means the range of motion is shrinking.
    """
    if not windows:
        return {"reps": [], "depth_trend_deg_per_rep": None, "range_trend_deg_per_rep": None}
    rows = tiger.query(
        f"""
        with w as (
            select * from unnest(%(nums)s::int[], %(starts)s::timestamptz[], %(ends)s::timestamptz[])
                   as w(rep_number, t0, t1)
        ), per_rep as (
            select w.rep_number, count(*) as samples,
                   min(r.metric_value) as deepest_deg, max(r.metric_value) as top_deg,
                   max(r.metric_value) - min(r.metric_value) as range_deg,
                   avg(r.metric_value) as avg_deg
            from w
            join sensor_readings r
              on r.session_id = %(sid)s and r.metric_name = any(%(metrics)s)
             and r.time >= w.t0 and r.time <= w.t1
             and {_CONFIDENT.replace('metadata', 'r.metadata')}
            group by w.rep_number
        )
        select rep_number, samples, deepest_deg, top_deg, range_deg, avg_deg,
               regr_slope(deepest_deg, rep_number) over () as depth_slope,
               regr_slope(range_deg, rep_number) over () as range_slope
        from per_rep order by rep_number
        """,
        {
            "sid": session_id,
            "metrics": list(metrics),
            "min_conf": min_confidence,
            "nums": [w[0] for w in windows],
            "starts": [w[1] for w in windows],
            "ends": [w[2] for w in windows],
        },
    )
    return {
        "reps": [
            {
                "rep_number": int(r["rep_number"]),
                "samples": int(r["samples"]),
                "deepest_deg": _round(r["deepest_deg"]),
                "top_deg": _round(r["top_deg"]),
                "range_deg": _round(r["range_deg"]),
                "avg_deg": _round(r["avg_deg"]),
            }
            for r in rows
        ],
        "depth_trend_deg_per_rep": _round(rows[0]["depth_slope"], 3) if rows else None,
        "range_trend_deg_per_rep": _round(rows[0]["range_slope"], 3) if rows else None,
    }


# ------------------------------------------------------------------ 5. compact summary for the coach


def sensor_overview(tiger: Queryable, session_id: UUID) -> dict[str, Any]:
    """Which metrics exist for the session, how many samples, and their basic statistics (one grouped query)."""
    rows = tiger.query(
        """
        select source, metric_name, count(*) as n, avg(metric_value) as avg_v,
               min(metric_value) as min_v, max(metric_value) as max_v,
               min(time) as first_t, max(time) as last_t
        from sensor_readings where session_id = %(sid)s
        group by source, metric_name order by source, metric_name
        """,
        {"sid": session_id},
    )
    if not rows:
        return {"readings": 0, "first_time": None, "last_time": None, "metrics": {}}
    return {
        "readings": sum(int(r["n"]) for r in rows),
        "first_time": min(r["first_t"] for r in rows).isoformat(),
        "last_time": max(r["last_t"] for r in rows).isoformat(),
        "metrics": {
            r["metric_name"]: {
                "source": r["source"],
                "samples": int(r["n"]),
                "avg": _round(r["avg_v"]),
                "min": _round(r["min_v"]),
                "max": _round(r["max_v"]),
            }
            for r in rows
        },
    }


def session_summary(
    tiger: Queryable,
    session_id: UUID,
    rep_windows: Sequence[tuple[int, datetime, datetime]] = (),
) -> dict[str, Any]:
    """Everything the coach needs from Tiger Data, in a few KB regardless of workout length."""
    return {
        "overview": sensor_overview(tiger, session_id),
        "knee_angle": angle_stats(tiger, session_id),
        "rep_trends": rep_trends(tiger, session_id, rep_windows),
    }
