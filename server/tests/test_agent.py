"""The AI coach layer: guardrails, fallbacks and live behavior, with a scripted fake provider.

Nothing here calls a real LLM. The Gemini adapter itself is tested against a fake HTTP server in test_gemini.py."""

import asyncio
import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import agent as ag
from app.services.agent import AgentError, AgentOutput, CoachAgent, build_cue_context, rules_summary
from app.services.coaching import CoachingDecision, RuleCoach
from app.services.rep_counter import RepMetrics
from app.simulation import squat_messages
from tests.fakes import ALICE, BOB, FakeAuth, FakeTiger, InMemoryStore, auth_message


def rep(n, depth=85.0, asym=2.0):
    return RepMetrics(n, depth, 400, 400, 1700, asym)


REPS = [rep(1), rep(2), rep(3), rep(4, 98), rep(5, 98)]
CANDIDATE = CoachingDecision(5, "reduced_depth", "Your depth has decreased compared with earlier reps. Try your previous comfortable range.")
CTX = build_cue_context("squat", REPS, CANDIDATE, [(5, "reduced_depth")], None)


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    """A scripted coach LLM. ``gate`` (a threading.Event) blocks decide() until set, to test timing."""

    def __init__(self, decide=None, summarize=None, delay=0.0, gate=None):
        self._decide, self._summarize, self.delay, self.gate = decide, summarize, delay, gate
        self.decide_calls, self.summarize_calls, self.active, self.max_active, self.closed = [], [], 0, 0, False

    async def _wait(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        while self.gate is not None and not self.gate.is_set():
            await asyncio.sleep(0.01)

    async def decide(self, context):
        self.decide_calls.append(context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self._wait()
            r = self._decide(context) if callable(self._decide) else self._decide
            if isinstance(r, Exception):
                raise r
            return r or AgentOutput(action="send_feedback", message="Nice effort, keep your depth steady.")
        finally:
            self.active -= 1

    async def summarize(self, context):
        self.summarize_calls.append(context)
        await self._wait()
        r = self._summarize(context) if callable(self._summarize) else self._summarize
        if isinstance(r, Exception):
            raise r
        return r if r is not None else "You did great today."

    async def aclose(self):
        self.closed = True


# ---- refine: what the agent may do -------------------------------------------


def test_a_disabled_agent_changes_nothing_and_calls_nothing():
    agent = CoachAgent(None)
    assert agent.enabled is False
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE
    assert agent.stats["calls"] == 0


def test_a_valid_llm_answer_rewords_the_cue_but_keeps_the_rule_decisions_identity():
    prov = FakeProvider(decide=AgentOutput(action="send_feedback", message="Try to keep your depth like your first reps."))
    final = run(CoachAgent(prov).refine(CANDIDATE, CTX))
    assert final.message == "Try to keep your depth like your first reps."
    assert final.source == "agent" and final.action == "send_feedback"
    assert (final.rep_number, final.issue_code, final.event_id) == (5, "reduced_depth", CANDIDATE.event_id)  # same event


def test_the_llm_can_choose_to_stay_quiet():
    agent = CoachAgent(FakeProvider(decide=AgentOutput(action="no_action", message="")))
    assert run(agent.refine(CANDIDATE, CTX)) is None
    assert agent.stats["suppressed"] == 1


def test_rest_is_only_allowed_after_five_reps():
    rest = AgentOutput(action="recommend_rest", message="Take a short rest before the next set.")
    ok = run(CoachAgent(FakeProvider(decide=rest)).refine(CANDIDATE, CTX))
    assert ok.action == "recommend_rest" and ok.source == "agent"
    early = build_cue_context("squat", REPS[:4], CANDIDATE, [], None)
    agent = CoachAgent(FakeProvider(decide=rest))
    assert run(agent.refine(CANDIDATE, early)) is CANDIDATE  # fell back to the rule message
    assert agent.fallback_reasons == {"rest_too_early": 1}


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("x" * 201, "too_long"),
        ("That could cause an injury, careful.", "medical_language"),
        ("Stop if you feel any pain.", "medical_language"),
        ("A doctor would say deeper is safer.", "medical_language"),
        ("This is dangerous at that depth.", "medical_language"),
        ("See https://evil.example/x for tips.", "markup_or_link"),
        ("Do <b>this</b> now.", "markup_or_link"),
        ('{"action": "send_feedback"}', "markup_or_link"),
        ("You are 37 degrees shallower than before.", "invented_number"),
        ("Aim for 120 degrees next time.", "invented_number"),
    ],
)
def test_unsafe_or_made_up_wording_is_rejected_and_the_rule_message_is_used(bad, reason):
    agent = CoachAgent(FakeProvider(decide=AgentOutput(action="send_feedback", message=bad)))
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE
    assert agent.fallback_reasons == {reason: 1}


def test_numbers_that_really_are_in_the_data_are_allowed():
    msg = "Your last two reps reached 98 degrees, compared with 85 earlier."
    final = run(CoachAgent(FakeProvider(decide=AgentOutput(action="send_feedback", message=msg))).refine(CANDIDATE, CTX))
    assert final is not CANDIDATE and final.message == msg


@pytest.mark.parametrize("error", [RuntimeError("boom"), AgentError("refusal"), ConnectionError("down"), ValueError("bad")])
def test_any_provider_failure_falls_back_to_the_rule_message_without_raising(error):
    agent = CoachAgent(FakeProvider(decide=error))
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE
    assert agent.stats["fallbacks"] == 1 and agent.stats["used"] == 0


def test_a_slow_llm_times_out_and_falls_back():
    agent = CoachAgent(FakeProvider(delay=5), timeout_s=0.1)
    t = time.monotonic()
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE
    assert time.monotonic() - t < 2 and agent.fallback_reasons == {"timeout": 1}


def test_concurrent_llm_calls_are_capped():
    prov = FakeProvider(delay=0.05)
    agent = CoachAgent(prov, max_concurrent=2)

    async def go():
        await asyncio.gather(*[agent.refine(CANDIDATE, CTX) for _ in range(8)])

    run(go())
    assert prov.max_active <= 2 and len(prov.decide_calls) == 8


# ---- what the model is allowed to see ---------------------------------------


def test_the_context_is_small_server_computed_and_bounded():
    many = [rep(i, 85.0 + i) for i in range(1, 40)]
    ctx = build_cue_context("squat", many, CANDIDATE, [(i, "reduced_depth") for i in range(30)], None)
    assert len(ctx["recent_reps"]) == 6 and len(ctx["feedback_already_given"]) == 6 and ctx["reps_completed"] == 39
    assert ctx["baseline_deepest_knee_angle_deg"] == 87.0  # median of the first three reps (86, 87, 88)
    assert set(ctx) == {"exercise", "reps_completed", "detected_issue", "rule_message", "baseline_deepest_knee_angle_deg",
                        "recent_reps", "feedback_already_given"}
    assert len(json.dumps(ctx)) < 2500


def test_the_rule_coach_records_the_cues_it_gives():
    c = RuleCoach()
    for r in REPS:
        c.observe(r)
    assert c.history == [(5, "reduced_depth")]


# ---- summaries --------------------------------------------------------------

SUMMARY_CTX = {"supabase": {"stats": {"rep_count": 5, "depth_change_deg": 8.7, "max_asymmetry_deg": 2.0},
                            "coaching_events": [{"rep_number": 5, "issue_code": "reduced_depth", "action": "send_feedback"}]}}


def test_rules_summary_reads_the_data_honestly():
    t = rules_summary(SUMMARY_CTX)
    assert "5 reps" in t and "8.7 degrees shallower" in t and "1 coaching cue" in t and "pain" not in t.lower()
    deeper = {"supabase": {"stats": {"rep_count": 1, "depth_change_deg": -6.0}, "coaching_events": []}}
    assert "1 rep." in rules_summary(deeper) and "deeper" in rules_summary(deeper)
    assert "No completed reps" in rules_summary({"supabase": {"stats": {"rep_count": 0}, "coaching_events": []}})
    logged = rules_summary(SUMMARY_CTX, analysis_supported=False, exercise_name="pull-up")
    assert "pull-up workout was logged" in logged and "aren't available" in logged and "reps" not in logged.split("aren't")[0].replace("Rep counting", "")


def test_a_pain_report_always_gets_the_rules_recap_and_never_calls_the_llm():
    ctx = {"supabase": {"stats": {"rep_count": 3}, "coaching_events": [{"issue_code": "pain_reported", "rep_number": None, "action": "recommend_rest"}]}}
    prov = FakeProvider(summarize="You crushed it, keep pushing through!")
    text, source = run(CoachAgent(prov).summarize(ctx, rules_summary(ctx)))
    assert source == "rules" and "health professional" in text and prov.summarize_calls == []


def test_summary_uses_the_llm_when_it_passes_the_checks_else_the_rules_text():
    good = "You did 5 reps and your depth drifted by 8.7 degrees; nice work."
    text, source = run(CoachAgent(FakeProvider(summarize=good)).summarize(SUMMARY_CTX, "RULES"))
    assert (text, source) == (good, "agent")
    for bad in ("You did 50 reps!", "That could cause an injury.", "x" * 701, ""):
        assert run(CoachAgent(FakeProvider(summarize=bad)).summarize(SUMMARY_CTX, "RULES")) == ("RULES", "rules")
    assert run(CoachAgent(FakeProvider(summarize=RuntimeError("x"))).summarize(SUMMARY_CTX, "RULES")) == ("RULES", "rules")


# ---- configuration ----------------------------------------------------------


def test_the_agent_is_off_unless_configured(monkeypatch):
    for name in ("COACH_LLM_PROVIDER", "COACH_LLM_MODEL", "COACH_LLM_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    assert ag.agent_from_env().enabled is False
    monkeypatch.setenv("COACH_LLM_PROVIDER", "openai-or-whatever")
    assert ag.agent_from_env().enabled is False  # unknown provider: rules only, never an error
    monkeypatch.setenv("COACH_LLM_PROVIDER", "anthropic")
    assert ag.agent_from_env().enabled is False  # removed: the team uses Gemini only
    monkeypatch.setenv("COACH_LLM_PROVIDER", "Gemini")
    a = ag.agent_from_env()
    assert a.enabled and a._provider._model == "gemini-3.8-flash" and a._timeout_s == 10.0
    monkeypatch.setenv("COACH_LLM_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("COACH_LLM_TIMEOUT_S", "2.5")
    a = ag.agent_from_env()
    assert a._provider._model == "gemini-3.8-flash" and a._timeout_s == 2.5
    monkeypatch.setenv("COACH_LLM_TIMEOUT_S", "banana")
    assert ag.agent_from_env()._timeout_s == 10.0


# ---- in the live workout loop -----------------------------------------------


def make_client(store, agent, tiger=None):
    return TestClient(create_app(store=store, auth=FakeAuth(), timeseries=tiger or FakeTiger(configured=False), agent=agent))


def new_session(client, headers=ALICE, exercise="squat"):
    return client.post("/api/workouts", json={"exercise": exercise}, headers=headers).json()["session_id"]


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


SHALLOWING = [85, 85, 85, 98, 98]


def stream(ws, sid, depths=SHALLOWING, start_sequence=0):
    for m in squat_messages(sid, reps=len(depths), depths=depths, start_sequence=start_sequence):
        ws.send_text(json.dumps(m))


def open_ws(client, sid, token="alice-token"):
    cm = client.websocket_connect(f"/ws/workouts/{sid}")
    ws = cm.__enter__()
    ws.send_text(json.dumps(auth_message(token)))
    return cm, ws, ws.receive_json()


# Reporter-only (team decision 2026-11-07): during the workout every cue is the rule message, sent at once, and the
# LLM is never consulted, even when it is configured and would answer. Gemini only writes the recap afterwards.


def test_live_cues_are_the_rule_message_even_with_the_llm_on():
    store = InMemoryStore()
    prov = FakeProvider(decide=AgentOutput(action="send_feedback", message="Try to keep your depth like your first reps."))
    with make_client(store, CoachAgent(prov)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            stream(ws, sid)
            events = [ws.receive_json() for _ in range(6)]
        finally:
            cm.__exit__(None, None, None)
    fb = next(e for e in events if e["type"] == "coaching_feedback")
    assert fb["source"] == "rules" and fb["message"].startswith("Your depth has decreased")
    assert fb["issue_code"] == "reduced_depth" and fb["rep_count"] == 5
    assert wait_for(lambda: len(store.events.get(sid, {})) == 1)
    (saved,) = store.events[sid].values()
    assert saved["message"] == fb["message"]  # the record matches the screen
    assert prov.decide_calls == []


def test_a_stuck_llm_cannot_delay_reps_or_cues():
    gate = threading.Event()  # this provider would never answer
    prov = FakeProvider(gate=gate)
    with make_client(InMemoryStore(), CoachAgent(prov, timeout_s=30)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            stream(ws, sid)
            t = time.monotonic()
            types = [ws.receive_json()["type"] for _ in range(6)]
            assert types.count("rep_completed") == 5 and "coaching_feedback" in types
            assert time.monotonic() - t < 2
        finally:
            gate.set()
            cm.__exit__(None, None, None)
    assert prov.decide_calls == []


def test_an_exercise_without_analysis_never_involves_the_llm():
    prov = FakeProvider()
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = new_session(c, exercise="pull-up")
        cm, ws, ready = open_ws(c, sid)
        try:
            assert ready["analysis_supported"] is False
            stream(ws, sid)
            time.sleep(0.3)
        finally:
            cm.__exit__(None, None, None)
    assert prov.decide_calls == []


def test_the_agent_is_closed_with_the_app():
    prov = FakeProvider()
    with make_client(InMemoryStore(), CoachAgent(prov)):
        pass
    assert prov.closed is True


# ---- the recap endpoint -----------------------------------------------------


def finished_workout(c, depths=SHALLOWING, headers=ALICE, token="alice-token", exercise="squat"):
    sid = new_session(c, headers, exercise)
    cm, ws, _ = open_ws(c, sid, token)
    try:
        stream(ws, sid, depths)
        [ws.receive_json() for _ in range(len(depths) + (1 if depths == SHALLOWING else 0))]
    finally:
        cm.__exit__(None, None, None)
    assert wait_for(lambda: c.app.state.store.count_repetitions(sid) == len(depths))
    assert c.post(f"/api/workouts/{sid}/finish", headers=headers).status_code == 200
    return sid


def test_recap_without_the_llm_is_the_deterministic_text():
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = finished_workout(c)
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
    assert r.status_code == 200 and r.json()["source"] == "rules"
    assert "5 reps" in r.json()["summary"] and "shallower" in r.json()["summary"]


def test_recap_uses_the_llm_text_when_valid_and_caches_finished_workouts():
    prov = FakeProvider(summarize="You did 5 reps and your depth drifted a little toward the end. Nice work.")
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = finished_workout(c)
        a = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
        b = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert a == b and a["source"] == "agent" and "drifted a little" in a["summary"]
    assert len(prov.summarize_calls) == 1  # the second request was served from the cache
    assert prov.summarize_calls[0]["supabase"]["stats"]["rep_count"] == 5  # the model got the data, nothing else


def test_an_unfinished_workouts_recap_is_not_cached():
    prov = FakeProvider(summarize="So far you did 0 reps.")
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = new_session(c)
        c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
        c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE)
    assert len(prov.summarize_calls) == 2


def test_recap_falls_back_to_rules_when_the_llm_text_fails_the_checks():
    with make_client(InMemoryStore(), CoachAgent(FakeProvider(summarize="This could cause an injury, stop!"))) as c:
        sid = finished_workout(c)
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert r["source"] == "rules" and "injury" not in r["summary"]


def test_recap_after_a_pain_report_is_always_the_rules_text_and_does_not_call_the_llm():
    store, prov = InMemoryStore(), FakeProvider(summarize="Great job, keep pushing!")
    with make_client(store, CoachAgent(prov)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pain"}))
            ws.receive_json(), ws.receive_json()
            assert wait_for(lambda: len(store.events.get(sid, {})) == 1)
        finally:
            cm.__exit__(None, None, None)
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert r["source"] == "rules" and "health professional" in r["summary"] and prov.summarize_calls == []


def test_recap_for_an_exercise_without_analysis_says_it_was_only_logged():
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = new_session(c, exercise="Pull Up")
        r = c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).json()
    assert "pull-up workout was logged" in r["summary"]


def test_recap_works_when_tiger_data_is_down_and_is_private_and_limited():
    tiger = FakeTiger()
    tiger.fail = True
    with make_client(InMemoryStore(), CoachAgent(None), tiger) as c:
        sid = new_session(c)
        assert c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).status_code == 200  # degrades, no 500
        assert c.get(f"/api/workouts/{sid}/coach-summary", headers=BOB).status_code == 404
        assert c.get(f"/api/workouts/{sid}/coach-summary").status_code == 401
        codes = [c.get(f"/api/workouts/{sid}/coach-summary", headers=ALICE).status_code for _ in range(5)]
    assert codes[-1] == 429 and 429 in codes  # each recap can cost an LLM call, so it has its own tight limit
