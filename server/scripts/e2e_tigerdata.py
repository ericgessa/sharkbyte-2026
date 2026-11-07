"""LIVE end-to-end check of the two-database design, and a demo for judges.

    python -m scripts.e2e_tigerdata [--keep]        (run from server/)

Needs BOTH Supabase (SUPABASE_*) and Tiger Data (TIGERDATA_URL, migrations applied) in server/.env.
Runs the real app in-process: throwaway user -> workout -> WebSocket with SIMULATED camera frames plus
SIMULATED ESP32 flex/IMU updates (synthetic data, clearly not real hardware) -> finish, then reads the
results back from BOTH databases and checks they agree on the same session_id.

By default it deletes everything it created. With --keep, the user, Supabase rows and Tiger Data
readings stay, so you can show the data live (it prints the session id and example SQL).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from supabase import create_client

from app.main import create_app
from app.services import analytics
from app.services.database import DatabaseConfigError, get_client
from app.services.timeseries import TigerDataConfigError, TigerStore, tigerdata_url
from app.simulation import squat_messages

REPS = 5
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--keep", action="store_true", help="leave the user, session and readings in place (for demos)")
    keep = p.parse_args().keep

    try:
        admin = get_client()
        url = tigerdata_url()
    except (DatabaseConfigError, TigerDataConfigError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    tiger = TigerStore()
    try:
        info = tiger.ping()
    except Exception as exc:
        print(f"Cannot reach Tiger Data: {type(exc).__name__}. Check TIGERDATA_URL.")
        return 1
    if not info["sensor_readings_is_hypertable"]:
        print("sensor_readings is not a hypertable: run python -m scripts.apply_tigerdata_migrations")
        return 1

    email, password = f"tiger-e2e-{uuid4().hex[:10]}@example.com", secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user
    sid = None
    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        token = create_client(os.environ["SUPABASE_URL"].strip(), key).auth.sign_in_with_password(
            {"email": email, "password": password}
        ).session.access_token
        headers = {"Authorization": f"Bearer {token}"}

        frames = list(squat_messages(uuid4(), reps=REPS, depths=[85, 85, 85, 98, 98]))
        with TestClient(create_app()) as client:
            sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=headers).json()["session_id"]
            print(f"workout {sid} (SIMULATED camera + ESP32 data)")
            with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
                ws.receive_json()
                sensor_msgs = 0
                for i, f in enumerate(frames):
                    f["session_id"] = sid
                    ws.send_text(json.dumps(f))
                    if i % 3 == 0:  # the ESP32 sends ~10 Hz on its own clock, relayed over the same socket
                        a = f["data"]["left_knee_angle_deg"]
                        ws.send_text(json.dumps({
                            "schema_version": 1, "type": "sensor_update", "session_id": sid, "device_id": "esp32-01",
                            "sequence": sensor_msgs, "timestamp_ms": f["timestamp_ms"] - 1_000_000,  # a different clock
                            "data": {"flex_raw": round(2000 - (170 - a) * 5, 1), "imu_pitch_deg": round((170 - a) * 0.3, 2)},
                        }))
                        sensor_msgs += 1
                events = [ws.receive_json() for _ in range(REPS + 1)]
            check("live feedback unaffected: 5 reps + coaching message", sorted(e["type"] for e in events).count("rep_completed") == REPS)
            r = client.post(f"/api/workouts/{sid}/finish", headers=headers)
            check("finish (flushes buffered sensor readings)", r.status_code == 200 and r.json()["total_reps"] == REPS)

            # --- Supabase: sessions, reps, coaching ---------------------------------------
            reps = admin.table("repetitions").select("rep_number,metrics").eq("session_id", sid).order("rep_number").execute().data
            check("Supabase: 5 completed reps stored", len(reps) == REPS)
            check("Supabase: reps carry server-time windows", all("started_at" in x["metrics"] for x in reps))
            check("Supabase: NOT the raw stream (reps only)", len(reps) < 50)

            # --- Tiger Data: raw time-series, same session_id -------------------------------
            deadline = time.time() + 10
            n = 0
            while time.time() < deadline:
                n = tiger.query("select count(*) as n from sensor_readings where session_id = %s", (sid,))[0]["n"]
                if n >= 2 * len(frames) * 0.9:
                    break
                time.sleep(0.5)
            check(f"Tiger Data: high-frequency readings stored under the same session_id ({n} rows)", n > 100)
            ov = analytics.sensor_overview(tiger, UUID(sid))
            check("Tiger Data: camera + flex + IMU metrics all present", {"left_knee_angle", "flex_value", "imu_pitch"} <= set(ov["metrics"]))

            # --- SQL analytics over Tiger Data, windows from Supabase ------------------------
            body = client.get(f"/api/workouts/{sid}/analytics", headers=headers).json()
            ts = body["timeseries"]
            check("analytics endpoint merges both databases", ts.get("available") is True and body["supabase"]["stats"]["rep_count"] == REPS)
            trends = ts.get("rep_trends", {})
            check("per-rep depth trend computed in SQL (getting shallower)", (trends.get("depth_trend_deg_per_rep") or 0) > 0)
            check("knee angle min is the deepest squat (~85)", abs((ts.get("knee_angle", {}).get("min_deg") or 0) - 85) < 2)

            print("\nSummary the AI coach would receive (compact, no raw stream):")
            print(json.dumps({"supabase_stats": body["supabase"]["stats"], "rep_trends": trends.get("reps"),
                              "depth_trend_deg_per_rep": trends.get("depth_trend_deg_per_rep"),
                              "range_trend_deg_per_rep": trends.get("range_trend_deg_per_rep")}, indent=2))
    finally:
        if keep and sid:
            print(f"\n--keep: data left in place. Session {sid}  (user {user.id}, email {email})")
            print(f"Inspect: python -m scripts.check_tigerdata --session {sid}")
        else:
            if sid:
                with psycopg.connect(url, connect_timeout=10) as conn:
                    conn.execute("delete from sensor_readings where session_id = %s", (sid,))
            admin.auth.admin.delete_user(user.id)  # cascades to the Supabase session, reps, coaching events
            print("\ncleaned up: deleted the throwaway user, its Supabase rows and its Tiger Data readings")
        tiger.close()

    failed = [n for n, ok in results if not ok]
    print("\nE2E FAILED: " + "; ".join(failed) if failed else "\nTwo-database E2E passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
