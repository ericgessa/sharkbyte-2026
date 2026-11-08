"""Act like the mobile app against a RUNNING server and report what works.

    python -m scripts.check_remote --base-url http://192.168.0.164:8000     (run from server/)

Unlike e2e_live (which runs the app in-process), this goes over real HTTP and WebSocket, so it
proves the server is reachable at that URL and behaves for a real client. It signs in as a
throwaway Supabase user exactly like the app would (email + password -> access token), streams
5 SIMULATED squats, finishes, reads the summary and history, then deletes the user.

Run it from another machine/network to test a tunnel URL too (needs server/.env there).
It does NOT prove a phone on the event Wi-Fi can reach the laptop; only a request from that
phone can (open <base-url>/health in its browser).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from uuid import uuid4

import httpx
from supabase import create_client
from websockets.sync.client import connect

from app.services.database import DatabaseConfigError, get_client
from app.simulation import squat_messages
from scripts._cleanup import purge_tiger_readings

REPS = 5
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base-url", required=True, help="e.g. http://192.168.0.164:8000 or an https tunnel URL")
    base = p.parse_args().base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    try:
        admin = get_client()
    except DatabaseConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    # 1) Is the server reachable at all? (what a phone's browser would check)
    try:
        r = httpx.get(f"{base}/health", timeout=8)
        check("GET /health reachable", r.status_code == 200 and r.json() == {"status": "ok"}, f"HTTP {r.status_code}")
    except httpx.HTTPError as exc:
        check("GET /health reachable", False, type(exc).__name__)
        print("\nCannot reach the server at that URL: check it is running, the IP/port, and the firewall.")
        return 1

    email, password = f"mobile-check-{uuid4().hex[:10]}@example.com", secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True}).user
    created: list[str] = []  # workouts whose Tiger Data readings must be cleaned up
    print(f"created throwaway user {user.id}")
    try:
        # 2) Sign in the way the app does (public client key; falls back to the secret key if unset).
        key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        if not key or key.startswith("your-"):
            key = os.environ["SUPABASE_SECRET_KEY"].strip()
        app_client = create_client(os.environ["SUPABASE_URL"].strip(), key)
        token = app_client.auth.sign_in_with_password({"email": email, "password": password}).session.access_token
        check("sign in with Supabase (email + password)", bool(token))
        auth = {"Authorization": f"Bearer {token}"}

        # 3) REST as the app would call it
        check("REST rejects a missing token (401)", httpx.post(f"{base}/api/workouts", json={}, timeout=8).status_code == 401)
        r = httpx.post(f"{base}/api/workouts", json={"exercise": "squat"}, headers=auth, timeout=15)
        check("POST /api/workouts (201)", r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}")
        sid = r.json()["session_id"]
        created.append(sid)

        # 4) WebSocket as the app would use it
        with connect(f"{ws_base}/ws/workouts/{sid}", open_timeout=10) as ws:
            ws.send(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
            check("WebSocket connects and authenticates", json.loads(ws.recv(timeout=15))["type"] == "session_ready")
            # An ESP32 reading relayed over the same socket. Accepted silently (an error event would break the counts below).
            ws.send(json.dumps({"schema_version": 1, "type": "sensor_update", "session_id": sid, "device_id": "esp32-check",
                                "sequence": 0, "timestamp_ms": 1000, "data": {"spine_lumbar_deg": 5.0, "imu_pitch_deg": 10.0}}))
            for msg in squat_messages(sid, reps=REPS, depths=[85, 85, 85, 98, 98]):
                ws.send(json.dumps(msg))
            events = [json.loads(ws.recv(timeout=15)) for _ in range(REPS + 1)]
        check(f"{REPS} rep_completed events received", [e["rep_count"] for e in events if e["type"] == "rep_completed"] == list(range(1, REPS + 1)))
        check("coaching_feedback received", any(e["type"] == "coaching_feedback" for e in events))

        # 5) Finish, summary, history
        r = httpx.post(f"{base}/api/workouts/{sid}/finish", headers=auth, timeout=20)
        check("POST finish (200, completed, 5 reps)", r.status_code == 200 and r.json().get("total_reps") == REPS, f"HTTP {r.status_code}")
        r = httpx.get(f"{base}/api/workouts/{sid}/summary", headers=auth, timeout=15)
        ok = r.status_code == 200 and len(r.json()["reps"]) == REPS and len(r.json()["coaching_events"]) == 1
        check("GET summary (5 reps, 1 coaching event)", ok, f"HTTP {r.status_code}")
        r = httpx.get(f"{base}/api/workouts", headers=auth, timeout=15)
        check("GET history lists the workout", r.status_code == 200 and [w["session_id"] for w in r.json()] == [sid])

        # 6) The safety button: pain -> stop message, coaching turned off
        sid2 = httpx.post(f"{base}/api/workouts", json={"exercise": "squat"}, headers=auth, timeout=15).json()["session_id"]
        created.append(sid2)
        with connect(f"{ws_base}/ws/workouts/{sid2}", open_timeout=10) as ws:
            ws.send(json.dumps({"schema_version": 1, "type": "auth", "access_token": token}))
            ws.recv(timeout=15)
            ws.send(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid2, "signal": "pain"}))
            fb, state = json.loads(ws.recv(timeout=15)), json.loads(ws.recv(timeout=15))
        check(
            "pain signal: stop message + coaching disabled",
            fb.get("issue_code") == "pain_reported" and state.get("coaching_enabled") is False,
        )
        httpx.post(f"{base}/api/workouts/{sid2}/finish", headers=auth, timeout=20)
    finally:
        purge_tiger_readings(created, settle_s=2.0)  # the server flushes readings ~1 s after the last frame
        admin.auth.admin.delete_user(user.id)
        time.sleep(0.3)
        print("deleted throwaway user")

    failed = [n for n, ok in results if not ok]
    print("\nREMOTE CHECK FAILED: " + "; ".join(failed) if failed else f"\nRemote check passed against {base}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
