"""GET /api/stats: progress across all of the caller's workouts (totals, records, weekly activity, streaks, race bests)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.routes import UserId
from app.schemas.stats import ProgressOut
from app.services.games_db import GamesNotSetUpError
from app.services.user_stats import compute_progress, race_bests

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_WORKOUTS = 1000  # the newest workouts looked at (one Supabase page)
MAX_RESULTS = 500


@router.get("/api/stats", response_model=ProgressOut)
def my_stats(
    request: Request,
    user_id: UserId,
    tz_offset_minutes: Annotated[int, Query(ge=-840, le=840, description="Your time zone's offset from UTC in minutes (e.g. -300 for US Eastern). Decides which day and week a workout belongs to.")] = 0,
    weeks: Annotated[int, Query(ge=1, le=52, description="How many weeks of activity to return, ending with the current one.")] = 8,
) -> ProgressOut:
    """Your progress across all workouts. Only completed workouts count; a day counts towards the streak only if a
    workout that day counted at least one rep. A user with no workouts gets zeros, not an error. ``games`` is null if the
    GRIT Games tables are not set up yet; everything else is unaffected. Only the caller's own data is ever read."""
    state = request.app.state
    clock = getattr(state, "clock", None)
    now = clock() if clock else datetime.now(timezone.utc)
    today = (now + timedelta(minutes=tz_offset_minutes)).date()

    sessions = state.store.list_sessions(user_id, MAX_WORKOUTS)
    progress = compute_progress(sessions, today, tz_offset_minutes, weeks)

    games = None
    try:
        games = race_bests(state.games.list_results(user_id, "rep_racer", None, MAX_RESULTS))
    except GamesNotSetUpError:
        pass  # the games migration has not been applied: no race stats yet, everything else works
    except Exception:  # a Supabase hiccup: stats about workouts must not depend on it
        logger.warning("race stats unavailable for user %s", user_id, exc_info=True)
    return ProgressOut(**progress, games=games, workouts_considered=len(sessions))
