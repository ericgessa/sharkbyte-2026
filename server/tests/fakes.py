"""In-memory stand-ins for SupabaseStore / SupabaseAuth (same method names)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


class InMemoryStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.reps: dict[str, dict[int, dict]] = {}
        self.events: dict[str, dict[str, dict]] = {}
        self.fail_saves = False
        self.reports: dict[str, dict] = {}  # saved coach reports, one per workout
        self.reports_not_set_up = False  # simulate "the workout_reports migration has not been applied"
        self.report_saves = 0

    def create_session(self, user_id: str, exercise: str) -> dict:
        row = {
            "id": str(uuid4()),
            "user_id": user_id,
            "status": "active",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None,
            "total_reps": 0,
            "exercises": {"name": exercise},
        }
        self.sessions[row["id"]] = row
        return dict(row)

    def get_session(self, session_id, user_id: str) -> dict | None:
        row = self.sessions.get(str(session_id))
        return dict(row) if row and row["user_id"] == user_id else None

    ping_ok = True
    ping_calls = 0

    def ping(self) -> bool:
        self.ping_calls += 1
        return self.ping_ok

    def list_all_session_ids(self, user_id: str) -> list[str]:
        return [sid for sid, r in self.sessions.items() if r["user_id"] == user_id]

    def delete_account(self, user_id: str) -> None:
        if getattr(self, "fail_account_delete", False):
            raise RuntimeError("simulated Supabase failure")
        for sid in self.list_all_session_ids(user_id):
            self.delete_session(sid, user_id)
        self.deleted_users = getattr(self, "deleted_users", []) + [user_id]

    def delete_session(self, session_id, user_id: str) -> bool:
        row = self.sessions.get(str(session_id))
        if not row or row["user_id"] != user_id:
            return False
        del self.sessions[str(session_id)]  # cascade, like Postgres
        self.reports.pop(str(session_id), None)
        self.reps.pop(str(session_id), None)
        self.events.pop(str(session_id), None)
        return True

    def list_sessions(self, user_id: str, limit: int) -> list[dict]:
        rows = [dict(r) for r in self.sessions.values() if r["user_id"] == user_id]
        return sorted(rows, key=lambda r: r["started_at"], reverse=True)[:limit]

    def list_repetitions(self, session_id) -> list[dict]:
        reps = self.reps.get(str(session_id), {})
        return [
            {"rep_number": n, "metrics": reps[n], "form_score": None, "created_at": datetime.now(timezone.utc).isoformat()}
            for n in sorted(reps)
        ]

    def list_coaching_events(self, session_id) -> list[dict]:
        return [
            {**e, "created_at": datetime.now(timezone.utc).isoformat()}
            for e in self.events.get(str(session_id), {}).values()
        ]

    def save_repetition(self, session_id, rep_number: int, metrics: dict) -> None:
        if self.fail_saves:
            raise RuntimeError("simulated database outage")
        self.reps.setdefault(str(session_id), {})[rep_number] = metrics  # idempotent, like the upsert

    def save_coaching_event(self, session_id, event_id, rep_number, issue_code, message, action) -> None:
        if self.fail_saves:
            raise RuntimeError("simulated database outage")
        self.events.setdefault(str(session_id), {})[str(event_id)] = {
            "rep_number": rep_number, "issue_code": issue_code, "message": message, "action": action,
        }

    def get_report(self, session_id) -> dict | None:
        if self.reports_not_set_up:
            return None
        r = self.reports.get(str(session_id))
        return dict(r) if r else None

    def save_report(self, session_id, summary: str, source: str, model: str | None = None) -> None:
        if self.reports_not_set_up:
            from app.services.database import ReportsNotSetUpError

            raise ReportsNotSetUpError()
        now = datetime.now(timezone.utc).isoformat()
        old = self.reports.get(str(session_id))
        self.reports[str(session_id)] = {"summary": summary, "source": source, "model": model, "created_at": old["created_at"] if old else now, "updated_at": now}
        self.report_saves += 1

    def count_repetitions(self, session_id) -> int:
        return len(self.reps.get(str(session_id), {}))

    def finish_session(self, session: dict, total_reps: int) -> dict | None:
        row = self.sessions[str(session["id"])]
        if row["status"] != "active":
            return None
        row.update(status="completed", total_reps=total_reps, ended_at=datetime.now(timezone.utc).isoformat())
        return dict(row)


class FakeAuth:
    TOKENS = {"alice-token": "user-alice", "bob-token": "user-bob"}

    def __init__(self) -> None:
        self.tokens = dict(self.TOKENS)
        self.invalidated: list[str] = []

    def verify(self, access_token: str) -> str | None:
        return self.tokens.get(access_token)

    def invalidate_user(self, user_id: str) -> None:
        self.invalidated.append(user_id)
        self.tokens = {t: u for t, u in self.tokens.items() if u != user_id}  # like a deleted user's tokens dying


ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


def auth_message(token: str = "alice-token") -> dict:
    return {"schema_version": 1, "type": "auth", "access_token": token}


class FakeTiger:
    """Stands in for TigerStore: records inserts, serves canned query results, can simulate an outage."""

    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.fail = False
        self.rows: list = []  # SensorReading objects that were "inserted"
        self.batches: list[int] = []
        self.queries: list[tuple[str, object]] = []
        self.result: object = []  # list of dict rows, or a callable(sql, params) -> list

    def insert_readings(self, readings) -> int:
        if self.fail:
            raise RuntimeError("simulated Tiger Data outage")
        self.rows.extend(readings)
        self.batches.append(len(readings))
        return len(readings)

    def delete_sessions_readings(self, session_ids) -> int:
        if self.fail:
            raise RuntimeError("simulated Tiger Data outage")
        ids = {str(s) for s in session_ids}
        before = len(self.rows)
        self.rows = [r for r in self.rows if str(r.session_id) not in ids]
        return before - len(self.rows)

    def delete_session_readings(self, session_id) -> int:
        if self.fail:
            raise RuntimeError("simulated Tiger Data outage")
        before = len(self.rows)
        self.rows = [r for r in self.rows if str(r.session_id) != str(session_id)]
        return before - len(self.rows)

    def query(self, sql, params=None, timeout_ms=10_000):
        if self.fail:
            raise RuntimeError("simulated Tiger Data outage")
        self.queries.append((sql, params))
        return self.result(sql, params) if callable(self.result) else list(self.result)  # type: ignore[arg-type]

    def close(self) -> None:
        pass


class InMemoryGameStore:
    """Stands in for GameStore (GRIT Games results and finished races)."""

    def __init__(self) -> None:
        self.results: list[dict] = []
        self.races: dict[str, dict] = {}
        self.not_set_up = False  # simulate "the migration has not been applied"
        self.fail_race_save = False
        self._clock = 0

    def _check(self) -> None:
        if self.not_set_up:
            from app.services.games_db import GamesNotSetUpError

            raise GamesNotSetUpError()

    def create_result(self, row: dict) -> dict:
        self._check()
        if row.get("workout_session_id") and any(r.get("workout_session_id") == row["workout_session_id"] for r in self.results):
            raise RuntimeError("duplicate key value violates unique constraint uq_game_results_workout")
        self._clock += 1
        saved = {**row, "id": str(uuid4()), "created_at": f"2026-11-08T10:00:{self._clock:02d}+00:00"}
        self.results.append(saved)
        return dict(saved)

    def result_for_workout(self, session_id, user_id: str) -> dict | None:
        self._check()
        return next((dict(r) for r in self.results if r.get("workout_session_id") == str(session_id) and r["user_id"] == user_id), None)

    def get_result(self, result_id, user_id: str) -> dict | None:
        self._check()
        return next((dict(r) for r in self.results if r["id"] == str(result_id) and r["user_id"] == user_id), None)

    def list_results(self, user_id: str, game=None, exercise=None, limit: int = 20) -> list[dict]:
        self._check()
        rows = [dict(r) for r in self.results if r["user_id"] == user_id and (not game or r["game"] == game) and (not exercise or r["exercise"] == exercise)]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    def save_race(self, room: dict, players: list[dict], results: list[dict]) -> str:
        self._check()
        if self.fail_race_save:
            raise RuntimeError("simulated database outage")
        room_id = str(uuid4())
        self.races[room_id] = {**room, "id": room_id, "players": [dict(p, room_id=room_id) for p in players]}
        for r in results:
            self.create_result({**r, "room_id": room_id})
        return room_id

    def get_race(self, room_id, user_id: str) -> dict | None:
        self._check()
        race = self.races.get(str(room_id))
        if race is None or not any(p["user_id"] == user_id for p in race["players"]):
            return None
        return dict(race)

    def ping(self) -> bool:
        return not self.not_set_up
