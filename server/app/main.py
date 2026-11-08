from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import games as games_api
from app.api import stats as stats_api
from app.api import trends as trends_api
from app.api import routes, websocket
from app.services.agent import agent_from_env
from app.services.auth import SupabaseAuth
from app.services.database import DatabaseConfigError, SupabaseStore
from app.services.game_rooms import GameRooms
from app.services.games_db import GameStore, GamesNotSetUpError
from app.services.ingest import SensorIngestor
from app.services.ratelimit import KeyedLimiter
from app.services.sensor_pairing import SensorPairing, bridge_secret
from app.services.session_manager import DEFAULT_IDLE_TTL_S, DEFAULT_SWEEP_INTERVAL_S, LiveSessions, sweep_loop
from app.services.timeseries import TigerStore

REST_RATE_PER_S, REST_BURST = 10.0, 60  # any /api call, per user
CREATE_RATE_PER_S, CREATE_BURST = 20 / 60, 10  # starting workouts, per user (20 per minute sustained)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per Supabase call is too noisy


def _cors_origins() -> list[str]:
    """Browser origins allowed to call the API (only matters for Expo *web*; native apps ignore CORS).

    Default ``*`` is fine here: auth is a bearer token in a header, not a cookie, so a foreign
    page can't ride on a logged-in session. Tighten with CORS_ORIGINS=https://a.com,https://b.com
    """
    raw = os.environ.get("CORS_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]


def create_app(
    store: Any = None,
    auth: Any = None,
    timeseries: Any = None,
    ingest: Any = None,
    agent: Any = None,
    games: Any = None,
    rooms: Any = None,
    idle_ttl_s: float = DEFAULT_IDLE_TTL_S,
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
) -> FastAPI:
    """``store`` / ``auth`` / ``timeseries`` / ``ingest`` can be replaced (tests use fakes).

    Tiger Data is optional: with no TIGERDATA_URL the ingestor is a no-op and everything else works.
    """
    tiger = timeseries if timeseries is not None else TigerStore()
    ingestor = ingest if ingest is not None else SensorIngestor(tiger if tiger.configured else None)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        await ingestor.start()  # background batch writer for sensor readings
        # Housekeeping: evict live sessions whose app went away and never came back.
        sweeper = asyncio.create_task(
            sweep_loop(application.state.live, application.state.store, idle_ttl_s, sweep_interval_s), name="live-session-sweeper"
        )
        try:
            yield
        finally:
            for driver in list(application.state.room_drivers.values()):
                driver.cancel()  # live 1v1 rooms end with the server
            for task in list(application.state.report_tasks):
                task.cancel()  # a report still being written is written again the next time it is asked for
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            await ingestor.stop()  # last best-effort flush
            await application.state.agent.aclose()
            close = getattr(tiger, "close", None)
            if close:
                close()

    app = FastAPI(title="SharkByte Gym Coach", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
    app.state.store = store or SupabaseStore()
    app.state.auth = auth or SupabaseAuth()
    app.state.tiger = tiger
    app.state.ingest = ingestor
    app.state.live = LiveSessions()
    app.state.games = games if games is not None else GameStore()  # GRIT Games results and races (Supabase)
    app.state.rooms = rooms if rooms is not None else GameRooms()  # live 1v1 rooms, in memory
    app.state.room_drivers = {}
    # Laptop bridge <-> workout: codes in memory, tokens signed so a paired bridge survives a server restart.
    app.state.sensor_pairing = SensorPairing(secret=bridge_secret())
    app.state.agent = agent if agent is not None else agent_from_env()  # LLM coach; rules-only unless configured
    app.state.summary_cache = OrderedDict()  # recaps that could not be saved in Supabase (kept for this server run)
    app.state.report_inflight = {}  # workout id -> the task writing its coach report (so a request joins it)
    app.state.report_tasks = set()
    app.state.summary_limiter = KeyedLimiter(rate_per_s=6 / 60, burst=3)  # recaps can cost an LLM call
    # Per-user request limits (generous for a real app; stops a runaway client or a script from hammering the
    # backend and, through it, Supabase). Tests and deployments can replace these on app.state.
    app.state.rest_limiter = KeyedLimiter(rate_per_s=REST_RATE_PER_S, burst=REST_BURST)
    app.state.create_limiter = KeyedLimiter(rate_per_s=CREATE_RATE_PER_S, burst=CREATE_BURST)

    @app.exception_handler(DatabaseConfigError)
    async def _db_not_configured(_: Request, exc: DatabaseConfigError) -> JSONResponse:
        return JSONResponse({"detail": "database is not configured on the server"}, status_code=503)

    @app.exception_handler(GamesNotSetUpError)
    async def _games_not_set_up(_: Request, exc: GamesNotSetUpError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    app.include_router(routes.router)
    app.include_router(games_api.router)
    app.include_router(stats_api.router)
    app.include_router(trends_api.router)
    app.include_router(websocket.router)
    return app


app = create_app()
