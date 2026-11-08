"""Turning a finished GRIT Games round into a saved result: pure functions, no database, no network.

The rule that matters: the client says WHAT it played (game, mode, exercise, target, time limit) and how long it
lasted, but every number that makes a result competitive comes from the server's own records of the workout its rep
counter scored: how many reps, when each happened, whether the target was reached in time. A player (or a modified
client) therefore cannot save "10 reps in 12 seconds" without the server having counted them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.games import GameResultIn, PersonalBestComparison

GRACE_MS = 1500  # network and clock slack when comparing a round against its time limit


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


def rep_times_ms(session_started_at: Any, reps: list[dict[str, Any]]) -> list[int]:
    """Milliseconds from the workout's start to each counted rep, oldest first. A rep with no recorded time is
    skipped (the list is then shorter than the rep count) rather than guessed."""
    started = _parse(session_started_at)
    if started is None:
        return []
    out: list[int] = []
    for rep in sorted(reps, key=lambda r: r.get("rep_number", 0)):
        ended = _parse((rep.get("metrics") or {}).get("ended_at"))
        if ended is not None:
            out.append(max(0, round((ended - started).total_seconds() * 1000)))
    return sorted(out)


def _wall_ms(session: dict[str, Any]) -> int | None:
    started, ended = _parse(session.get("started_at")), _parse(session.get("ended_at"))
    if started is None or ended is None:
        return None
    return max(0, round((ended - started).total_seconds() * 1000))


def build_result_row(user_id: str, body: GameResultIn, session: dict[str, Any], reps: list[dict[str, Any]]) -> dict[str, Any]:
    """The row to insert for ``body``, scored from the workout ``session`` and its saved ``reps``."""
    times = rep_times_ms(session.get("started_at"), reps)
    have_times = len(times) == len(reps)
    wall = _wall_ms(session)
    duration_cap = (wall + GRACE_MS) if wall is not None else 3_600_000

    target, limit = int(body.target_reps or 0), int(body.time_limit_s or 0)
    limit_ms = limit * 1000 + GRACE_MS
    in_time = [t for t in times if t <= limit_ms] if have_times else times
    counted = len(in_time) if have_times else min(len(reps), target)
    valid = min(counted, target)
    completed = valid >= target
    times = in_time[:target]
    if completed and have_times and len(times) >= target:
        duration = times[target - 1]  # the moment the server counted the target-th rep
    else:
        duration = min(body.duration_ms, limit_ms, duration_cap)
    score = valid
    return {
        "user_id": user_id,
        "game": body.game,
        "mode": body.mode,
        "exercise": body.exercise,
        "input_source": "camera",
        "valid_reps": valid,
        "score": score,
        "duration_ms": int(duration),
        "target_reps": target,
        "time_limit_s": limit,
        "completed": completed,
        "form_feedback": dict(body.form_feedback),
        "rep_times_ms": times,
        "workout_session_id": str(body.workout_session_id),
        "room_id": None,
    }


def choose_ghost(prior: list[dict[str, Any]], target_reps: int | None) -> dict[str, Any] | None:
    """The previous attempt to race against: the fastest FINISHED race with the same target; failing that, the
    attempt with the most reps (same exercise). None on a first attempt. ``prior`` must already be one user's
    camera-tracked Rep Racer results for one exercise."""
    finished = [r for r in prior if r.get("completed") and r.get("target_reps") == target_reps]
    if finished:
        return min(finished, key=lambda r: (r["duration_ms"], str(r.get("created_at", ""))))
    attempted = [r for r in prior if r.get("rep_times_ms")]
    if attempted:
        return max(attempted, key=lambda r: (r["valid_reps"], -r["duration_ms"]))
    return None


def compare(new: dict[str, Any], previous: dict[str, Any] | None) -> PersonalBestComparison:
    """``new`` against ``previous`` (from choose_ghost, taken BEFORE ``new`` was saved)."""
    if previous is None:
        return PersonalBestComparison(has_previous=False, is_new_best=bool(new["completed"]))
    reps_delta = new["valid_reps"] - previous["valid_reps"]
    if new["completed"] and previous["completed"]:
        delta = new["duration_ms"] - previous["duration_ms"]
        return PersonalBestComparison(
            has_previous=True,
            previous_duration_ms=previous["duration_ms"],
            previous_reps=previous["valid_reps"],
            is_new_best=delta < 0,
            delta_ms=delta,
            reps_delta=reps_delta,
        )
    return PersonalBestComparison(
        has_previous=True,
        previous_duration_ms=previous["duration_ms"] if previous["completed"] else None,
        previous_reps=previous["valid_reps"],
        is_new_best=bool(new["completed"]) and not previous["completed"],
        delta_ms=None,
        reps_delta=reps_delta,
    )
