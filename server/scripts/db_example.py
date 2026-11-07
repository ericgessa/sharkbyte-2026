"""End-to-end example: create and retrieve a workout session.

    python -m scripts.db_example      (run from server/)

Writes to the project in server/.env. It creates a THROWAWAY auth user (which also
exercises the profile-creation trigger), creates and reads back a session for it,
then deletes the user; deleting cascades to the profile and session. Do not point
this at a project with data you cannot afford to touch.
"""

from __future__ import annotations

import secrets
import sys
from uuid import uuid4

from app.services.database import (
    DatabaseConfigError,
    create_workout_session,
    get_client,
    get_workout_session,
    list_workout_sessions,
)


def main() -> int:
    try:
        client = get_client()
    except DatabaseConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    email = f"db-example-{uuid4().hex[:12]}@example.com"
    user = client.auth.admin.create_user(
        {"email": email, "password": secrets.token_urlsafe(24), "email_confirm": True}
    ).user
    print(f"created throwaway auth user {user.id}")
    try:
        profile = client.table("profiles").select("*").eq("id", user.id).execute().data
        print(f"profile created by trigger: {profile}")

        session = create_workout_session(user.id, "squat")
        print(f"created session: {session['id']} status={session['status']}")

        fetched = get_workout_session(session["id"], user.id)
        print(f"fetched by owner: {fetched is not None}")

        stranger = get_workout_session(session["id"], uuid4())
        print(f"fetched by a different user id: {stranger is not None} (must be False)")

        print(f"user has {len(list_workout_sessions(user.id))} session(s)")
        ok = fetched is not None and stranger is None and len(profile) == 1
    finally:
        client.auth.admin.delete_user(user.id)
        print("deleted throwaway user (cascade removed profile and session)")

    print("\nExample passed" if ok else "\nExample FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
