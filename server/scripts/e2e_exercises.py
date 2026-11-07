"""LIVE check of multi-exercise support against the Supabase project in server/.env.

    python -m scripts.e2e_exercises        (run from server/)

Throwaway user -> a workout for EVERY registered exercise (using sloppy spellings) through the real app
and real Supabase -> checks the exercise rows were created on first use, squat still counts and saves
reps, and non-squat workouts are logged but get no counted reps or coaching. Deletes the user afterwards.

Note: the exercise ROWS it creates in the ``exercises`` table are kept on purpose; they are reference data
the app would create on first use anyway.
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
from app.services import exercises as ex
from app.services.database import DatabaseConfigError, get_client
from app.simulation import squat_messages

SPELLINGS = {"squat": "Squats", "push-up": "Push Up", "pull-up": "pullup", "deadlift": "DEAD LIFT",
             "bench-press": "bench_press", "lunge": "lunges", "plank": "PLANK"}
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

    email, password = f"exercises-{uuid4().hex[:10]}@example.com", secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user
    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        token = create_client(os.environ["SUPABASE_URL"].strip(), key).auth.sign_in_with_password(
            {"email": email, "password": password}
        ).session.access_token
        h = {"Authorization": f"Bearer {token}"}

        with TestClient(create_app()) as client:
            listing = client.get("/api/exercises")
            check("GET /api/exercises lists all 7 without a login", listing.status_code == 200 and len(listing.json()) == 7)

            bad = client.post("/api/workouts", json={"exercise": "burpee"}, headers=h)
            check("unsupported exercise -> 422 naming the supported ones", bad.status_code == 422 and "push-up" in bad.text)

            sessions = {}
            for slug, spelling in SPELLINGS.items():
                r = client.post("/api/workouts", json={"exercise": spelling}, headers=h)
                ok = r.status_code == 201 and r.json()["exercise"] == slug and r.json()["analysis_supported"] is (slug == "squat")
                check(f"create workout for {spelling!r} -> canonical {slug!r}", ok, r.text[:120])
                if r.status_code == 201:
                    sessions[slug] = r.json()["session_id"]

            rows = {r["name"]: r["config"] for r in admin.table("exercises").select("name,config").execute().data}
            check("Supabase has an exercises row for every exercise", set(SPELLINGS) <= set(rows))
            check("squat's seeded config was left untouched", rows.get("squat", {}).get("bottom_deg") == 110)
            check("new rows say honestly that analysis is not supported", all(rows[s]["analysis_supported"] is False for s in SPELLINGS if s != "squat" and s in rows))

            # squat: unchanged behavior
            sid = sessions["squat"]
            with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
                ready = ws.receive_json()
                for m in squat_messages(sid, reps=3):
                    ws.send_text(json.dumps(m))
                evs = [ws.receive_json() for _ in range(3)]
            check("squat: analysis on, 3 reps counted", ready["analysis_supported"] is True and [e["rep_count"] for e in evs] == [1, 2, 3])
            check("squat: finish saves 3 reps", client.post(f"/api/workouts/{sid}/finish", headers=h).json()["total_reps"] == 3)

            # every other exercise: logged, not analyzed
            for slug, sid in sessions.items():
                if slug == "squat":
                    continue
                with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                    ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
                    ready = ws.receive_json()
                    for m in squat_messages(sid, reps=2):  # would be 2 squat reps if mis-routed
                        ws.send_text(json.dumps(m))
                    ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
                    first = ws.receive_json()
                done = client.post(f"/api/workouts/{sid}/finish", headers=h).json()
                saved = admin.table("repetitions").select("id", count="exact").eq("session_id", sid).limit(0).execute().count
                coaching = admin.table("coaching_events").select("id", count="exact").eq("session_id", sid).limit(0).execute().count
                ok = (ready["analysis_supported"] is False and first["type"] == "session_state"
                      and done["total_reps"] == 0 and saved == 0 and coaching == 0 and done["status"] == "completed")
                check(f"{slug}: logged and finished, 0 reps counted, 0 coaching events", ok)

            hist = client.get("/api/workouts?limit=20", headers=h).json()
            check("history shows all 7 workouts with their exercise", sorted(w["exercise"] for w in hist) == sorted(SPELLINGS))
    finally:
        admin.auth.admin.delete_user(user.id)
        time.sleep(0.3)
        print("deleted throwaway user (cascade removed its workouts)")

    failed = [n for n, ok in results if not ok]
    print("\nE2E FAILED: " + "; ".join(failed) if failed else "\nMulti-exercise E2E passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
