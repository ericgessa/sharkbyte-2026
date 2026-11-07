from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes, websocket
from app.services.auth import SupabaseAuth
from app.services.database import DatabaseConfigError, SupabaseStore
from app.services.session_manager import LiveSessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per Supabase call is too noisy


def create_app(store: Any = None, auth: Any = None) -> FastAPI:
    """``store`` / ``auth`` can be replaced (tests use in-memory fakes)."""
    app = FastAPI(title="SharkByte Gym Coach")
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
