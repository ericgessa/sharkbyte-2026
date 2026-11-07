"""Posture cues from the upper-back IMU, end to end through the shared coach path (agent.py + websocket.py).

All sensor data here is SIMULATED (app/sensor_simulation.py). Nothing calls a real LLM: FakeProvider scripts it.
"""

import json
import time
from types import SimpleNamespace

import pytest

from app.api import websocket as wsmod
from app.sensor_simulation import generate
from app.services.agent import AgentOutput, CoachAgent, _check_text, build_posture_context
from app.services.posture import MESSAGES, analyze
from tests.fakes import InMemoryStore
from tests.test_agent import CANDIDATE, CTX, FakeProvider, make_client, new_session, open_ws, run, wait_for

RATE_HZ = 20  # a whole simulated set (~230 messages) stays inside the socket's burst limit of 600


def _posture_decision(scenario):
    coach = analyze(generate(scenario).messages)
    return coach.decisions[0], coach


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


def _barrier(ws):
    """Ask for a session_state reply: everything the server sent earlier has arrived before it."""
    ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": ws.sid, "signal": "resume"}))
    return _next_of_type(ws, "session_state")[1]


def _open(c, sid):
    cm, ws, ready = open_ws(c, sid)
    ws.sid = sid
    return cm, ws, ready


# ---- the context the model sees, and the checks on its reply -----------------------------------


def test_posture_context_is_small_server_computed_and_carries_the_wording_rules():
    decision, coach = _posture_decision("lateral_shift")
    ctx = build_posture_context("squat", decision, coach)
    assert ctx["source"] == "upper_back_imu"
    assert ctx["detected_issue"]["code"] == "lateral_shift" and "one side" in ctx["detected_issue"]["meaning"]
    assert ctx["rule_message"] == MESSAGES["lateral_shift"].format(side="right")
    assert ctx["evidence"] == decision.evidence  # the numbers behind the rule decision, not the raw stream
    assert ctx["numbers_allowed"] is False and ctx["side"] == "right"
    assert len(json.dumps(ctx)) < 2000


def test_the_cue_being_worded_is_not_listed_as_already_given():
    """Otherwise the model reads its own cue as a repeat and chooses no_action (seen on a live Gemini run)."""
    coach = analyze(generate("forward_lean", reps=20).messages)
    first, second = coach.decisions[:2]
    assert build_posture_context("squat", first, coach)["feedback_already_given"] == [
        {"rep": d.rep_number, "issue": d.issue_code} for d in coach.decisions[1:]
    ]
    listed = build_posture_context("squat", second, coach)["feedback_already_given"]
    assert {"rep": first.rep_number, "issue": first.issue_code} in listed
    assert {"rep": second.rep_number, "issue": second.issue_code} not in listed


def test_every_posture_issue_has_a_meaning_for_the_model():
    for scenario in ("forward_lean", "lean_creep", "lateral_shift", "slouched_standing"):
        decision, coach = _posture_decision(scenario)
        ctx = build_posture_context("squat", decision, coach)
        assert ctx["detected_issue"]["meaning"] != decision.issue_code


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Your shoulders dipped about 7 degrees to the right.", "number_not_allowed"),
        ("Your shoulders dip to the left at the bottom.", "wrong_side"),
        ("Keep your shoulders level.", "wrong_side"),  # must name the measured side
        ("Don't lean left or right.", "wrong_side"),
        ("Keep your spine neutral and your shoulders level on the right.", "medical_language"),
    ],
)
def test_posture_wording_rules_are_enforced(text, reason):
    decision, coach = _posture_decision("lateral_shift")
    assert _check_text(text, build_posture_context("squat", decision, coach), 200) == reason


def test_good_posture_wording_passes():
    decision, coach = _posture_decision("lateral_shift")
    ctx = build_posture_context("squat", decision, coach)
    assert _check_text("Your shoulders dip to the right as you sit down. Press evenly through both feet.", ctx, 200) is None


def test_camera_cues_still_allow_numbers_from_the_data():
    """The posture-only rules are opt-in through the context: camera cue checks are unchanged."""
    assert "numbers_allowed" not in CTX and "side" not in CTX
    assert _check_text("Your knees reached 85 degrees in your first reps.", CTX, 200) is None


def test_an_llm_reply_with_numbers_falls_back_to_the_rule_message():
    decision, coach = _posture_decision("forward_lean")
    ctx = build_posture_context("squat", decision, coach)
    agent = CoachAgent(FakeProvider(decide=AgentOutput(action="send_feedback", message="You leaned 62 degrees forward.")))
    candidate = CANDIDATE.__class__(decision.rep_number, decision.issue_code, decision.message)
    assert run(agent.refine(candidate, ctx)) is candidate
    assert agent.fallback_reasons == {"number_not_allowed": 1}


# ---- live: sensor_update over the workout WebSocket ----------------------------------------------


def test_a_posture_cue_reaches_the_app_worded_by_the_llm_and_is_saved():
    store = InMemoryStore()
    prov = FakeProvider(decide=AgentOutput(action="send_feedback", message="Your shoulders dip to the right; press evenly through both feet."))
    with make_client(store, CoachAgent(prov)) as c:
        sid = new_session(c)
        cm, ws, _ = _open(c, sid)
        try:
            _send_sensor_set(ws, sid, "lateral_shift")
            fb, _ = _next_of_type(ws, "coaching_feedback")
        finally:
            cm.__exit__(None, None, None)
    assert fb["issue_code"] == "lateral_shift" and fb["source"] == "agent"
    assert fb["message"] == "Your shoulders dip to the right; press evenly through both feet."
    assert fb["rep_count"] == 2  # no camera frames: the IMU's own rep number
    assert wait_for(lambda: len(store.events.get(sid, {})) == 1)
    (saved,) = store.events[sid].values()
    assert saved["issue_code"] == "lateral_shift" and saved["message"] == fb["message"]
    (ctx,) = prov.decide_calls
    assert ctx["source"] == "upper_back_imu" and ctx["side"] == "right" and ctx["numbers_allowed"] is False


def test_without_the_llm_the_posture_rule_message_is_sent():
    store = InMemoryStore()
    with make_client(store, CoachAgent(None)) as c:
        sid = new_session(c)
        cm, ws, _ = _open(c, sid)
        try:
            _send_sensor_set(ws, sid, "forward_lean")
            fb, _ = _next_of_type(ws, "coaching_feedback")
        finally:
            cm.__exit__(None, None, None)
    assert fb["issue_code"] == "excessive_forward_lean" and fb["source"] == "rules"
    assert fb["message"] == MESSAGES["excessive_forward_lean"]


@pytest.mark.parametrize("scenario", ["good_form", "sensor_dropout"])
def test_good_form_gets_no_posture_cue(scenario):
    prov = FakeProvider()
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = new_session(c)
        cm, ws, _ = _open(c, sid)
        try:
            _send_sensor_set(ws, sid, scenario, reps=5)
            time.sleep(0.2)
            assert "coaching_feedback" not in _barrier(ws)
        finally:
            cm.__exit__(None, None, None)
    assert prov.decide_calls == []


def test_after_a_pain_report_posture_cues_stop():
    prov = FakeProvider()
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = new_session(c)
        cm, ws, _ = _open(c, sid)
        try:
            ws.send_text(json.dumps({"schema_version": 1, "type": "user_signal", "session_id": sid, "signal": "pain"}))
            _next_of_type(ws, "session_state")
            _send_sensor_set(ws, sid, "forward_lean")
            time.sleep(0.2)
            assert "coaching_feedback" not in _barrier(ws)
        finally:
            cm.__exit__(None, None, None)
    assert prov.decide_calls == []


def test_posture_cues_are_only_for_squats():
    prov = FakeProvider()
    with make_client(InMemoryStore(), CoachAgent(prov)) as c:
        sid = new_session(c, exercise="push-up")
        cm, ws, _ = _open(c, sid)
        try:
            _send_sensor_set(ws, sid, "forward_lean")
            time.sleep(0.2)
            assert "coaching_feedback" not in _barrier(ws)
        finally:
            cm.__exit__(None, None, None)
    assert prov.decide_calls == []


# ---- spacing between cues ------------------------------------------------------------------------


class _Ws:
    def __init__(self, agent):
        self.app = SimpleNamespace(state=SimpleNamespace(agent=agent))


def _live(last_cue_at=None):
    return SimpleNamespace(
        exercise=SimpleNamespace(slug="squat"),
        posture={},
        pain_reported=False,
        last_cue_at=last_cue_at,
        counter=SimpleNamespace(rep_count=0),
        session_id="s",
        agent_tasks=set(),
        socket=None,
        finished=False,
    )


def _feed(live, agent, scenario="forward_lean"):
    from app.schemas.sensors import SensorUpdate

    ws = _Ws(agent)

    async def go():
        for m in generate(scenario, session_id="00000000-0000-4000-8000-000000000001").messages:
            await wsmod._coach_posture(ws, live, SensorUpdate.model_validate(m))

    run(go())


def test_a_posture_cue_waits_if_another_cue_just_went_out():
    prov = FakeProvider()
    live = _live(last_cue_at=time.monotonic() + 3600)  # "a cue just went out" for the whole replay
    _feed(live, CoachAgent(prov))
    assert prov.decide_calls == []  # held back, never sent to the LLM
    assert live.posture["esp32-01"].decisions  # ...although the posture rules did decide on one


def test_a_posture_cue_goes_out_when_the_last_cue_is_old():
    prov = FakeProvider(decide=AgentOutput(action="no_action", message=""))
    live = _live(last_cue_at=time.monotonic() - wsmod.MIN_CUE_GAP_S - 1)
    _feed(live, CoachAgent(prov))
    assert len(prov.decide_calls) == 1


def test_only_two_devices_per_workout_get_posture_coaches():
    from app.schemas.sensors import SensorUpdate

    live = _live()
    ws = _Ws(CoachAgent(None))
    m = generate("good_form").messages[0]

    async def go():
        for device in ("a", "b", "c"):
            await wsmod._coach_posture(ws, live, SensorUpdate.model_validate({**m, "device_id": device}))

    run(go())
    assert set(live.posture) == {"a", "b"}
