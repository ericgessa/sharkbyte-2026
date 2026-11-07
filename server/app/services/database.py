"""Supabase access for the backend. Trusted server-side code only.

The client built here uses the *secret* key and therefore BYPASSES Row Level
Security. Every function that touches user data takes ``user_id`` and filters on
it explicitly: callers must pass the id of the user whose JWT the API has
already validated (never an id taken from a request body).

The supabase-py client used here is synchronous. In FastAPI, call these from
plain ``def`` endpoints (run in the threadpool) or wrap them with
``asyncio.to_thread`` / ``starlette.concurrency.run_in_threadpool`` so they never
block the WebSocket event loop.
"""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()  # reads server/.env when present; real environment variables win


class DatabaseConfigError(RuntimeError):
    """Missing or unsuitable Supabase configuration (message never contains a key)."""


class DatabaseError(RuntimeError):
    """A query failed or returned something unexpected."""


# ----------------------------------------------------------------- connection


def _check_secret_key(key: str) -> None:
    """Catch the most common mistake: using a client-safe key on the server."""
    if key.startswith("sb_publishable_"):
        raise DatabaseConfigError(
            "SUPABASE_SECRET_KEY holds a publishable key. Use the secret key "
            "(sb_secret_...) from Dashboard > Settings > API Keys."
        )
    if key.startswith("eyJ"):  # legacy JWT-style key: look at its role claim
        try:
            payload = key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError):
            return  # not decodable; let Supabase reject it if it is wrong
        if claims.get("role") == "anon":
            raise DatabaseConfigError(
                "SUPABASE_SECRET_KEY holds the legacy anon key. Use the secret "
                "key (or the legacy service_role key) instead."
            )


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Process-wide Supabase client built from SUPABASE_URL + SUPABASE_SECRET_KEY."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_SECRET_KEY", key)) if not v]
    if missing:
        raise DatabaseConfigError(
            f"Missing {', '.join(missing)}. Copy server/.env.example to server/.env and fill it in."
        )
    if "your-project" in url or key.startswith("your-"):
        raise DatabaseConfigError("server/.env still contains the placeholder values from .env.example.")
    _check_secret_key(key)
    return create_client(url, key)


# ------------------------------------------------------------------- queries


def _rows(response: Any) -> list[dict[str, Any]]:
    return list(response.data or [])


def get_exercise_id(name: str, client: Client | None = None) -> int:
    """Look up an exercise definition id by name (e.g. 'squat')."""
    db = client or get_client()
    rows = _rows(db.table("exercises").select("id").eq("name", name).limit(1).execute())
    if not rows:
        raise DatabaseError(f"Unknown exercise {name!r}. Was the seed migration applied?")
    return int(rows[0]["id"])


def create_workout_session(
    user_id: UUID | str, exercise_name: str = "squat", client: Client | None = None
) -> dict[str, Any]:
    """Insert an 'active' session for ``user_id`` and return the new row."""
    db = client or get_client()
    exercise_id = get_exercise_id(exercise_name, db)
    rows = _rows(
        db.table("workout_sessions")
        .insert({"user_id": str(user_id), "exercise_id": exercise_id})
        .execute()
    )
    if len(rows) != 1:
        raise DatabaseError("Session insert returned no row.")
    return rows[0]


def get_workout_session(
    session_id: UUID | str, user_id: UUID | str, client: Client | None = None
) -> dict[str, Any] | None:
    """Return the session only if it belongs to ``user_id``; otherwise None.

    Returning None for "exists but is someone else's" is deliberate: callers
    should answer 404 either way so session ids cannot be probed.
    """
    db = client or get_client()
    rows = _rows(
        db.table("workout_sessions")
        .select("*")
        .eq("id", str(session_id))
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    )
    return rows[0] if rows else None


def list_workout_sessions(
    user_id: UUID | str, limit: int = 20, client: Client | None = None
) -> list[dict[str, Any]]:
    """Newest-first sessions for ``user_id`` (uses idx_sessions_user_started)."""
    db = client or get_client()
    return _rows(
        db.table("workout_sessions")
        .select("*")
        .eq("user_id", str(user_id))
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )
