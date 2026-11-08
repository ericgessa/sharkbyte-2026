"""The coach's end-of-workout report: written once when a workout finishes, saved in Supabase, read back from there.

    finish workout --(background)--> build report (Gemini, or the rules text) --> save in ``workout_reports``
    History tab opens a workout  ---> GET /api/workouts/{id}/coach-summary ---> the saved report (no second Gemini call)

Rules that keep it cheap, safe and honest:
  * One report per workout, written when the workout FINISHES (not for a workout still in progress: it would go stale).
  * Opening the History tab never pays for a second Gemini call: the saved report is returned. If a request arrives while
    the report is still being written, it waits for that one instead of starting another (in-flight de-duplication).
  * ``source`` says who wrote it: ``agent`` (Gemini) or ``rules`` (the deterministic text: AI coach off, unavailable,
    failed a safety check, or the user reported pain). A saved ``rules`` report is replaced by a Gemini one if the AI coach
    is available later (at most once per ``UPGRADE_AFTER_S``, so a broken key cannot cause a storm of calls).
  * The report never blocks or breaks anything else. If saving fails (for example the table has not been created yet) the
    report is still returned and kept in memory for this server run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from app.services.agent import rules_summary
from app.services.coach_context import build_coach_context
from app.services.database import ReportsNotSetUpError
from app.services.exercises import DEFAULT_EXERCISE, for_stored_name

logger = logging.getLogger(__name__)

UPGRADE_AFTER_S = 600.0
MEMORY_CACHE_MAX = 200


def _model_name(app: FastAPI) -> str | None:
    provider = getattr(app.state.agent, "_provider", None)
    name = getattr(provider, "model", None)
    return str(name)[:120] if name else None


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


def _out(summary: str, source: str, saved: bool, generated_at: Any = None) -> dict[str, Any]:
    at = _parse(generated_at)
    return {"summary": summary, "source": source, "saved": saved, "generated_at": at.isoformat() if at else None}


async def build_report(app: FastAPI, row: dict[str, Any]) -> tuple[str, str]:
    """(text, source) for the workout ``row``: reads both databases, asks Gemini if it is on, else the rules text."""
    state = app.state
    session_id = UUID(str(row["id"]))
    context = await asyncio.to_thread(build_coach_context, state.store, state.tiger, session_id)
    exercise = for_stored_name((row.get("exercises") or {}).get("name", DEFAULT_EXERCISE))
    fallback = rules_summary(context, exercise.analysis_supported, exercise.display_name.lower(), exercise.joint)
    if exercise.analysis_supported:  # Gemini reports on every analyzed exercise (it is told which joint was counted)
        report = {**context, "exercise": exercise.display_name, "counted_joint": exercise.joint}
        return await state.agent.summarize(report, fallback)
    return fallback, "rules"  # only logged, nothing measured to report on


async def _generate_and_save(app: FastAPI, row: dict[str, Any]) -> dict[str, Any]:
    state = app.state
    text, source = await build_report(app, row)
    if row.get("status") != "completed":
        return _out(text, source, saved=False)  # an open workout's report would go stale: never stored
    sid = str(row["id"])
    try:
        await asyncio.to_thread(state.store.save_report, sid, text, source, _model_name(app) if source == "agent" else None)
        state.summary_cache.pop(sid, None)
        return _out(text, source, saved=True, generated_at=datetime.now(timezone.utc))
    except ReportsNotSetUpError:
        logger.info("workout report not saved: apply supabase/migrations/20261108000002_workout_reports.sql")
    except Exception:
        logger.warning("could not save the report for workout %s; it is kept in memory for now", sid, exc_info=True)
    state.summary_cache[sid] = (text, source)
    while len(state.summary_cache) > MEMORY_CACHE_MAX:
        state.summary_cache.popitem(last=False)
    return _out(text, source, saved=False)


def _start(app: FastAPI, row: dict[str, Any]) -> asyncio.Task[dict[str, Any]]:
    """Begin writing this workout's report, or join the one already in progress."""
    sid = str(row["id"])
    inflight: dict[str, asyncio.Task[dict[str, Any]]] = app.state.report_inflight
    task = inflight.get(sid)
    if task is None or task.done():
        task = asyncio.create_task(_generate_and_save(app, row), name=f"report-{sid}")
        inflight[sid] = task
        app.state.report_tasks.add(task)

        def done(t: asyncio.Task[dict[str, Any]]) -> None:
            app.state.report_tasks.discard(t)
            if inflight.get(sid) is t:
                inflight.pop(sid, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error("report generation for %s crashed", sid, exc_info=t.exception())

        task.add_done_callback(done)
    return task


def schedule_on_finish(app: FastAPI, row: dict[str, Any]) -> None:
    """Called when a workout has just finished: write and save its report in the background (finishing never waits)."""
    _start(app, row)


async def get_report(app: FastAPI, row: dict[str, Any]) -> dict[str, Any]:
    """The workout's report: the saved one, else (one in the making, or) a new one written now."""
    state = app.state
    sid = str(row["id"])
    if row.get("status") == "completed":
        try:
            saved = await asyncio.to_thread(state.store.get_report, sid)
        except Exception:
            logger.warning("could not read the saved report for %s", sid, exc_info=True)
            saved = None
        if saved is not None:
            if _should_upgrade(app, row, saved):
                return await asyncio.shield(_start(app, row))
            return _out(saved["summary"], saved["source"], saved=True, generated_at=saved.get("created_at"))
        if sid in state.summary_cache:  # the database could not store it earlier in this server run
            text, source = state.summary_cache[sid]
            return _out(text, source, saved=False)
    return await asyncio.shield(_start(app, row))


def _should_upgrade(app: FastAPI, row: dict[str, Any], saved: dict[str, Any]) -> bool:
    """A saved rules report is worth replacing with a Gemini one when Gemini is available now."""
    if saved.get("source") != "rules" or not app.state.agent.enabled:
        return False
    if not for_stored_name((row.get("exercises") or {}).get("name", DEFAULT_EXERCISE)).analysis_supported:
        return False
    updated = _parse(saved.get("updated_at")) or _parse(saved.get("created_at"))
    return updated is None or (datetime.now(timezone.utc) - updated).total_seconds() >= UPGRADE_AFTER_S
