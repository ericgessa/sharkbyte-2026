"""Progress across all of a user's workouts: totals, per-exercise records, weekly activity, streaks, race bests.

Pure functions: no database, no clock reads (``today`` is passed in), so every rule is unit-tested with fixed dates.

What counts: only COMPLETED workouts. A day counts towards a streak only if a workout that day counted at least one
rep (opening the app and finishing nothing is not training). Days and weeks are the user's LOCAL ones: the caller
passes the offset of their time zone from UTC, because "today" is not the same instant everywhere.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.services.exercises import for_stored_name

MAX_ACTIVE_SECONDS = 6 * 3600  # a workout left open and finished much later must not count as hours of training


def _parse(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_date(ts: Any, tz_offset_minutes: int) -> date | None:
    parsed = _parse(ts)
    return (parsed + timedelta(minutes=tz_offset_minutes)).date() if parsed else None


def active_days(sessions: list[dict[str, Any]], tz_offset_minutes: int = 0) -> set[date]:
    """The local dates on which at least one completed workout counted a rep."""
    days: set[date] = set()
    for s in sessions:
        if s.get("status") == "completed" and (s.get("total_reps") or 0) > 0:
            d = local_date(s.get("started_at"), tz_offset_minutes)
            if d is not None:
                days.add(d)
    return days


def streaks(days: set[date], today: date) -> tuple[int, int]:
    """(current, longest) streak in days. The current streak survives until the end of tomorrow's window: if you have not
    trained yet TODAY it still counts through yesterday, so opening the app in the morning does not show 0."""
    if not days:
        return 0, 0
    ordered = sorted(days)
    longest = run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    anchor = today if today in days else today - timedelta(days=1)
    current = 0
    while anchor in days:
        current += 1
        anchor -= timedelta(days=1)
    return current, max(longest, current)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def compute_progress(sessions: list[dict[str, Any]], today: date, tz_offset_minutes: int = 0, weeks: int = 8) -> dict[str, Any]:
    """``sessions`` are workout_sessions rows (each with ``exercises: {name}``). Returns plain dicts for the API schema."""
    done = [s for s in sessions if s.get("status") == "completed"]
    total_reps = sum(int(s.get("total_reps") or 0) for s in done)

    seconds = 0
    first = last = None
    per: dict[str, dict[str, Any]] = {}
    weekly: dict[date, dict[str, int]] = defaultdict(lambda: {"workouts": 0, "reps": 0})
    best: dict[str, Any] | None = None
    for s in done:
        started, ended = _parse(s.get("started_at")), _parse(s.get("ended_at"))
        if started and ended:
            seconds += int(max(0.0, min((ended - started).total_seconds(), MAX_ACTIVE_SECONDS)))
        if started:
            first = started if first is None or started < first else first
            last = started if last is None or started > last else last
        reps = int(s.get("total_reps") or 0)
        slug = for_stored_name(((s.get("exercises") or {}).get("name")) or "squat").slug
        row = per.setdefault(slug, {"exercise": slug, "workouts": 0, "total_reps": 0, "best_workout_reps": 0, "best_workout_id": None, "last_workout_at": None})
        row["workouts"] += 1
        row["total_reps"] += reps
        if reps > row["best_workout_reps"]:
            row["best_workout_reps"], row["best_workout_id"] = reps, s.get("id")
        if started and (row["last_workout_at"] is None or started > row["last_workout_at"]):
            row["last_workout_at"] = started
        d = local_date(s.get("started_at"), tz_offset_minutes)
        if d is not None:
            w = weekly[week_start(d)]
            w["workouts"] += 1
            w["reps"] += reps
        if reps > 0 and (best is None or reps > best["reps"]):
            best = {"exercise": slug, "reps": reps, "workout_id": s.get("id"), "at": started}

    this_week = week_start(today)
    series = []
    for i in range(weeks - 1, -1, -1):
        start = this_week - timedelta(weeks=i)
        w = weekly.get(start, {"workouts": 0, "reps": 0})
        series.append({"week_start": start, "workouts": w["workouts"], "reps": w["reps"]})

    current, longest = streaks(active_days(done, tz_offset_minutes), today)
    return {
        "totals": {
            "workouts": len(done),
            "reps": total_reps,
            "active_seconds": seconds,
            "first_workout_at": first,
            "last_workout_at": last,
        },
        "per_exercise": sorted(per.values(), key=lambda r: (-r["total_reps"], r["exercise"])),
        "weekly": series,
        "streak": {"current_days": current, "longest_days": longest},
        "records": {"most_reps_in_a_workout": best},
    }


def race_bests(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Rep Racer summary from saved game results (all camera-tracked): how many races, how many reached their target, and
    the fastest finished race for every exercise and target."""
    fastest: dict[tuple[str, int], dict[str, Any]] = {}
    finished = 0
    for r in results:
        if r.get("game") != "rep_racer" or not r.get("completed"):
            continue
        finished += 1
        key = (r["exercise"], int(r.get("target_reps") or 0))
        cur = fastest.get(key)
        if cur is None or r["duration_ms"] < cur["duration_ms"]:
            fastest[key] = {"exercise": r["exercise"], "target_reps": key[1], "duration_ms": int(r["duration_ms"]), "mode": r["mode"], "achieved_at": _parse(r.get("created_at"))}
    return {
        "races": sum(1 for r in results if r.get("game") == "rep_racer"),
        "finished": finished,
        "fastest": sorted(fastest.values(), key=lambda b: (b["exercise"], b["target_reps"])),
    }
