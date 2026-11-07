"""LIVE check of ACCOUNT deletion across Supabase + Tiger Data, using two throwaway users.

    python -m scripts.e2e_account        (run from server/)

User A (two workouts with data) deletes their account through the API; user B (one workout) must be untouched.
Verifies: A's login is gone, A's workouts/reps/coaching events are gone (Supabase), A's sensor readings are gone
(Tiger Data), A's token stops working at once, and B and B's data survive. Cleans up B afterwards.
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
from app.services.database import DatabaseConfigError, get_client
from app.services.timeseries import TigerDataConfigError, TigerStore, tigerdata_url
from app.simulation import squat_messages
from scripts._cleanup import arm_watchdog, purge_tiger_readings

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def main() -> int:
    try:
        admin = get_client()
        tigerdata_url()
    except (DatabaseConfigError, TigerDataConfigError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    tiger = TigerStore()
    users: dict[str, object] = {}
    created: list[str] = []
    watchdog = arm_watchdog(
        240, lambda: (purge_tiger_readings(created), [admin.auth.admin.delete_user(u.id) for u in users.values()])
    )

    def count(table: str, col: str, val: str) -> int:
        return admin.table(table).select(col, count="exact").eq(col, val).limit(0).execute().count

    def tiger_count(sid: str) -> int:
        return tiger.query("select count(*) as n from sensor_readings where session_id = %s", (sid,))[0]["n"]

    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        tokens = {}
        for tag in ("a", "b"):
            email, pw = f"account-{tag}-{uuid4().hex[:8]}@example.com", secrets.token_urlsafe(18)
            users[tag] = admin.auth.admin.create_user({"email": email, "password": pw, "email_confirm": True}).user
            tokens[tag] = create_client(os.environ["SUPABASE_URL"].strip(), key).auth.sign_in_with_password(
                {"email": email, "password": pw}).session.access_token
        h = {t: {"Authorization": f"Bearer {tok}"} for t, tok in tokens.items()}

        with TestClient(create_app()) as client:
            def workout(tag: str) -> str:
                sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=h[tag]).json()["session_id"]
                created.append(sid)
                with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                    ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": tokens[tag]}))
                    ws.receive_json()
                    for m in squat_messages(sid, reps=5, depths=[85, 85, 85, 98, 98]):
                        ws.send_text(json.dumps(m))
                    [ws.receive_json() for _ in range(6)]
                client.post(f"/api/workouts/{sid}/finish", headers=h[tag])
                return sid

            a1, a2, b1 = workout("a"), workout("a"), workout("b")
            check("setup: A has 2 workouts with data in both databases",
                  all(count("repetitions", "session_id", s) == 5 and tiger_count(s) > 100 for s in (a1, a2)))
            check("setup: B has 1 workout with data", count("repetitions", "session_id", b1) == 5 and tiger_count(b1) > 100)

            bad = client.post("/api/account/delete", json={"confirm": "yes please"}, headers=h["a"])
            check("wrong confirmation phrase -> 422 and nothing deleted", bad.status_code == 422 and count("workout_sessions", "id", a1) == 1)
            check("no token -> 401", client.post("/api/account/delete", json={"confirm": "delete my account"}).status_code == 401)

            r = client.post("/api/account/delete", json={"confirm": "delete my account"}, headers=h["a"])
            check("POST /api/account/delete -> 204", r.status_code == 204)
            gone_ids = {u.id for u in admin.auth.admin.list_users()}
            check("Supabase Auth: A's login no longer exists", users["a"].id not in gone_ids)
            check("Supabase: A's workouts, reps and coaching events are gone",
                  all(count("workout_sessions", "id", s) == 0 and count("repetitions", "session_id", s) == 0
                      and count("coaching_events", "session_id", s) == 0 for s in (a1, a2)))
            check("Supabase: A's profile is gone", count("profiles", "id", users["a"].id) == 0)
            check("Tiger Data: all of A's sensor readings are gone", tiger_count(a1) == 0 and tiger_count(a2) == 0)
            check("A's token stops working immediately (401)", client.get("/api/workouts", headers=h["a"]).status_code == 401)
            check("B is untouched: login, workout, reps and sensor data",
                  users["b"].id in gone_ids and count("repetitions", "session_id", b1) == 5 and tiger_count(b1) > 100
                  and client.get("/api/workouts", headers=h["b"]).status_code == 200)
    finally:
        watchdog.cancel()
        purge_tiger_readings(created, settle_s=1.0)
        for tag, u in users.items():
            if tag == "b":  # A was deleted by the API itself (the point of the test)
                admin.auth.admin.delete_user(u.id)
        time.sleep(0.3)
        tiger.close()
        print("cleaned up: user B, and any Tiger Data readings created")

    failed = [n for n, ok in results if not ok]
    print("\nE2E FAILED: " + "; ".join(failed) if failed else "\nAccount-deletion E2E passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
