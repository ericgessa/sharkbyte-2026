"""Posture cues from the upper-back IMU over the live socket, and how they feed the end-of-workout report.

Live cues are deterministic (rule messages only); Gemini is only the reporter after the workout (agent.py).
All sensor data here is SIMULATED (app/sensor_simulation.py). Nothing calls a real LLM: FakeProvider scripts it.
"""

import json
import time
from types import SimpleNamespace

import pytest

from app.api import websocket as wsmod
from app.sensor_simulation import generate
from app.services.agent import FOCUS, ISSUE_LABELS, ISSUE_MEANINGS, CoachAgent, issues_seen, report_context, rules_summary
from app.services.posture import ISSUE_CODES, MESSAGES
from tests.fakes import InMemoryStore
from tests.test_agent import FakeProvider, make_client, new_session, open_ws, run, wait_for

RATE_HZ = 20  # a whole simulated set (~230 messages) stays inside the socket's burst limit of 600


def _send_sensor_set(ws, sid, scenario, reps=3):
    for m in generate(scenario, reps=reps, rate_hz=RATE_HZ, session_id=sid).messages:
        ws.send_text(json.dumps(m))


def _next_of_type(ws, wanted):
    """Read until a message of type ``wanted`` (skipping others); return it with the types seen before it."""
    seen = []
    while True:
        msg = ws.receive_json()
        if msg["type"] == wanted:
            return msg, seen
        seen.append(msg["type"])


def _barrier(ws, sid):
    """Ask for a session_state reply: everything the server sent earlier has arrived before it."""
    ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "resume"}))
    return _next_of_type(ws, "session_state")[1]


# ---- live: sensor_update over the workout WebSocket ----------------------------------------------


@pytest.mark.parametrize("llm_on", [False, True])
def test_a_posture_cue_is_the_rule_message_and_never_calls_the_llm(llm_on):
    store = InMemoryStore()
    prov = FakeProvider()
    with make_client(store, CoachAgent(prov if llm_on else None)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            _send_sensor_set(ws, sid, "slouched_standing")
            fb, _ = _next_of_type(ws, "coaching_feedback")
        finally:
            cm.__exit__(None, None, None)
    assert fb["issue_code"] == "rounded_standing_posture" and fb["source"] == "rules"
    assert fb["message"] == MESSAGES["rounded_standing_posture"]
    assert fb["rep_count"] == 2  # no camera frames: the IMU's own rep number
    assert wait_for(lambda: len(store.events.get(sid, {})) == 1)
    (saved,) = store.events[sid].values()
    assert saved["issue_code"] == "rounded_standing_posture" and saved["message"] == fb["message"]
    assert prov.decide_calls == []  # Gemini is the reporter only: never consulted during the workout


def test_forward_lean_cue_reaches_the_app():
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            _send_sensor_set(ws, sid, "forward_lean")
            fb, _ = _next_of_type(ws, "coaching_feedback")
        finally:
            cm.__exit__(None, None, None)
    assert fb["issue_code"] == "excessive_forward_lean" and fb["message"] == MESSAGES["excessive_forward_lean"]


@pytest.mark.parametrize("scenario", ["good_form", "sensor_dropout"])
def test_good_form_gets_no_posture_cue(scenario):
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            _send_sensor_set(ws, sid, scenario, reps=5)
            assert "coaching_feedback" not in _barrier(ws, sid)
        finally:
            cm.__exit__(None, None, None)


def test_after_a_pain_report_posture_cues_stop():
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = new_session(c)
        cm, ws, _ = open_ws(c, sid)
        try:
            ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pain"}))
            _next_of_type(ws, "session_state")
            _send_sensor_set(ws, sid, "forward_lean")
            assert "coaching_feedback" not in _barrier(ws, sid)
        finally:
            cm.__exit__(None, None, None)


def test_posture_cues_are_only_for_squats():
    with make_client(InMemoryStore(), CoachAgent(None)) as c:
        sid = new_session(c, exercise="push-up")
        cm, ws, _ = open_ws(c, sid)
        try:
            _send_sensor_set(ws, sid, "forward_lean")
            assert "coaching_feedback" not in _barrier(ws, sid)
        finally:
            cm.__exit__(None, None, None)


# ---- spacing between cues ------------------------------------------------------------------------


def _live(last_cue_at=None):
    return SimpleNamespace(
        exercise=SimpleNamespace(slug="squat"),
        posture={},
        pain_reported=False,
        last_cue_at=last_cue_at,
        counter=SimpleNamespace(rep_count=0),
        session_id="s",
        socket=None,
        finished=False,
    )


def _feed(live, sent, scenario="forward_lean", device="esp32-01", first_only=False):
    from app.schemas.sensors import SensorUpdate

    async def fake_deliver(ws, live_, decision):
        live_.last_cue_at = time.monotonic()
        sent.append(decision)

    original = wsmod._deliver_coaching
    wsmod._deliver_coaching = fake_deliver
    try:
        msgs = generate(scenario, session_id="00000000-0000-4000-8000-000000000001").messages
        if first_only:
            msgs = msgs[:1]

        async def go():
            for m in msgs:
                await wsmod._coach_posture(None, live, SensorUpdate.model_validate({**m, "device_id": device}))

        run(go())
    finally:
        wsmod._deliver_coaching = original


def test_a_posture_cue_waits_if_another_cue_just_went_out():
    sent = []
    live = _live(last_cue_at=time.monotonic() + 3600)  # "a cue just went out" for the whole replay
    _feed(live, sent)
    assert sent == []
    assert live.posture["esp32-01"].decisions  # ...although the posture rules did decide on one


def test_a_posture_cue_goes_out_when_the_last_cue_is_old():
    sent = []
    _feed(_live(last_cue_at=time.monotonic() - wsmod.MIN_CUE_GAP_S - 1), sent)
    assert [d.issue_code for d in sent] == ["excessive_forward_lean"]
    assert sent[0].source == "rules"


def test_only_two_devices_per_workout_get_posture_coaches():
    live, sent = _live(), []
    for device in ("a", "b", "c"):
        _feed(live, sent, scenario="good_form", device=device, first_only=True)
    assert set(live.posture) == {"a", "b"}


# ---- the end-of-workout report: deterministic parts ---------------------------------------------


def _ctx(*codes, reps=8):
    return {"supabase": {"stats": {"rep_count": reps}, "coaching_events": [{"issue_code": c} for c in codes]}}


def test_every_issue_has_a_label_a_meaning_and_a_focus():
    for code in (*ISSUE_CODES, "reduced_depth", "asymmetry"):
        assert code in ISSUE_LABELS and code in ISSUE_MEANINGS and code in FOCUS


def test_issues_are_ranked_by_how_often_they_came_up_then_by_first_seen():
    ranked = issues_seen(_ctx("lateral_shift", "reduced_depth", "reduced_depth", "pain_reported", "excessive_forward_lean"))
    assert [(i["code"], i["times"]) for i in ranked] == [
        ("reduced_depth", 2),
        ("lateral_shift", 1),
        ("excessive_forward_lean", 1),
    ]
    assert ranked[0]["meaning"] == ISSUE_MEANINGS["reduced_depth"]


def test_the_report_context_carries_the_issues_and_one_focus_chosen_by_code():
    rc = report_context(_ctx("excessive_forward_lean", "excessive_forward_lean", "reduced_depth"))
    assert [i["code"] for i in rc["issues_seen"]] == ["excessive_forward_lean", "reduced_depth"]
    assert rc["suggested_focus"] == FOCUS["excessive_forward_lean"]
    assert report_context(_ctx())["suggested_focus"] is None  # no issues: nothing to fix


def test_the_rules_report_names_what_came_up_and_one_focus():
    t = rules_summary(_ctx("rounded_standing_posture", "lateral_shift", "rounded_standing_posture"))
    assert "upper back tilted forward between reps (2x)" in t and "shoulders tilting to one side" in t
    assert t.endswith(f"Next time, focus on this: {FOCUS['rounded_standing_posture']}.")


def test_after_pain_the_rules_report_gives_no_technique_focus():
    t = rules_summary(_ctx("excessive_forward_lean", "pain_reported"))
    assert "focus on this" not in t and "health professional" in t


def test_gemini_gets_the_issue_breakdown_and_the_focus():
    prov = FakeProvider(summarize="Nice work today. Next time, keep your chest a little taller at the bottom of each rep.")
    text, source = run(CoachAgent(prov).summarize(_ctx("excessive_forward_lean"), "fallback"))
    assert source == "agent" and text.startswith("Nice work")
    (sent,) = prov.summarize_calls
    assert sent["issues_seen"][0]["code"] == "excessive_forward_lean"
    assert sent["suggested_focus"] == FOCUS["excessive_forward_lean"]


def test_a_report_mentioning_the_spine_falls_back_to_the_rules_text():
    prov = FakeProvider(summarize="Keep your spine neutral next time.")
    assert run(CoachAgent(prov).summarize(_ctx("excessive_forward_lean"), "fallback")) == ("fallback", "rules")
