"""LIVE check of deletion across BOTH databases (Supabase + Tiger Data).

    python -m scripts.e2e_delete        (run from server/)

Needs Supabase and Tiger Data configured in server/.env. Uses a throwaway user and cleans up after itself.

  1. DELETE /api/workouts/{id} removes the workout, its reps and coaching events (Supabase) AND its sensor
     readings (Tiger Data), and leaves a second workout of the same user untouched.
  2. A workout deleted DIRECTLY in Supabase (what the dashboard / account removal does) leaves orphaned readings
     in Tiger Data; scripts.purge_orphan_readings finds and removes them.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
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
# Simulated workouts stream ~15 s of data in well under a second, so their newest reading is a few seconds in the
# FUTURE. A real workout ends in the past, so the tool's default 1 h margin is right; here a negative margin
# makes simulated data count as "old enough".
MARGIN = "-0.1"


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
    email, password = f"delete-{uuid4().hex[:10]}@example.com", secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user
    created: list[str] = []
    watchdog = arm_watchdog(240, lambda: (purge_tiger_readings(created), admin.auth.admin.delete_user(user.id)))

    def tiger_count(sid: str) -> int:
        return tiger.query("select count(*) as n from sensor_readings where session_id = %s", (sid,))[0]["n"]

    def sb_count(table: str, sid: str) -> int:
        col = "id" if table == "workout_sessions" else "session_id"
        return admin.table(table).select(col, count="exact").eq(col, sid).limit(0).execute().count

    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        token = create_client(os.environ["SUPABASE_URL"].strip(), key).auth.sign_in_with_password(
            {"email": email, "password": password}
        ).session.access_token
        h = {"Authorization": f"Bearer {token}"}

        def run_workout(client: TestClient, depths, events: int = 6) -> str:
            sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=h).json()["session_id"]
            created.append(sid)
            with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
                ws.receive_json()
                for m in squat_messages(sid, reps=5, depths=depths):
                    ws.send_text(json.dumps(m))
                [ws.receive_json() for _ in range(events)]  # 5 reps (+ 1 coaching message when depth drops)
            assert client.post(f"/api/workouts/{sid}/finish", headers=h).status_code == 200
            return sid

        with TestClient(create_app()) as client:
            keep = run_workout(client, [85, 85, 85, 98, 98])
            gone = run_workout(client, [85, 85, 85, 98, 98])
            check("setup: both workouts stored in both databases", all(
                sb_count("repetitions", s) == 5 and tiger_count(s) > 100 for s in (keep, gone)))
            check("setup: a coaching event exists for the workout to be deleted", sb_count("coaching_events", gone) == 1)

            # --- 1) delete through the API -------------------------------------------------
            check("an invalid token cannot delete it (401)", client.delete(
                f"/api/workouts/{gone}", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401)
            r = client.delete(f"/api/workouts/{gone}", headers=h)
            check("DELETE /api/workouts/{id} -> 204", r.status_code == 204)
            check("Supabase: session, reps and coaching events all gone",
                  sb_count("workout_sessions", gone) == 0 and sb_count("repetitions", gone) == 0 and sb_count("coaching_events", gone) == 0)
            check("Tiger Data: its readings are gone", tiger_count(gone) == 0)
            check("the OTHER workout is untouched in both databases",
                  sb_count("workout_sessions", keep) == 1 and sb_count("repetitions", keep) == 5 and tiger_count(keep) > 100)
            check("deleting again -> 404", client.delete(f"/api/workouts/{gone}", headers=h).status_code == 404)
            check("history no longer lists it", [w["session_id"] for w in client.get("/api/workouts", headers=h).json()] == [keep])

            # --- 2) orphans from a direct Supabase delete ----------------------------------
            orphan = run_workout(client, [85, 85, 85, 85, 85], events=5)  # steady depth: no coaching message
            check("setup: third workout has readings", tiger_count(orphan) > 100)
            admin.table("workout_sessions").delete().eq("id", orphan).execute()  # what the dashboard does
            check("after a direct Supabase delete the readings are orphaned (the gap)", sb_count("workout_sessions", orphan) == 0 and tiger_count(orphan) > 100)

        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        dry = subprocess.run([sys.executable, "-m", "scripts.purge_orphan_readings", "--older-than-hours", MARGIN],
                             capture_output=True, text=True, env=env)
        check("purge tool (dry run) lists the orphan and deletes nothing", orphan in dry.stdout and tiger_count(orphan) > 100, dry.stdout[-200:])
        others = [line for line in dry.stdout.splitlines() if line.startswith("  ") and orphan not in line]
        if others:  # never blanket-delete somebody else's garbage from a test: remove only ours
            print(f"      note: {len(others)} unrelated orphan workout(s) exist in Tiger Data; deleting only this test's")
            tiger.delete_session_readings(orphan)
        else:
            applied = subprocess.run([sys.executable, "-m", "scripts.purge_orphan_readings", "--older-than-hours", MARGIN, "--apply"],
                                     capture_output=True, text=True, env=env)
            check("purge tool --apply reports the deletion", "Deleted" in applied.stdout, applied.stdout[-200:])
        check("Tiger Data: orphaned readings are gone", tiger_count(orphan) == 0)
        check("the kept workout's readings survived the purge", tiger_count(keep) > 100)
    finally:
        watchdog.cancel()
        purge_tiger_readings(created, settle_s=1.0)
        admin.auth.admin.delete_user(user.id)
        tiger.close()
        print("cleaned up: throwaway user, its Supabase rows and all Tiger Data readings it created")

    failed = [n for n, ok in results if not ok]
    print("\nE2E FAILED: " + "; ".join(failed) if failed else "\nDeletion E2E passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
