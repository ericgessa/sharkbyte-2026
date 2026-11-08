"""The coach report saved for every finished workout (Supabase ``workout_reports``).

Uses the fake store and a scripted fake Gemini; nothing touches Supabase or a real model. The live check against a real
project is ``scripts/e2e_reports.py`` once the migration is applied.
"""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import reports as reports_service
from app.services.agent import CoachAgent
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryStore
from tests.test_agent import FakeProvider, finished_workout, make_client, new_session, wait_for

GOOD = "You did 5 reps and your depth drifted a little toward the end. Nice work."


def saved(store: InMemoryStore, sid: str):
    return store.reports.get(sid)


# ------------------------------------------------------------------ saved when the workout finishes


def test_a_report_is_written_and_saved_the_moment_a_workout_finishes():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None), "the report was never saved"
    r = saved(store, sid)
    assert r["summary"] == GOOD and r["source"] == "agent"
    assert len(prov.summarize_calls) == 1


def test_the_history_tab_gets_the_saved_report_without_a_second_gemini_call():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
        a = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
        b = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert a["summary"] == b["summary"] == GOOD and a["source"] == "agent" and a["saved"] is True and a["generated_at"]
    assert len(prov.summarize_calls) == 1  # finishing wrote it once; opening the tab twice cost nothing


def test_a_request_that_arrives_while_the_report_is_being_written_waits_for_it():
    gate = threading.Event()
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD, gate=gate)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        result: list = []
        t = threading.Thread(target=lambda: result.append(c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()))
        t.start()
        time.sleep(0.2)  # the request is now waiting on the report already in progress
        gate.set()
        t.join(5)
    assert result and result[0]["summary"] == GOOD
    assert len(prov.summarize_calls) == 1  # one report, however many people asked while it was being written


def test_the_saved_report_survives_a_server_restart():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    fresh = FakeProvider(summarize="A different text that must never be shown.")
    with make_client(store, CoachAgent(fresh)) as c2:  # a new server, the same database
        r = c2.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert r["summary"] == GOOD and r["source"] == "agent"
    assert fresh.summarize_calls == []


def test_every_finished_workout_gets_a_report_even_without_the_ai_coach():
    store = InMemoryStore()
    with make_client(store, CoachAgent(None)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    r = saved(store, sid)
    assert r["source"] == "rules" and "5 reps" in r["summary"] and r["model"] is None


def test_the_model_that_wrote_a_report_is_recorded():
    prov = FakeProvider(summarize=GOOD)
    prov.model = "gemini-test-1"
    store = InMemoryStore()
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    assert saved(store, sid)["model"] == "gemini-test-1"


def test_finishing_a_workout_twice_does_not_write_a_second_report():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
        assert c.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 200
        time.sleep(0.2)
    assert len(prov.summarize_calls) == 1 and store.report_saves == 1


def test_finishing_does_not_wait_for_gemini():
    gate = threading.Event()  # Gemini "takes forever"
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD, gate=gate)
    with make_client(store, CoachAgent(prov)) as c:
        started = time.time()
        sid = finished_workout(c)  # includes POST /finish
        assert time.time() - started < 3.0
        assert saved(store, sid) is None  # still being written
        gate.set()
        assert wait_for(lambda: saved(store, sid) is not None)


# ------------------------------------------------------------------ what is NOT saved, and failures


def test_a_workout_still_in_progress_gets_no_saved_report():
    store, prov = InMemoryStore(), FakeProvider(summarize="So far you did 0 reps.")
    with make_client(store, CoachAgent(prov)) as c:
        sid = new_session(c)
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert r["saved"] is False and store.reports == {}


def test_a_failing_gemini_saves_the_rules_report_and_it_is_upgraded_later():
    store = InMemoryStore()
    broken = FakeProvider(summarize=RuntimeError("quota"))
    with make_client(store, CoachAgent(broken)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    assert saved(store, sid)["source"] == "rules"

    # Gemini is back, but a rules report is only upgraded after a cool-down (no storm of calls with a broken key)
    good = FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(good)) as c2:
        first = c2.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
        assert first["source"] == "rules" and good.summarize_calls == []
        store.reports[sid]["updated_at"] = "2020-01-01T00:00:00+00:00"  # long ago
        upgraded = c2.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
        again = c2.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert upgraded["source"] == "agent" and upgraded["summary"] == GOOD and again["summary"] == GOOD
    assert saved(store, sid)["source"] == "agent" and len(good.summarize_calls) == 1


def test_a_report_the_model_wrote_is_never_replaced_by_the_rules_text():
    store = InMemoryStore()
    with make_client(store, CoachAgent(FakeProvider(summarize=GOOD))) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    store.reports[sid]["updated_at"] = "2020-01-01T00:00:00+00:00"
    with make_client(store, CoachAgent(None)) as c2:
        assert c2.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()["summary"] == GOOD


def test_if_the_table_does_not_exist_the_report_still_works_and_is_kept_in_memory():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    store.reports_not_set_up = True  # the migration has not been applied
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: len(prov.summarize_calls) == 1)
        time.sleep(0.2)
        a = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
        b = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
    assert a.status_code == b.status_code == 200
    assert a.json()["summary"] == GOOD and a.json()["saved"] is False
    assert len(prov.summarize_calls) == 1  # the in-memory copy stood in, so Gemini was not called again
    assert store.reports == {}


def test_a_database_error_while_saving_never_breaks_finishing_or_the_report():
    store = InMemoryStore()

    def explode(*a, **k):
        raise RuntimeError("database down")

    store.save_report = explode
    with make_client(store, CoachAgent(FakeProvider(summarize=GOOD))) as c:
        sid = finished_workout(c)  # finish still returned 200
        time.sleep(0.3)
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
    assert r.status_code == 200 and r.json()["summary"] == GOOD


def test_a_pain_report_gets_the_rules_text_saved_and_gemini_is_never_called():
    store, prov = InMemoryStore(), FakeProvider(summarize="Great job, push through!")
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    # Pain is detected from the saved coaching events, so a pain workout saves the rules text.
    store2, prov2 = InMemoryStore(), FakeProvider(summarize="Great job, push through!")
    with make_client(store2, CoachAgent(prov2)) as c2:
        import json

        sid2 = new_session(c2)
        with c2.websocket_connect(f"/ws/workouts/{sid2}") as ws:
            ws.send_text(json.dumps({"schema_version": 1, "type": "auth", "access_token": "alice-token"}))
            ws.receive_json()
            ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid2, "signal": "pain"}))
            ws.receive_json(), ws.receive_json()
            assert wait_for(lambda: len(store2.events.get(sid2, {})) == 1)
        assert c2.post(f"/api/workouts/{sid2}/finish", headers=ALICE).status_code == 200
        assert wait_for(lambda: saved(store2, sid2) is not None)
    r = saved(store2, sid2)
    assert r["source"] == "rules" and "health professional" in r["summary"] and prov2.summarize_calls == []


def test_an_exercise_without_analysis_saves_the_logged_only_text():
    store = InMemoryStore()
    with make_client(store, CoachAgent(None)) as c:
        sid = new_session(c, exercise="Pull Up")
        assert c.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 200
        assert wait_for(lambda: saved(store, sid) is not None)
    assert "pull-up workout was logged" in saved(store, sid)["summary"]


# ------------------------------------------------------------------ privacy


def test_only_the_owner_can_read_a_saved_report():
    store = InMemoryStore()
    with make_client(store, CoachAgent(FakeProvider(summarize=GOOD))) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
        assert c.get(f"/api/workouts/{sid}/coach-summary", headers=BOB).status_code == 404
        assert c.get(f"/api/workouts/{sid}/coach-summary").status_code == 401


def test_deleting_a_workout_deletes_its_report():
    store = InMemoryStore()
    with make_client(store, CoachAgent(FakeProvider(summarize=GOOD))) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
        assert c.delete(f"/api/workouts/{sid}", headers=ALICE).status_code == 204
    assert sid not in store.reports


def test_the_report_text_is_only_what_the_coach_wrote_not_other_users_data():
    store, prov = InMemoryStore(), FakeProvider(summarize=GOOD)
    with make_client(store, CoachAgent(prov)) as c:
        sid = finished_workout(c)
        assert wait_for(lambda: saved(store, sid) is not None)
    context = prov.summarize_calls[0]
    assert "user-alice" not in str(context) and "alice-token" not in str(context)


# ------------------------------------------------------------------ the service in isolation


def test_the_upgrade_rule_needs_gemini_a_rules_report_and_a_cool_down():
    class App:
        class state:
            agent = CoachAgent(FakeProvider(summarize=GOOD))

    row = {"id": "x", "status": "completed", "exercises": {"name": "squat"}}
    old = {"source": "rules", "updated_at": "2020-01-01T00:00:00+00:00"}
    assert reports_service._should_upgrade(App, row, old) is True
    assert reports_service._should_upgrade(App, row, {**old, "source": "agent"}) is False
    assert reports_service._should_upgrade(App, row, {"source": "rules", "updated_at": "2999-01-01T00:00:00+00:00"}) is False
    assert reports_service._should_upgrade(App, {**row, "exercises": {"name": "pull-up"}}, old) is False  # nothing to report on
    App.state.agent = CoachAgent(None)
    assert reports_service._should_upgrade(App, row, old) is False
