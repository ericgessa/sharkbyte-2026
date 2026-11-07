from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes, websocket
from app.services.auth import SupabaseAuth
from app.services.database import DatabaseConfigError, SupabaseStore
from app.services.ingest import SensorIngestor
from app.services.ratelimit import KeyedLimiter
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
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            await ingestor.stop()  # last best-effort flush
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
    # Per-user request limits (generous for a real app; stops a runaway client or a script from hammering the
    # backend and, through it, Supabase). Tests and deployments can replace these on app.state.
    app.state.rest_limiter = KeyedLimiter(rate_per_s=REST_RATE_PER_S, burst=REST_BURST)
    app.state.create_limiter = KeyedLimiter(rate_per_s=CREATE_RATE_PER_S, burst=CREATE_BURST)

    @app.exception_handler(DatabaseConfigError)
    async def _db_not_configured(_: Request, exc: DatabaseConfigError) -> JSONResponse:
        return JSONResponse({"detail": "database is not configured on the server"}, status_code=503)

    app.include_router(routes.router)
    app.include_router(websocket.router)
    return app


app = create_app()
