from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import routes, websocket
from app.services.session_manager import SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    app = FastAPI(title="SharkByte Gym Coach")
    app.state.sessions = SessionManager()
    app.include_router(routes.router)
    app.include_router(websocket.router)
    return app


app = create_app()
