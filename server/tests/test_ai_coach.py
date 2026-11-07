"""AI coach wording layer, with a mocked LLM (no network). Posture warnings come from SIMULATED data."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.sensor_simulation import generate
from app.services.ai_coach import (
    DEFAULT_MODEL,
    ISSUE_MEANING,
    SYSTEM_PROMPT,
    AICoach,
    FakeLLM,
    GeminiLLM,
    InvalidReply,
    MissingApiKey,
    build_prompt,
    validate_reply,
)
from app.services.posture import ISSUE_CODES, analyze


def _decision(scenario: str):
    return analyze(generate(scenario).messages).decisions[0]


class ScriptedLLM:
    """Replies with a fixed string (or raises / stalls), and records what it was asked."""

    def __init__(self, reply: str = "", *, raises: Exception | None = None, delay_s: float = 0.0) -> None:
        self.reply, self.raises, self.delay_s = reply, raises, delay_s
        self.calls: list[tuple[str, str, dict]] = []

    async def generate(self, system, prompt, schema):
        self.calls.append((system, prompt, schema))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises:
            raise self.raises
        return self.reply


def _reply(message: str) -> str:
    return json.dumps({"message": message})


def _phrase(coach: AICoach, decision):
    return asyncio.run(coach.phrase(decision))


def test_good_reply_replaces_only_the_wording():
    d = _decision("forward_lean")
    out = _phrase(AICoach(ScriptedLLM(_reply("Keep your chest a bit taller as you sit down into the squat."))), d)
    assert out.source == "agent"
    assert out.message == "Keep your chest a bit taller as you sit down into the squat."
    assert (out.rep_number, out.issue_code, out.evidence, out.event_id, out.action) == (
        d.rep_number,
        d.issue_code,
        d.evidence,
        d.event_id,
        d.action,
    )


@pytest.mark.parametrize(
    "raw, why",
    [
        ("not json", "not the expected JSON"),
        (json.dumps({"text": "Stand tall."}), "not the expected JSON"),
        (json.dumps({"message": "Stand tall.", "extra": 1}), "not the expected JSON"),
        (_reply("   "), "empty"),
        (_reply("Stand tall " * 30), "too long"),
        (_reply("You leaned 62 degrees forward."), "numbers"),
        (_reply("Read more at https://example.com"), "link"),
        (_reply("Keep your spine neutral."), "forbidden"),
        (_reply("This could cause an injury."), "forbidden"),
        (_reply("If it hurts, see a doctor."), "forbidden"),
        (_reply("Your sensor shows a lean."), "forbidden"),
    ],
)
def test_invalid_replies_are_rejected(raw, why):
    with pytest.raises(InvalidReply, match=why):
        validate_reply(raw, _decision("forward_lean"))


def test_lateral_shift_must_name_the_measured_side():
    d = _decision("lateral_shift")
    assert d.evidence["side"] == "right"
    assert validate_reply(_reply("Your shoulders dip to the right; press evenly through both feet."), d)
    for wrong in ("Your shoulders dip to the left.", "Keep your shoulders level.", "Don't lean left or right."):
        with pytest.raises(InvalidReply, match="right side only"):
            validate_reply(_reply(wrong), d)


@pytest.mark.parametrize(
    "llm, reason",
    [
        (ScriptedLLM(_reply("Your spine is curved.")), "invalid reply"),
        (ScriptedLLM(raises=ConnectionError("down")), "ConnectionError"),
        (ScriptedLLM(raises=RuntimeError("quota")), "RuntimeError"),
        (ScriptedLLM(_reply("Too slow."), delay_s=1.0), "timed out"),
        (None, "no LLM configured"),
    ],
)
def test_any_problem_falls_back_to_the_rule_message(llm, reason):
    d = _decision("forward_lean")
    coach = AICoach(llm, timeout_s=0.05)
    out = _phrase(coach, d)
    assert out == d  # unchanged, source "rules"
    assert out.source == "rules"
    assert coach.fallbacks == 1 and reason in coach.last_fallback_reason


def test_prompt_carries_the_issue_and_evidence_and_recent_cues():
    llm = ScriptedLLM(_reply("Stand tall between reps and set your shoulders back."))
    coach = AICoach(llm)
    d = _decision("slouched_standing")
    _phrase(coach, d)
    _phrase(coach, d)
    system, prompt, schema = llm.calls[1]
    body = json.loads(prompt)
    assert system == SYSTEM_PROMPT
    assert body["issue"] == "rounded_standing_posture"
    assert body["evidence"] == d.evidence
    assert body["default_cue"] == d.message
    assert body["recent_cues"] == ["Stand tall between reps and set your shoulders back."]
    assert schema["required"] == ["message"]


def test_every_issue_has_a_meaning_for_the_model():
    assert set(ISSUE_MEANING) == set(ISSUE_CODES)
    for scenario in ("forward_lean", "lean_creep", "lateral_shift", "slouched_standing"):
        assert json.loads(build_prompt(_decision(scenario), []))["what_it_means"] in ISSUE_MEANING.values()


def test_fake_llm_runs_the_whole_flow_offline():
    d = _decision("lateral_shift")
    out = _phrase(AICoach(FakeLLM()), d)
    assert out.source == "agent" and out.message == f"Coach: {d.message}"


def test_debug_lines_show_the_request_and_the_reply(caplog):
    caplog.set_level(logging.DEBUG, logger="app.services.ai_coach")
    _phrase(AICoach(ScriptedLLM(_reply("Keep your chest up tall."))), _decision("forward_lean"))
    assert "-> ScriptedLLM request" in caplog.text
    assert '"issue": "excessive_forward_lean"' in caplog.text
    assert "<- ScriptedLLM reply" in caplog.text
    assert "Keep your chest up tall." in caplog.text


def test_debug_lines_truncate_long_text(caplog):
    caplog.set_level(logging.DEBUG, logger="app.services.ai_coach")
    _phrase(AICoach(ScriptedLLM(_reply("x" * 5000))), _decision("forward_lean"))
    reply_line = next(r.getMessage() for r in caplog.records if "reply in" in r.getMessage())
    assert "[+" in reply_line and len(reply_line) < 1000


def test_no_debug_lines_by_default(caplog):
    caplog.set_level(logging.INFO, logger="app.services.ai_coach")
    _phrase(AICoach(ScriptedLLM(_reply("Keep your chest up tall."))), _decision("forward_lean"))
    assert "request" not in caplog.text


def test_gemini_needs_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(MissingApiKey):
        GeminiLLM()


def test_old_sdk_is_refused_with_a_clear_error():
    genai = pytest.importorskip("google.genai")
    if hasattr(genai.types, "ThinkingLevel"):
        pytest.skip("installed google-genai is new enough")
    with pytest.raises(RuntimeError, match="too old"):
        GeminiLLM(api_key="test-key")


def test_gemini_request_shape(monkeypatch):
    """Uses the real google-genai types with the network call replaced. Skipped unless google-genai >= 3.0."""
    pytest.importorskip("google.genai", minversion="3.0")
    from google.genai import types

    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    llm = GeminiLLM(api_key="test-key")
    seen = {}

    class _Resp:
        text = _reply("Keep your chest up.")

    async def fake_generate_content(*, model, contents, config):
        seen.update(model=model, contents=contents, config=config)
        return _Resp()

    monkeypatch.setattr(llm._client.aio.models, "generate_content", fake_generate_content)
    raw = asyncio.run(llm.generate("sys", "prompt", {"type": "object"}))

    assert raw == _reply("Keep your chest up.")
    assert seen["model"] == DEFAULT_MODEL == "gemini-3.8-flash"
    cfg = seen["config"]
    assert isinstance(cfg, types.GenerateContentConfig)
    assert cfg.system_instruction == "sys"
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == {"type": "object"}
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW


def test_api_deadline_is_never_below_the_10s_minimum():
    """Gemini rejects server deadlines under 10 s (400 INVALID_ARGUMENT); our 4 s limit is enforced client side."""
    pytest.importorskip("google.genai", minversion="3.0")
    assert GeminiLLM(api_key="test-key", timeout_s=4).http_timeout_ms == 10_000
    assert GeminiLLM(api_key="test-key", timeout_s=15).http_timeout_ms == 15_000


def test_fallback_reason_includes_the_error_message():
    coach = AICoach(ScriptedLLM(raises=RuntimeError("400 INVALID_ARGUMENT: bad deadline")))
    _phrase(coach, _decision("forward_lean"))
    assert coach.last_fallback_reason == "RuntimeError: 400 INVALID_ARGUMENT: bad deadline"
