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

    def verify(self, access_token: str) -> str | None:
        return self.TOKENS.get(access_token)


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

    def query(self, sql, params=None, timeout_ms=10_000):
        if self.fail:
            raise RuntimeError("simulated Tiger Data outage")
        self.queries.append((sql, params))
        return self.result(sql, params) if callable(self.result) else list(self.result)  # type: ignore[arg-type]

    def close(self) -> None:
        pass
