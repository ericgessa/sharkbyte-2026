"""Offline tests for app.services.database (no Supabase project needed).

An opt-in live test at the bottom runs only when RUN_SUPABASE_TESTS=1 and real
credentials are present; it writes to that project (throwaway user, cleaned up).
"""

import base64
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import database as db


class FakeQuery:
    """Records the calls made on it; returns canned rows from execute()."""

    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.log.append((name, args, kwargs))
            return self

        return call

    def execute(self):
        return SimpleNamespace(data=self.rows, count=len(self.rows))


class FakeClient:
    def __init__(self, tables):
        self.tables, self.log = tables, []

    def table(self, name):
        self.log.append(("table", (name,), {}))
        return FakeQuery(self.tables.get(name, []), self.log)


def calls(client, name):
    return [(a, k) for n, a, k in client.log if n == name]


def test_get_workout_session_filters_on_both_session_and_user():
    client = FakeClient({"workout_sessions": [{"id": "s1"}]})
    sid, uid = uuid4(), uuid4()
    assert db.get_workout_session(sid, uid, client) == {"id": "s1"}
    assert (("id", str(sid)), {}) in calls(client, "eq")
    assert (("user_id", str(uid)), {}) in calls(client, "eq")


def test_get_workout_session_returns_none_when_not_owned():
    assert db.get_workout_session(uuid4(), uuid4(), FakeClient({})) is None


def test_create_workout_session_uses_exercise_id_and_user():
    uid = uuid4()
    client = FakeClient({"exercises": [{"id": 7}], "workout_sessions": [{"id": "new"}]})
    assert db.create_workout_session(uid, "squat", client) == {"id": "new"}
    (args, _), = calls(client, "insert")
    assert args[0] == {"user_id": str(uid), "exercise_id": 7}


def test_unknown_exercise_raises_clear_error():
    with pytest.raises(db.DatabaseError, match="Unknown exercise"):
        db.get_exercise_id("deadlift", FakeClient({"exercises": []}))


def test_list_sessions_is_newest_first_and_scoped_to_user():
    uid = uuid4()
    client = FakeClient({"workout_sessions": []})
    db.list_workout_sessions(uid, limit=5, client=client)
    assert (("started_at",), {"desc": True}) in calls(client, "order")
    assert (("user_id", str(uid)), {}) in calls(client, "eq")


# ---- configuration ---------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    db.get_client.cache_clear()
    yield monkeypatch
    db.get_client.cache_clear()


def jwt_with_role(role):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'HS256'})}.{b64({'role': role})}.sig"


def test_missing_config_names_the_variables_but_no_secrets(clean_env):
    with pytest.raises(db.DatabaseConfigError, match="SUPABASE_URL, SUPABASE_SECRET_KEY"):
        db.get_client()


def test_placeholder_values_are_rejected(clean_env):
    clean_env.setenv("SUPABASE_URL", "https://your-project.supabase.co")
    clean_env.setenv("SUPABASE_SECRET_KEY", "your-secret-key")
    with pytest.raises(db.DatabaseConfigError, match="placeholder"):
        db.get_client()


@pytest.mark.parametrize("key", ["sb_publishable_abc123", jwt_with_role("anon")])
def test_client_safe_keys_are_rejected_without_echoing_them(clean_env, key):
    clean_env.setenv("SUPABASE_URL", "https://abc.supabase.co")
    clean_env.setenv("SUPABASE_SECRET_KEY", key)
    with pytest.raises(db.DatabaseConfigError) as exc:
        db.get_client()
    assert key not in str(exc.value)


def test_service_role_jwt_is_accepted_by_the_guard():
    db._check_secret_key(jwt_with_role("service_role"))  # must not raise


# ---- opt-in live test ------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_SUPABASE_TESTS") != "1", reason="set RUN_SUPABASE_TESTS=1 to run against a real project"
)
def test_live_create_and_fetch_session_with_ownership():
    import secrets

    client = db.get_client()
    user = client.auth.admin.create_user(
        {"email": f"pytest-{uuid4().hex[:12]}@example.com", "password": secrets.token_urlsafe(24), "email_confirm": True}
    ).user
    try:
        assert client.table("profiles").select("id").eq("id", user.id).execute().data  # trigger ran
        session = db.create_workout_session(user.id, "squat")
        assert db.get_workout_session(session["id"], user.id)["status"] == "active"
        assert db.get_workout_session(session["id"], uuid4()) is None
    finally:
        client.auth.admin.delete_user(user.id)
