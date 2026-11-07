"""Gemini adapter, against a FAKE HTTP server: the real google-genai SDK builds the request and reads the reply, but
nothing leaves the machine. This verifies the request shape and every failure mapping; it does NOT verify that the
real Gemini API accepts the request (no key yet). Guardrails and fallbacks are shared with every provider (CoachAgent)."""

import json
import logging

import httpx2
import pytest
from google import genai
from google.genai import types as gtypes

from app.services import agent as ag
from app.services.agent import AgentError, AgentOutput, AnthropicProvider, CoachAgent, GeminiProvider
from tests.test_agent import CANDIDATE, CTX, SUMMARY_CTX, run


def gemini_ok(payload, finish="STOP"):
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(payload)}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7, "totalTokenCount": 12},
    }


def gemini_with(handler, **kw):
    calls = []

    def wrapped(request):
        calls.append({"method": request.method, "url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return handler(request)

    client = genai.Client(
        api_key="test-key",
        http_options=gtypes.HttpOptions(
            httpx_async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(wrapped)),
            retry_options=gtypes.HttpRetryOptions(attempts=1),
        ),
    )
    return GeminiProvider(client=client, **kw), calls


def ok_handler(payload, finish="STOP"):
    return lambda request: httpx2.Response(200, json=gemini_ok(payload, finish))


def api_error(code, status, message="x"):
    return httpx2.Response(code, json={"error": {"code": code, "message": message, "status": status}})


def test_the_request_has_the_shape_the_api_documents():
    prov, calls = gemini_with(ok_handler({"action": "send_feedback", "message": "Keep it steady."}))
    assert run(prov.decide(CTX)) == AgentOutput(action="send_feedback", message="Keep it steady.")
    (c,) = calls
    assert c["method"] == "POST" and c["url"].split("?")[0].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert c["headers"].get("x-goog-api-key") == "test-key" and "test-key" not in c["url"]  # key in a header, never the URL
    body = c["body"]
    assert set(body) == {"contents", "systemInstruction", "generationConfig"}
    gc = body["generationConfig"]
    assert set(gc) == {"maxOutputTokens", "responseMimeType", "responseJsonSchema"}  # no sampling overrides
    assert gc["responseMimeType"] == "application/json" and gc["maxOutputTokens"] == 2000
    assert gc["responseJsonSchema"]["additionalProperties"] is False and set(gc["responseJsonSchema"]["required"]) == {"action", "message"}
    assert gc["responseJsonSchema"]["properties"]["action"]["enum"] == ["send_feedback", "no_action", "recommend_rest"]
    assert "medical advice" in body["systemInstruction"]["parts"][0]["text"]
    assert body["contents"][0]["parts"][0]["text"] == json.dumps(CTX, sort_keys=True)  # server numbers only


def test_the_summary_request_uses_its_own_prompt_and_schema():
    prov, calls = gemini_with(ok_handler({"summary": "You did 5 reps."}))
    assert run(prov.summarize(SUMMARY_CTX)) == "You did 5 reps."
    assert list(calls[0]["body"]["generationConfig"]["responseJsonSchema"]["properties"]) == ["summary"]
    assert "recap" in calls[0]["body"]["systemInstruction"]["parts"][0]["text"]


def test_the_model_can_be_configured():
    prov, calls = gemini_with(ok_handler({"action": "no_action", "message": ""}), model="gemini-3.1-flash-lite")
    run(prov.decide(CTX))
    assert "/models/gemini-3.1-flash-lite:generateContent" in calls[0]["url"]


@pytest.mark.parametrize(
    "reply, reason",
    [
        (httpx2.Response(200, json=gemini_ok({"action": "send_feedback", "message": "tru"}, finish="MAX_TOKENS")), "finish_max_tokens"),
        (httpx2.Response(200, json=gemini_ok({"action": "send_feedback", "message": "x"}, finish="SAFETY")), "finish_safety"),
        (httpx2.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "blocked"),
        (httpx2.Response(200, json={"candidates": []}), "no_candidates"),
        (httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}]}), "no_text"),
        (httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "not json"}]}, "finishReason": "STOP"}]}), "invalid_json"),
        (httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": '["a list"]'}]}, "finishReason": "STOP"}]}), "invalid_json"),
        (httpx2.Response(200, json=gemini_ok({"action": "delete_everything", "message": "x"})), "schema_violation"),
        (httpx2.Response(200, json=gemini_ok({"action": "send_feedback", "message": "x", "extra": 1})), "schema_violation"),
        (api_error(401, "UNAUTHENTICATED"), "AuthenticationError"),
        (api_error(403, "PERMISSION_DENIED"), "AuthenticationError"),
        (api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), "AuthenticationError"),
        (api_error(404, "NOT_FOUND", "model not found"), "NotFoundError"),
        (api_error(429, "RESOURCE_EXHAUSTED"), "rate_limited"),
        (api_error(500, "INTERNAL"), "ServerError"),
        (api_error(400, "INVALID_ARGUMENT", "bad field"), "ClientError"),
    ],
)
def test_every_bad_reply_has_a_clear_reason_and_the_user_still_gets_the_rule_message(reply, reason):
    prov, calls = gemini_with(lambda request: reply)
    agent = CoachAgent(prov)
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE
    assert agent.fallback_reasons == {reason: 1}
    assert len(calls) == 1  # never retried: the rule message is the immediate fallback


def test_misconfiguration_triggers_the_one_time_operator_warning(caplog):
    prov, _ = gemini_with(lambda request: api_error(403, "PERMISSION_DENIED"))
    agent = CoachAgent(prov)
    with caplog.at_level(logging.INFO, logger="app.services.agent"):
        run(agent.refine(CANDIDATE, CTX))
        run(agent.refine(CANDIDATE, CTX))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "GEMINI_API_KEY" in warnings[0].message


def test_a_missing_key_is_named_clearly_with_the_real_sdk(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    prov = GeminiProvider()
    with pytest.raises(AgentError, match="no_credentials"):
        run(prov.decide(CTX))
    agent = CoachAgent(GeminiProvider())
    assert run(agent.refine(CANDIDATE, CTX)) is CANDIDATE and agent.fallback_reasons == {"no_credentials": 1}


def test_unsafe_wording_is_rejected_for_gemini_exactly_as_for_any_provider():
    for bad in ("That risks an injury.", "Aim for 120 degrees.", "See https://x.example"):
        prov, _ = gemini_with(ok_handler({"action": "send_feedback", "message": bad}))
        assert run(CoachAgent(prov).refine(CANDIDATE, CTX)) is CANDIDATE  # the guardrails live in CoachAgent, not the vendor


def test_gemini_plus_agent_end_to_end_with_the_fake_server():
    prov, _ = gemini_with(ok_handler({"action": "send_feedback", "message": "Hold your depth like your first reps."}))
    final = run(CoachAgent(prov).refine(CANDIDATE, CTX))
    assert final.source == "agent" and final.message == "Hold your depth like your first reps."


def test_the_provider_is_chosen_by_the_environment(monkeypatch):
    for name in ("COACH_LLM_MODEL", "COACH_LLM_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COACH_LLM_PROVIDER", "gemini")
    a = ag.agent_from_env()
    assert a.enabled and isinstance(a._provider, GeminiProvider) and a._provider._model == "gemini-3.5-flash-lite"
    monkeypatch.setenv("COACH_LLM_PROVIDER", "Google")  # alias, any case
    assert isinstance(ag.agent_from_env()._provider, GeminiProvider)
    monkeypatch.setenv("COACH_LLM_MODEL", "gemini-3.1-flash-lite")
    assert ag.agent_from_env()._provider._model == "gemini-3.1-flash-lite"
    monkeypatch.delenv("COACH_LLM_MODEL")
    monkeypatch.setenv("COACH_LLM_PROVIDER", "anthropic")
    b = ag.agent_from_env()
    assert isinstance(b._provider, AnthropicProvider) and b._provider._model == "claude-opus-5-5"  # each keeps its own default
    monkeypatch.setenv("COACH_LLM_PROVIDER", "openai")
    assert ag.agent_from_env().enabled is False


def test_closing_the_provider_closes_the_sdk_client():
    prov, _ = gemini_with(ok_handler({"action": "no_action", "message": ""}))
    run(prov.decide(CTX))
    run(prov.aclose())  # must not raise


def test_the_provider_builds_its_own_client_with_one_attempt_and_the_configured_timeout(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(genai, "Client", FakeClient)
    GeminiProvider(timeout_s=3)._get_client()
    opts = captured["http_options"]
    assert opts.retry_options.attempts == 1  # the SDK's own retries (default is several) would delay the rule fallback
    assert opts.timeout == 6000  # milliseconds: twice the cue timeout, so the agent's own timeout fires first
    assert "api_key" not in captured  # the key is never handled here: the SDK reads it from the environment
