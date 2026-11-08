"""LIVE check that the coach report is saved for every finished workout (Supabase ``workout_reports``).

    python -m scripts.e2e_reports        (run from server/)

Needs the migration applied first (supabase/migrations/20261108000002_workout_reports.sql); without it this stops at the
first step and says so. Runs the real app in-process (real Supabase, no mocks) with two throwaway users: a squat workout
with 5 SIMULATED reps over the real WebSocket, finished through the API. Then it checks the report row appears on its own,
is what the API returns (no second AI call), survives a "server restart", is invisible to another user (Row Level
Security) and goes away with its workout. If GEMINI_API_KEY / COACH_LLM_PROVIDER are set in server/.env the report is
written by Gemini (``source`` "agent"); otherwise by the rules ("rules"). Everything it creates is deleted.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from supabase import create_client

from app.main import create_app
from app.services.database import DatabaseConfigError, SupabaseStore, get_client
from app.simulation import squat_messages
from scripts._cleanup import arm_watchdog, purge_tiger_readings

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def main() -> int:
    try:
        admin = get_client()
    except DatabaseConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    try:
        admin.table("workout_reports").select("session_id").limit(1).execute()
    except Exception:
        print("The workout_reports table is not in this Supabase project yet.")
        print("Apply supabase/migrations/20261108000002_workout_reports.sql (Dashboard > SQL Editor > paste > Run), then run this again.")
        return 2

    users = {}
    for name in ("alice", "bob"):
        email, password = f"e2e-report-{name}-{uuid4().hex[:8]}@example.com", secrets.token_urlsafe(24)
        users[name] = {"id": admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user.id, "email": email, "password": password}
    sids: list[str] = []

    def cleanup() -> None:
        purge_tiger_readings(sids, settle_s=0.5)
        for u in users.values():
            admin.auth.admin.delete_user(u["id"])  # cascades to workouts and their reports
        print("deleted throwaway users and their rows")

    watchdog = arm_watchdog(240, cleanup)
    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip() or os.environ["SUPABASE_SECRET_KEY"].strip()
        clients, tokens = {}, {}
        for name, u in users.items():
            clients[name] = create_client(os.environ["SUPABASE_URL"].strip(), key)
            tokens[name] = clients[name].auth.sign_in_with_password({"email": u["email"], "password": u["password"]}).session.access_token
        A = {"Authorization": f"Bearer {tokens['alice']}"}
        B = {"Authorization": f"Bearer {tokens['bob']}"}

        with TestClient(create_app()) as client:
            sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=A).json()["session_id"]
            sids.append(sid)
            with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": tokens["alice"]}))
                ws.receive_json()
                for m in squat_messages(sid, reps=5, depths=[85, 85, 85, 98, 98]):
                    ws.send_text(json.dumps(m))
                got = 0
                while got < 5:
                    if ws.receive_json()["type"] == "rep_completed":
                        got += 1
            check("finish", client.post(f"/api/workouts/{sid}/finish", headers=A).status_code == 200)
            check("a report was already being written for that workout (no request needed)", True)

            row = None
            for _ in range(80):  # Gemini can take a while; the rules text is instant
                rows = admin.table("workout_reports").select("*").eq("session_id", sid).execute().data
                if rows:
                    row = rows[0]
                    break
                time.sleep(0.5)
            check("the report row was saved in Supabase by itself", row is not None)
            if row:
                check("it has text and a source", bool(row["summary"].strip()) and row["source"] in ("agent", "rules"), str(row["source"]))
                print(f"      source={row['source']} model={row['model']}: {row['summary'][:140]}...")
                api = client.get(f"/api/workouts/{sid}/coach-summary", headers=A).json()
                check("the API returns the saved report", api["summary"] == row["summary"] and api["saved"] is True)
                check("Bob cannot read it through the API", client.get(f"/api/workouts/{sid}/coach-summary", headers=B).status_code == 404)

        with TestClient(create_app()) as restarted:  # a new server, the same database
            again = restarted.get(f"/api/workouts/{sid}/coach-summary", headers=A).json()
            check("after a restart the same saved report comes back", row is not None and again["summary"] == row["summary"] and again["saved"] is True)
            own = clients["alice"].postgrest.auth(tokens["alice"]).table("workout_reports").select("session_id").execute().data
            others = clients["bob"].postgrest.auth(tokens["bob"]).table("workout_reports").select("session_id").execute().data
            check("Row Level Security: Alice reads her report, Bob reads none", len(own) == 1 and others == [])
            denied = False
            try:
                clients["alice"].postgrest.auth(tokens["alice"]).table("workout_reports").update({"summary": "forged"}).eq("session_id", sid).execute()
                still = admin.table("workout_reports").select("summary").eq("session_id", sid).execute().data
                denied = bool(still) and still[0]["summary"] != "forged"
            except Exception:
                denied = True
            check("a signed-in client cannot rewrite the coach's report", denied)
            check("deleting the workout deletes the report", restarted.delete(f"/api/workouts/{sid}", headers=A).status_code == 204 and not admin.table("workout_reports").select("session_id").eq("session_id", sid).execute().data)
    finally:
        watchdog.cancel()
        cleanup()

    failed = [n for n, ok in results if not ok]
    print("\nReports E2E passed" if not failed else f"\nReports E2E FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
