"""LIVE check that the spine wearable's data reaches Tiger Data AND drives the back-shape feedback.

    python -m scripts.e2e_spine        (run from server/)

Needs Supabase (SUPABASE_*) and Tiger Data (TIGERDATA_URL, migrations applied) in server/.env. Runs the real app
in-process with a throwaway user: one squat workout over the real WebSocket with SIMULATED camera frames plus SIMULATED
wearable messages (`sensor_update` with `spine_upper_deg` / `spine_mid_deg` / `spine_lumbar_deg` and the BNO085 tilt)
where the lower back rounds at the bottom of every rep. Then it checks:
  * the live `coaching_feedback` is the lower-back cue (the camera counted the reps, the wearable judged the back shape);
  * Supabase kept the reps with their `spine` summary (not the raw stream);
  * Tiger Data holds the high-frequency spine readings under the same session_id.
Pose and sensor data are generated here; they are not real camera or hardware data. Everything it creates is deleted.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
from supabase import create_client

from app.main import create_app
from app.services.database import DatabaseConfigError, get_client
from app.services.timeseries import TigerDataConfigError, TigerStore, tigerdata_url
from app.simulation import squat_messages
from scripts._cleanup import arm_watchdog, purge_tiger_readings

REPS = 5
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
    try:
        if not tiger.ping()["sensor_readings_is_hypertable"]:
            print("sensor_readings is not a hypertable: run python -m scripts.apply_tigerdata_migrations")
            return 1
    except Exception as exc:
        print(f"Cannot reach Tiger Data: {type(exc).__name__}. Check TIGERDATA_URL.")
        return 1

    email, password = f"spine-e2e-{uuid4().hex[:10]}@example.com", secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user
    sid = None
    watchdog = arm_watchdog(240, lambda: (purge_tiger_readings([sid] if sid else []), admin.auth.admin.delete_user(user.id)))
    try:
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        token = create_client(os.environ["SUPABASE_URL"].strip(), key).auth.sign_in_with_password({"email": email, "password": password}).session.access_token
        headers = {"Authorization": f"Bearer {token}"}

        frames = list(squat_messages(uuid4(), reps=REPS))
        with TestClient(create_app()) as client:
            sid = client.post("/api/workouts", json={"exercise": "squat"}, headers=headers).json()["session_id"]
            print(f"workout {sid} (SIMULATED camera + wearable data)")
            events: list[dict] = []
            with client.websocket_connect(f"/ws/workouts/{sid}") as ws:
                ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
                ws.receive_json()
                n = 0
                for i, f in enumerate(frames):
                    f["session_id"] = sid
                    ws.send_text(json.dumps(f))
                    if i % 2 == 0:  # the wearable streams ~20 Hz on its OWN clock
                        depth = max(0.0, min(1.0, (170 - f["data"]["left_knee_angle_deg"]) / 85))  # 0 standing .. 1 bottom
                        ws.send_text(json.dumps({
                            "schema_version": 1, "type": "sensor_update", "session_id": sid, "device_id": "esp32-spine", "sequence": n,
                            "timestamp_ms": f["timestamp_ms"] - 1_700_000_000_000,
                            "data": {
                                "spine_upper_deg": round(2 * depth, 2), "spine_mid_deg": round(1.5 * depth, 2),
                                "spine_lumbar_deg": round(24 * depth, 2),  # the lower back rounds as the squat deepens
                                "imu_pitch_deg": round(30 * depth, 2), "imu_roll_deg": 0.5,
                            },
                        }))
                        n += 1
                ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pause"}))
                while True:  # the pause answer marks the end of everything queued before it
                    e = ws.receive_json()
                    events.append(e)
                    if e["type"] == "session_state":
                        break
            reps = [e for e in events if e["type"] == "rep_completed"]
            cues = [e for e in events if e["type"] == "coaching_feedback"]
            check(f"{REPS} reps counted by the camera", len(reps) == REPS)
            check("a lower-back cue came from the WEARABLE data", any(c["issue_code"] == "lower_back_rounding" for c in cues), str([c["issue_code"] for c in cues]))
            check("the cue says what to do", any("lower back" in c["message"].lower() for c in cues))
            check("finish", client.post(f"/api/workouts/{sid}/finish", headers=headers).status_code == 200)

            stored = client.get(f"/api/workouts/{sid}/summary", headers=headers).json()["reps"]
            with_spine = [r for r in stored if "spine" in (r["metrics"] or {})]
            check("Supabase: reps saved with their spine summary", len(with_spine) == REPS, f"{len(with_spine)} of {len(stored)}")

            rows = tiger.query("select metric_name, count(*) as n from sensor_readings where session_id = %s group by 1", (sid,))
            counts = {r["metric_name"]: r["n"] for r in rows}
            check("Tiger Data: spine_upper / spine_mid / spine_lumbar stored", {"spine_upper", "spine_mid", "spine_lumbar"} <= set(counts), str(counts))
            check("Tiger Data: BNO085 tilt stored too", "imu_pitch" in counts)
            check("Tiger Data: the camera's angles are in the same table", "left_knee_angle" in counts)
            check("Tiger Data: many readings, not one per rep", counts.get("spine_lumbar", 0) > 20, str(counts.get("spine_lumbar")))
    finally:
        watchdog.cancel()
        purge_tiger_readings([sid] if sid else [], settle_s=1.0)
        admin.auth.admin.delete_user(user.id)
        print("deleted throwaway user and its Tiger Data readings")

    failed = [n for n, ok in results if not ok]
    print("\nSpine E2E passed" if not failed else f"\nSpine E2E FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
