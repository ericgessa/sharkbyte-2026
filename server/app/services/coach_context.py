"""Compact, bounded workout context for the (future) AI coach, assembled from BOTH databases.

  * Supabase: completed reps, their stats, the coaching events already given.
  * Tiger Data: summaries computed in SQL from the raw sensor readings.

Only summaries ever leave this module: never raw sensor streams. If Tiger Data is not configured or
unreachable, the Tiger part is replaced by ``{"available": False, ...}`` and the Supabase part still
works. This is deliberately NOT a distributed transaction: the two reads are independent, and the
context is advisory.

Blocking (database drivers): call from a plain ``def`` endpoint or ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.services import analytics
from app.services.summary import compute_stats

logger = logging.getLogger(__name__)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def rep_windows(reps: list[dict[str, Any]]) -> list[tuple[int, datetime, datetime]]:
    """(rep_number, start, end) for reps whose saved metrics include their server-time window.

    Reps saved before time windows existed simply have none and are skipped.
    """
    out = []
    for r in reps:
        m = r.get("metrics") or {}
        start, end = _parse_time(m.get("started_at")), _parse_time(m.get("ended_at"))
        if start and end and end >= start:
            out.append((int(r["rep_number"]), start, end))
    return out


def build_coach_context(store: Any, tiger: Any, session_id: UUID) -> dict[str, Any]:
    """The caller must already have verified that ``session_id`` belongs to the user."""
    reps = store.list_repetitions(session_id)
    events = store.list_coaching_events(session_id)
    context: dict[str, Any] = {
        "session_id": str(session_id),
        "supabase": {
            "stats": compute_stats(reps),
            "coaching_events": [
                {"rep_number": e.get("rep_number"), "issue_code": e.get("issue_code"), "action": e.get("action")}
                for e in events
            ],
        },
        "timeseries": {"available": False, "reason": "not_configured"},
    }
    if not getattr(tiger, "configured", False):
        return context
    try:
        summary = analytics.session_summary(tiger, session_id, rep_windows(reps))
        context["timeseries"] = {"available": True, **summary}
    except Exception as exc:  # outage, timeout, missing table: degrade, don't fail
        logger.warning("Tiger Data summary unavailable (%s); returning Supabase data only", type(exc).__name__)
        context["timeseries"] = {"available": False, "reason": "unavailable"}
    return context
