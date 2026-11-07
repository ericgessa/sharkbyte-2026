from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes, websocket
from app.services.auth import SupabaseAuth
from app.services.database import DatabaseConfigError, SupabaseStore
from app.services.session_manager import LiveSessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per Supabase call is too noisy


def _cors_origins() -> list[str]:
    """Browser origins allowed to call the API (only matters for Expo *web*; native apps ignore CORS).

    Default ``*`` is fine here: auth is a bearer token in a header, not a cookie, so a foreign
    page can't ride on a logged-in session. Tighten with CORS_ORIGINS=https://a.com,https://b.com
    """
    raw = os.environ.get("CORS_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]


def create_app(store: Any = None, auth: Any = None) -> FastAPI:
    """``store`` / ``auth`` can be replaced (tests use in-memory fakes)."""
    app = FastAPI(title="SharkByte Gym Coach")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
    app.state.store = store or SupabaseStore()
    app.state.auth = auth or SupabaseAuth()
    app.state.live = LiveSessions()

    @app.exception_handler(DatabaseConfigError)
    async def _db_not_configured(_: Request, exc: DatabaseConfigError) -> JSONResponse:
        return JSONResponse({"detail": "database is not configured on the server"}, status_code=503)

    app.include_router(routes.router)
    app.include_router(websocket.router)
    return app


app = create_app()
