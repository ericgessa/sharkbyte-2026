"""Trends for one exercise (the app's floating fact bubbles + one piece of advice), and device-counted reps.

GET  /api/trends/{exercise}?session_id=...  facts computed by code (services/trends.py); headline + advice worded by
                                           the AI coach when it is on, else the rules wording. Only the caller's data.
POST /api/workouts/{id}/device-reps        for exercises the server can't count (jumping jacks): the phone's count is
                                           saved as reps, so the set shows up in history, stats and trends.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes import UserId, _owned_session
from app.services.exercises import UnknownExerciseError, for_stored_name
from app.services.exercises import resolve as resolve_exercise
from app.services.trends import MAX_SETS, build_trends, rules_headline, set_stats

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_SESSIONS_SCANNED = 300
MAX_DEVICE_REPS = 1000
CACHE_SIZE = 300


class TrendFact(BaseModel):
    key: str
    big: str
    small: str
    tone: str


class WaveBar(BaseModel):
    d: float | None = None  # deepest angle of the rep (smaller = deeper)
    t: float | None = None  # rep duration, ms


class TrendWave(BaseModel):
    session_id: str
    at: str
    reps: int
    bars: list[WaveBar]


class TrendsOut(BaseModel):
    exercise: str
    sets: int
    facts: list[TrendFact]
    headline: str
    advice: str
    source: str  # "agent" (AI coach) or "rules"
    waves: list[TrendWave] = []  # recent sets' rep waves, oldest first


class DeviceReps(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reps: int = Field(ge=0, le=MAX_DEVICE_REPS)


def _cache(state: Any) -> OrderedDict:
    c = getattr(state, "trends_cache", None)
    if c is None:
        c = state.trends_cache = OrderedDict()
    return c


@router.get("/api/trends/{exercise}", response_model=TrendsOut)
async def exercise_trends(exercise: str, request: Request, user_id: UserId, session_id: UUID | None = None) -> TrendsOut:
    """Your trend for one exercise. ``session_id`` (optional): the set you just finished, which the facts focus on."""
    try:
        ex = resolve_exercise(exercise)
    except UnknownExerciseError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    state = request.app.state
    rows = await asyncio.to_thread(state.store.list_sessions, user_id, MAX_SESSIONS_SCANNED)
    mine = [
        r for r in rows
        if r.get("status") == "completed" and for_stored_name((r.get("exercises") or {}).get("name", "")).slug == ex.slug
    ]
    recent = mine[:MAX_SETS]
    all_time_reps = sum(int(r.get("total_reps") or 0) for r in mine)
    focus = str(session_id) if session_id else None

    key = (user_id, ex.slug, tuple(str(r["id"]) for r in recent), focus)
    cache = _cache(state)
    if key in cache:
        return cache[key]

    reps_per_set = await asyncio.gather(*(asyncio.to_thread(state.store.list_repetitions, r["id"]) for r in recent))
    sets = [set_stats(r, reps) for r, reps in zip(recent, reps_per_set)]
    t = build_trends(ex.display_name + "s" if not ex.display_name.endswith("s") else ex.display_name, sets, all_time_reps, len(mine), focus)

    headline, advice, source = rules_headline(t), t["advice"], "rules"
    limiter = getattr(state, "summary_limiter", None)
    if sets and getattr(state.agent, "enabled", False) and (limiter is None or limiter.allow(f"trends:{user_id}")):
        headline, advice, source = await state.agent.trends(t["context"], headline, advice)

    out = TrendsOut(
        exercise=ex.slug, sets=len(mine), facts=[TrendFact(**f) for f in t["facts"]], headline=headline, advice=advice,
        source=source, waves=[TrendWave(**w) for w in t.get("waves", [])],
    )
    cache[key] = out
    while len(cache) > CACHE_SIZE:
        cache.popitem(last=False)
    return out


@router.post("/api/workouts/{session_id}/device-reps", status_code=status.HTTP_204_NO_CONTENT)
async def device_reps(session_id: UUID, body: DeviceReps, request: Request, user_id: UserId) -> None:
    """Save the phone's rep count for an exercise the server doesn't count itself (e.g. jumping jacks). Call before
    ``finish``. Refused (409) for exercises the server counts, so camera-counted reps can never be overwritten."""
    row = await asyncio.to_thread(_owned_session, request, session_id, user_id)
    if row["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "workout is not active")
    ex = for_stored_name((row.get("exercises") or {}).get("name", ""))
    if ex.analysis_supported:
        raise HTTPException(status.HTTP_409_CONFLICT, "the server counts this exercise itself")
    store = request.app.state.store
    for n in range(1, body.reps + 1):  # (session_id, rep_number) is unique, so a retry can't double count
        await asyncio.to_thread(store.save_repetition, session_id, n, {"counted_by": "device"})
    logger.info("device reps saved session=%s reps=%d", session_id, body.reps)
