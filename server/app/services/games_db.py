"""Supabase access for GRIT Games results and finished races. Trusted server-side code only (secret key).

Same rules as ``database.py``: every query that touches a user's data filters on ``user_id`` that the API has
already authenticated, and the client is synchronous, so callers use ``asyncio.to_thread`` (or a plain ``def``
endpoint). The tables come from ``supabase/migrations/20261108000001_grit_games.sql``; until that is applied
every call raises ``GamesNotSetUpError`` and the API answers 503 with the fix.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError
from supabase import Client

from app.services.database import get_client

MIGRATION_FILE = "supabase/migrations/20261108000001_grit_games.sql"
RESULT_COLUMNS = (
    "id,user_id,game,mode,exercise,valid_reps,score,duration_ms,target_reps,time_limit_s,completed,"
    "form_feedback,rep_times_ms,workout_session_id,room_id,created_at"
)


class GamesNotSetUpError(RuntimeError):
    """The GRIT Games tables do not exist in this Supabase project yet."""

    def __init__(self) -> None:
        super().__init__(f"GRIT Games tables are missing: apply {MIGRATION_FILE} (see SUPABASE_SETUP.md)")


def _guard(exc: APIError) -> Exception:
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')}"
    if "PGRST205" in text or "42P01" in text or "does not exist" in text or "schema cache" in text:
        return GamesNotSetUpError()
    return exc


def _rows(response: Any) -> list[dict[str, Any]]:
    return list(response.data or [])


class GameStore:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        return self._client or get_client()

    # ------------------------------------------------------------ results

    def create_result(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            rows = _rows(self.client.table("game_results").insert(row).execute())
        except APIError as exc:
            raise _guard(exc) from exc
        return rows[0]

    def result_for_workout(self, session_id: UUID | str, user_id: str) -> dict[str, Any] | None:
        try:
            rows = _rows(
                self.client.table("game_results")
                .select(RESULT_COLUMNS)
                .eq("workout_session_id", str(session_id))
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            )
        except APIError as exc:
            raise _guard(exc) from exc
        return rows[0] if rows else None

    def get_result(self, result_id: UUID | str, user_id: str) -> dict[str, Any] | None:
        try:
            rows = _rows(
                self.client.table("game_results")
                .select(RESULT_COLUMNS)
                .eq("id", str(result_id))
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            )
        except APIError as exc:
            raise _guard(exc) from exc
        return rows[0] if rows else None

    def list_results(
        self, user_id: str, game: str | None = None, exercise: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The user's camera-tracked results, newest first."""
        try:
            q = self.client.table("game_results").select(RESULT_COLUMNS).eq("user_id", str(user_id))
            if game:
                q = q.eq("game", game)
            if exercise:
                q = q.eq("exercise", exercise)
            return _rows(q.order("created_at", desc=True).limit(limit).execute())
        except APIError as exc:
            raise _guard(exc) from exc

    # ------------------------------------------------------------ races

    def save_race(
        self, room: dict[str, Any], players: list[dict[str, Any]], results: list[dict[str, Any]]
    ) -> str:
        """Store a finished 1v1 race: the room, both players' lines and each player's own result. Returns the room id."""
        try:
            room_id = _rows(self.client.table("game_rooms").insert(room).execute())[0]["id"]
            self.client.table("game_room_players").insert([{**p, "room_id": room_id} for p in players]).execute()
            self.client.table("game_results").insert([{**r, "room_id": room_id} for r in results]).execute()
        except APIError as exc:
            raise _guard(exc) from exc
        return str(room_id)

    def get_race(self, room_id: UUID | str, user_id: str) -> dict[str, Any] | None:
        """A finished race with both players' lines, only if ``user_id`` played in it."""
        try:
            players = _rows(self.client.table("game_room_players").select("*").eq("room_id", str(room_id)).execute())
            if not any(p["user_id"] == str(user_id) for p in players):
                return None
            room = _rows(self.client.table("game_rooms").select("*").eq("id", str(room_id)).limit(1).execute())
        except APIError as exc:
            raise _guard(exc) from exc
        if not room:
            return None
        return {**room[0], "players": players}

    def ping(self) -> bool:
        try:
            self.client.table("game_results").select("id").limit(1).execute()
            return True
        except Exception:
            return False
