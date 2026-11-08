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
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from supabase import Client, create_client

from app.services.exercises import find as find_exercise

load_dotenv()  # reads server/.env when present; real environment variables win


class DatabaseConfigError(RuntimeError):
    """Missing or unsuitable Supabase configuration (message never contains a key)."""


class DatabaseError(RuntimeError):
    """A query failed or returned something unexpected."""


class ReportsNotSetUpError(RuntimeError):
    """The ``workout_reports`` table does not exist in this Supabase project yet (apply the migration)."""

    def __init__(self) -> None:
        super().__init__("workout_reports table is missing: apply supabase/migrations/20261108000002_workout_reports.sql")


def _reports_missing(exc: APIError) -> bool:
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')}"
    return "PGRST205" in text or "42P01" in text or "schema cache" in text or "does not exist" in text


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


def ensure_exercise(name: str, config: dict[str, Any], client: Client | None = None) -> int:
    """The id of exercise ``name``, creating its row (with ``config``) the first time it is used.

    This is why adding an exercise to the code registry needs no database migration. An existing row
    (e.g. the seeded squat row) is never modified, and two requests racing to create it are safe.
    """
    db = client or get_client()
    rows = _rows(db.table("exercises").select("id").eq("name", name).limit(1).execute())
    if rows:
        return int(rows[0]["id"])
    db.table("exercises").upsert({"name": name, "config": config}, on_conflict="name", ignore_duplicates=True).execute()
    return get_exercise_id(name, db)


def create_workout_session(
    user_id: UUID | str,
    exercise_name: str = "squat",
    client: Client | None = None,
    exercise_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert an 'active' session for ``user_id`` and return the new row.

    With ``exercise_config`` the exercise row is created if missing; without it the exercise must exist.
    """
    db = client or get_client()
    exercise_id = (
        ensure_exercise(exercise_name, exercise_config, db) if exercise_config is not None else get_exercise_id(exercise_name, db)
    )
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
        .select("*, exercises(name)")
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
        .select("*, exercises(name)")
        .eq("user_id", str(user_id))
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )


def delete_workout_session(session_id: UUID | str, user_id: UUID | str, client: Client | None = None) -> bool:
    """Delete one of ``user_id``'s sessions. Reps and coaching events go with it (ON DELETE CASCADE).

    Returns False if no such session belongs to the user. The caller must also delete the session's
    Tiger Data readings: that database is separate and does not cascade.
    """
    db = client or get_client()
    rows = _rows(db.table("workout_sessions").delete().eq("id", str(session_id)).eq("user_id", str(user_id)).execute())
    return bool(rows)


def list_repetitions(session_id: UUID | str, client: Client | None = None) -> list[dict[str, Any]]:
    """Saved reps of a session in order. Caller must have verified ownership."""
    db = client or get_client()
    return _rows(
        db.table("repetitions")
        .select("rep_number, metrics, form_score, created_at")
        .eq("session_id", str(session_id))
        .order("rep_number")
        .execute()
    )


def list_coaching_events(session_id: UUID | str, client: Client | None = None) -> list[dict[str, Any]]:
    """Coaching events of a session in time order. Caller must have verified ownership."""
    db = client or get_client()
    return _rows(
        db.table("coaching_events")
        .select("rep_number, issue_code, message, action, created_at")
        .eq("session_id", str(session_id))
        .order("created_at")
        .execute()
    )


def save_repetition(
    session_id: UUID | str,
    rep_number: int,
    metrics: dict[str, Any],
    form_score: float | None = None,
    client: Client | None = None,
) -> None:
    """Idempotently store one rep (upsert on session_id + rep_number).

    The caller must already have verified that the session belongs to the
    authenticated user; this function does not re-check ownership.
    """
    db = client or get_client()
    db.table("repetitions").upsert(
        {
            "session_id": str(session_id),
            "rep_number": rep_number,
            "metrics": metrics,
            "form_score": form_score,
        },
        on_conflict="session_id,rep_number",
    ).execute()


def save_coaching_event(
    session_id: UUID | str,
    event_id: UUID | str,
    rep_number: int | None,
    issue_code: str | None,
    message: str,
    action: str | None,
    client: Client | None = None,
) -> None:
    """Idempotently store one coaching event (upsert on id; the caller supplies the id)."""
    db = client or get_client()
    db.table("coaching_events").upsert(
        {
            "id": str(event_id),
            "session_id": str(session_id),
            "rep_number": rep_number,
            "issue_code": issue_code,
            "message": message,
            "action": action,
        },
        on_conflict="id",
    ).execute()


def count_repetitions(session_id: UUID | str, client: Client | None = None) -> int:
    db = client or get_client()
    res = db.table("repetitions").select("id", count="exact").eq("session_id", str(session_id)).limit(0).execute()
    return int(res.count or 0)


def finish_workout_session(
    session: dict[str, Any], total_reps: int, client: Client | None = None
) -> dict[str, Any] | None:
    """Mark an *active* session completed. ``session`` is the ownership-checked row.

    ``ended_at`` is never earlier than ``started_at`` even if this machine's clock
    is behind the database's (the table has a CHECK for that).
    """
    db = client or get_client()
    started = datetime.fromisoformat(str(session["started_at"]).replace("Z", "+00:00"))
    ended = max(datetime.now(timezone.utc), started)
    rows = _rows(
        db.table("workout_sessions")
        .update({"status": "completed", "ended_at": ended.isoformat(), "total_reps": total_reps})
        .eq("id", str(session["id"]))
        .eq("user_id", str(session["user_id"]))
        .eq("status", "active")
        .execute()
    )
    return rows[0] if rows else None


class SupabaseStore:
    """What the API layer talks to. Tests substitute an in-memory fake with the same methods.

    The client is created lazily so importing/creating the app needs no credentials.
    """

    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        return self._client or get_client()

    def create_session(self, user_id: str, exercise: str) -> dict[str, Any]:
        known = find_exercise(exercise)  # the registry knows how to describe a brand-new exercise row
        row = create_workout_session(
            user_id, exercise, self.client, exercise_config=known.db_config() if known else None
        )
        row["exercises"] = {"name": exercise}
        return row

    def get_session(self, session_id: UUID, user_id: str) -> dict[str, Any] | None:
        return get_workout_session(session_id, user_id, self.client)

    def list_sessions(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        return list_workout_sessions(user_id, limit, self.client)

    def ping(self) -> bool:
        """True if Supabase answers a trivial query (used by the readiness endpoint)."""
        try:
            self.client.table("exercises").select("id").limit(1).execute()
            return True
        except Exception:
            return False

    def list_all_session_ids(self, user_id: str) -> list[str]:
        """Every workout id of a user (paged, no cap): needed to delete their sensor data on account deletion."""
        out: list[str] = []
        start = 0
        while True:
            rows = _rows(
                self.client.table("workout_sessions").select("id").eq("user_id", str(user_id)).order("id").range(start, start + 999).execute()
            )
            out += [r["id"] for r in rows]
            if len(rows) < 1000:
                return out
            start += 1000

    def delete_account(self, user_id: str) -> None:
        """Delete the Supabase Auth user. The database cascades: profile -> workouts -> reps and coaching events."""
        self.client.auth.admin.delete_user(str(user_id))

    def delete_session(self, session_id: UUID, user_id: str) -> bool:
        return delete_workout_session(session_id, user_id, self.client)

    def list_repetitions(self, session_id: UUID) -> list[dict[str, Any]]:
        return list_repetitions(session_id, self.client)

    def list_coaching_events(self, session_id: UUID) -> list[dict[str, Any]]:
        return list_coaching_events(session_id, self.client)

    def save_repetition(self, session_id: UUID, rep_number: int, metrics: dict[str, Any]) -> None:
        save_repetition(session_id, rep_number, metrics, client=self.client)

    def save_coaching_event(
        self,
        session_id: UUID,
        event_id: UUID,
        rep_number: int | None,
        issue_code: str | None,
        message: str,
        action: str | None,
    ) -> None:
        save_coaching_event(session_id, event_id, rep_number, issue_code, message, action, client=self.client)

    def count_repetitions(self, session_id: UUID) -> int:
        return count_repetitions(session_id, self.client)

    # ---- the saved coach report (one per workout)

    def get_report(self, session_id: UUID | str) -> dict[str, Any] | None:
        """The saved report, or None if there is none yet (or the table has not been created: then nothing is saved)."""
        try:
            rows = _rows(
                self.client.table("workout_reports")
                .select("summary,source,model,created_at,updated_at")
                .eq("session_id", str(session_id))
                .limit(1)
                .execute()
            )
        except APIError as exc:
            if _reports_missing(exc):
                return None
            raise
        return rows[0] if rows else None

    def save_report(self, session_id: UUID | str, summary: str, source: str, model: str | None = None) -> None:
        """Insert or replace the workout's report. The caller has already checked that the workout is the user's."""
        try:
            self.client.table("workout_reports").upsert(
                {
                    "session_id": str(session_id),
                    "summary": summary,
                    "source": source,
                    "model": model,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="session_id",
            ).execute()
        except APIError as exc:
            if _reports_missing(exc):
                raise ReportsNotSetUpError() from exc
            raise

    def finish_session(self, session: dict[str, Any], total_reps: int) -> dict[str, Any] | None:
        row = finish_workout_session(session, total_reps, self.client)
        if row is not None:
            row["exercises"] = session.get("exercises")
        return row
