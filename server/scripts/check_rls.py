"""LIVE, repeatable test of the database security rules (Row Level Security + table grants).

    python -m scripts.check_rls        (run from server/)

Acts like a malicious or buggy CLIENT: it talks to Supabase's REST API directly with the PUBLISHABLE key and
signed-in users' tokens (never the secret key) and tries to read other people's data and to write where only the
backend may. Run it after ANY change to supabase/migrations, so policies can't be loosened by accident.

Needs SUPABASE_URL, SUPABASE_SECRET_KEY (to create/remove the two throwaway users and their data) and a real
SUPABASE_PUBLISHABLE_KEY in server/.env. Cleans up after itself.
"""

from __future__ import annotations

import os
import secrets
import sys
from uuid import uuid4

import httpx
from supabase import create_client

from app.services.database import DatabaseConfigError, get_client, ensure_exercise
from scripts._cleanup import arm_watchdog

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
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")
    pub = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if not pub or pub.startswith("your-"):
        print("CONFIG ERROR: SUPABASE_PUBLISHABLE_KEY is not set; this test must use the client-safe key, not the secret key.")
        return 2

    users: list = []
    watchdog = arm_watchdog(180, lambda: [admin.auth.admin.delete_user(u.id) for u in users])
    try:
        creds = []
        for tag in ("a", "b"):
            email, pw = f"rls-{tag}-{uuid4().hex[:8]}@example.com", secrets.token_urlsafe(18)
            users.append(admin.auth.admin.create_user({"email": email, "password": pw, "email_confirm": True}).user)
            creds.append((email, pw))
        ua, ub = users
        tokens = [create_client(url, pub).auth.sign_in_with_password({"email": e, "password": p}).session.access_token for e, p in creds]

        # Setup (as the backend would): one workout + rep + coaching event for each user.
        ex_id = ensure_exercise("squat", {}, admin)
        sess = {}
        for u in users:
            s = admin.table("workout_sessions").insert({"user_id": u.id, "exercise_id": ex_id}).execute().data[0]
            admin.table("repetitions").insert({"session_id": s["id"], "rep_number": 1, "metrics": {}}).execute()
            admin.table("coaching_events").insert({"session_id": s["id"], "message": "x"}).execute()
            sess[u.id] = s["id"]

        def api(method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
            headers = {"apikey": pub, "Content-Type": "application/json", "Prefer": "return=representation"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return httpx.request(method, f"{url}/rest/v1/{path}", headers=headers, timeout=20, **kw)

        def denied(r: httpx.Response) -> bool:
            """Refused outright (permission error), or silently affected no rows."""
            return r.status_code in (401, 403) or (r.status_code in (200, 204) and r.json() == [] if r.content else r.status_code == 204)

        ta, tb = tokens
        # ---- reading: only your own data -------------------------------------------------
        for table, col in (("workout_sessions", "id"), ("repetitions", "session_id"), ("coaching_events", "session_id")):
            rows = api("GET", f"{table}?select={col}", ta).json()
            ids = {r[col] for r in rows}
            check(f"A reads {table}: sees exactly their own row, none of B's",
                  ids == {sess[ua.id]} and sess[ub.id] not in ids, str(rows)[:120])
        prof = api("GET", "profiles?select=id", ta).json()
        check("A reads profiles: only their own", [p["id"] for p in prof] == [ua.id], str(prof)[:120])
        ex = api("GET", "exercises?select=name", ta)
        check("A (signed in) can read the exercise list", ex.status_code == 200 and len(ex.json()) >= 1)
        direct = api("GET", f"workout_sessions?id=eq.{sess[ub.id]}&select=id", ta)
        check("A asking for B's workout BY ID gets nothing", direct.status_code == 200 and direct.json() == [])

        # ---- writing: clients may not forge workouts, reps, or coaching ------------------
        r = api("POST", "workout_sessions", ta, json={"user_id": ua.id, "exercise_id": ex_id})
        check("A cannot insert a workout session (only the backend can)", r.status_code in (401, 403), f"HTTP {r.status_code} {r.text[:100]}")
        r = api("POST", "repetitions", ta, json={"session_id": sess[ua.id], "rep_number": 99, "metrics": {}})
        check("A cannot forge a repetition on their OWN workout", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = api("POST", "coaching_events", ta, json={"session_id": sess[ua.id], "message": "forged"})
        check("A cannot forge a coaching event", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = api("PATCH", f"workout_sessions?id=eq.{sess[ua.id]}", ta, json={"total_reps": 9999})
        check("A cannot edit their own workout's rep total", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = api("DELETE", f"workout_sessions?id=eq.{sess[ua.id]}", ta)
        check("A cannot delete a workout directly (the API's DELETE does it properly)", r.status_code in (401, 403), f"HTTP {r.status_code}")
        still = admin.table("workout_sessions").select("id,total_reps").eq("id", sess[ua.id]).execute().data
        check("...and A's workout is intact with total_reps unchanged", len(still) == 1 and still[0]["total_reps"] == 0)

        # ---- profiles: edit your own display name only -----------------------------------
        r = api("PATCH", f"profiles?id=eq.{ua.id}", ta, json={"display_name": "Alice"})
        check("A can rename themselves", r.status_code in (200, 204) and (r.status_code == 204 or r.json()[0]["display_name"] == "Alice"), f"HTTP {r.status_code}")
        r = api("PATCH", f"profiles?id=eq.{ub.id}", ta, json={"display_name": "HACKED"})
        b_name = admin.table("profiles").select("display_name").eq("id", ub.id).execute().data[0]["display_name"]
        check("A cannot rename B", b_name != "HACKED" and denied(r), f"HTTP {r.status_code}, B is now {b_name!r}")
        r = api("PATCH", f"profiles?id=eq.{ua.id}", ta, json={"created_at": "2000-01-01T00:00:00Z"})
        check("A cannot edit other profile columns (only display_name)", r.status_code in (401, 403), f"HTTP {r.status_code}")

        # ---- not signed in ---------------------------------------------------------------
        for table in ("workout_sessions", "repetitions", "coaching_events", "profiles", "exercises"):
            r = api("GET", f"{table}?select=*")
            check(f"anonymous (no login) cannot read {table}", r.status_code in (401, 403) or r.json() == [], f"HTTP {r.status_code} {r.text[:80]}")
        # B's token sees B's data, proving the isolation is per user and not a blanket block
        rows_b = {r["id"] for r in api("GET", "workout_sessions?select=id", tb).json()}
        check("B (a different user) sees only B's workout", rows_b == {sess[ub.id]})
    finally:
        watchdog.cancel()
        for u in users:
            admin.auth.admin.delete_user(u.id)  # cascades to their profile, workouts, reps, coaching events
        print("cleaned up: both throwaway users and their data")

    failed = [n for n, ok in results if not ok]
    print("\nRLS CHECK FAILED: " + "; ".join(failed) if failed else f"\nRLS check passed ({len(results)} checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
