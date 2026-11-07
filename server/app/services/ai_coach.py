"""AI coach layer: Gemini turns a posture warning into one short, natural cue for the app's warning banner.

Division of labour (AGENTS.md: the LLM never counts reps or decides what happened):
  * app/services/posture.py decides WHETHER to warn and WHAT about (deterministic, tested, rate limited).
  * This module only decides HOW TO SAY IT. Gemini gets the issue code and its measured evidence and returns
    a JSON ``{"message": ...}``. Everything it returns is validated; on a timeout, an API error, or any
    invalid reply the rule's template message is used instead, so the user always gets a sensible warning.

The model call is behind a small ``LLM`` protocol so tests use a fake and another provider can be swapped in.
The Gemini client needs the ``google-genai`` package (>= 3.0) and ``GEMINI_API_KEY`` in the server
environment. The key is backend only and must never ship in the mobile app.

Try it against the mock dataset (from ``server/``, with GEMINI_API_KEY set):
    python -m app.sensor_simulation --out mock_data/sensors
    python -m app.services.ai_coach mock_data/sensors/lean_creep.jsonl
    python -m app.services.ai_coach mock_data/sensors/lateral_shift.jsonl --fake    # no API call
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.posture import PostureDecision

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"
TIMEOUT_S = 4.0  # a cue that arrives later than this is about a rep the user has already forgotten
MAX_MESSAGE_CHARS = 160
DEBUG_MAX_CHARS = 800  # debug lines truncate the prompt/reply past this
# The SDK sends its HTTP timeout to Google as a server deadline, and the API rejects deadlines under 10 s
# (400 INVALID_ARGUMENT). So the SDK gets this backstop and TIMEOUT_S is enforced on our side instead.
MIN_API_DEADLINE_MS = 10_000

# Debug lines (what is sent to the model and what it returns) are logged at DEBUG level.
# Turn them on with AI_COACH_DEBUG=1 (shell or server/.env), or --debug on the CLI below.


def _truncate(text: str, limit: int = DEBUG_MAX_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}... [+{len(text) - limit} chars]"

# What each issue means, in words the model can build on. Kept factual: tilt, not anatomy.
ISSUE_MEANING = {
    "forward_lean_increasing": "In the latest reps the upper back tilted further forward at the bottom than in the "
    "first reps, and the reps got slower. This often happens as people tire.",
    "excessive_forward_lean": "At the bottom of the latest reps the upper back tilted far forward.",
    "lateral_shift": "At the bottom of the latest reps the shoulders tilted to one side (see evidence.side).",
    "rounded_standing_posture": "Between reps, while standing, the upper back stayed tilted forward instead of upright.",
}

SYSTEM_PROMPT = """\
You write one short coaching cue for the warning banner of a squat-coaching phone app.

Rules:
- Use only the observation you are given. Do not add other problems or guess at causes.
- Say what the body did (tilting, leaning, shifting) and give one simple thing to try. Speak to the user \
as "you", friendly and calm.
- At most 25 words, one or two sentences. No numbers, no emojis, no links.
- The sensor measures how the upper back tilts, nothing else. Never describe the spine, injuries, pain, \
medical conditions, or safety risks, and never diagnose. Don't mention doctors or the sensor.
- If a side is given, name that side and only that side.
- Don't repeat the wording of the recent cues.

Reply with JSON only: {"message": "<the cue>"}"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "The cue shown to the user, at most 25 words."}},
    "required": ["message"],
}

_FORBIDDEN = re.compile(
    r"\b(spin(e|al)|injur\w*|diagnos\w*|scoliosis|kyphosis|lordosis|herniat\w*|disc|damag\w*|danger\w*|unsafe|"
    r"medical|doctor\w*|physician|pain\w*|sensor\w*)\b",
    re.IGNORECASE,
)


class LLM(Protocol):
    async def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        """Return the model's raw text reply (expected to be JSON matching ``schema``)."""
        ...


class MissingApiKey(RuntimeError):
    pass


class GeminiLLM:
    """Gemini Developer API via the official ``google-genai`` SDK (async client)."""

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise MissingApiKey("GEMINI_API_KEY is not set (server environment only, never the mobile app)")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("the AI coach needs the google-genai package: pip install 'google-genai>=3.0'") from exc
        if not hasattr(types, "ThinkingLevel"):  # older SDKs lack thinking_level; every call would fail
            raise RuntimeError(
                f"google-genai {getattr(genai, '__version__', '?')} is too old; pip install -U 'google-genai>=3.0'"
            )

        self._types = types
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        # One attempt only: a retried cue would arrive too late to be useful, and the caller falls back anyway.
        # The real time limit is AICoach.timeout_s (asyncio.wait_for); this deadline is only a backstop.
        self.http_timeout_ms = max(int(timeout_s * 1000), MIN_API_DEADLINE_MS)
        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=self.http_timeout_ms, retry_options=types.HttpRetryOptions(attempts=1)),
        )

    async def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        t = self._types
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=t.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
                thinking_config=t.ThinkingConfig(thinking_level=t.ThinkingLevel.LOW),  # a short cue needs little reasoning
            ),
        )
        return response.text or ""


# ------------------------------------------------------------------ prompt + validation


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)


class InvalidReply(ValueError):
    pass


def build_prompt(decision: PostureDecision, recent_cues: list[str], exercise: str = "squat") -> str:
    return json.dumps(
        {
            "exercise": exercise,
            "issue": decision.issue_code,
            "what_it_means": ISSUE_MEANING.get(decision.issue_code, decision.issue_code),
            "evidence": decision.evidence,
            "default_cue": decision.message,
            "recent_cues": recent_cues,
        },
        indent=2,
    )


def validate_reply(raw: str, decision: PostureDecision) -> str:
    """The cleaned message, or InvalidReply. Anything doubtful is rejected: the template is always a fine answer."""
    try:
        message = " ".join(_Reply.model_validate_json(raw).message.split())
    except ValidationError as exc:
        raise InvalidReply(f"not the expected JSON: {exc.error_count()} error(s)") from exc
    if not message:
        raise InvalidReply("empty message")
    if len(message) > MAX_MESSAGE_CHARS:
        raise InvalidReply(f"too long ({len(message)} chars)")
    if len(message.split()) > 30:
        raise InvalidReply("too many words")
    if re.search(r"\d", message):
        raise InvalidReply("contains numbers")  # raw angles are uncalibrated; don't show them to the user
    if re.search(r"https?://|www\.", message, re.IGNORECASE):
        raise InvalidReply("contains a link")
    if hit := _FORBIDDEN.search(message):
        raise InvalidReply(f"forbidden term {hit.group(0)!r}")
    side = decision.evidence.get("side")
    if side in ("left", "right"):
        other = "left" if side == "right" else "right"
        words = set(re.findall(r"[a-z]+", message.lower()))
        if side not in words or other in words:
            raise InvalidReply(f"must name the {side} side only")
    return message


# ------------------------------------------------------------------ the coach


class AICoach:
    """Rephrases rule decisions with an LLM, falling back to the rule's own message on any problem.

    ``phrase`` is async and bounded by ``timeout_s``. In the live server run it in a background task
    (``asyncio.create_task``) so a slow model can never hold up rep counting.
    """

    def __init__(self, llm: LLM | None, *, timeout_s: float = TIMEOUT_S, exercise: str = "squat") -> None:
        self.llm = llm
        self.timeout_s = timeout_s
        self.exercise = exercise
        self.recent_cues: deque[str] = deque(maxlen=3)
        self.agent_messages = 0
        self.fallbacks = 0
        self.last_fallback_reason: str | None = None
        if os.environ.get("AI_COACH_DEBUG") == "1":  # read here, after server/.env has been loaded
            logger.setLevel(logging.DEBUG)

    async def phrase(self, decision: PostureDecision) -> PostureDecision:
        """The decision with Gemini's wording (``source="agent"``) or unchanged (``source="rules"``).

        Only ``message`` and ``source`` can change: rep number, issue code, evidence and event id never do.
        """
        if self.llm is None:
            return self._fallback(decision, "no LLM configured")
        prompt = build_prompt(decision, list(self.recent_cues), self.exercise)
        model = getattr(self.llm, "model", type(self.llm).__name__)
        logger.debug(
            "-> %s request (rep %s, %s; system prompt %d chars not shown):\n%s",
            model,
            decision.rep_number,
            decision.issue_code,
            len(SYSTEM_PROMPT),
            _truncate(prompt),
        )
        try:
            start = time.perf_counter()
            raw = await asyncio.wait_for(self.llm.generate(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA), self.timeout_s)
            logger.debug("<- %s reply in %.0f ms:\n%s", model, (time.perf_counter() - start) * 1000, _truncate(raw))
            message = validate_reply(raw, decision)
        except asyncio.TimeoutError:
            return self._fallback(decision, f"timed out after {self.timeout_s:g} s")
        except InvalidReply as exc:
            return self._fallback(decision, f"invalid reply: {exc}")
        except Exception as exc:  # provider/network errors: never let them reach the workout
            return self._fallback(decision, f"{type(exc).__name__}: {_truncate(str(exc), 300)}")
        self.agent_messages += 1
        self.recent_cues.append(message)
        return dataclasses.replace(decision, message=message, source="agent")

    def _fallback(self, decision: PostureDecision, reason: str) -> PostureDecision:
        self.fallbacks += 1
        self.last_fallback_reason = reason
        logger.warning("AI coach fell back to the rule message (%s) issue=%s", reason, decision.issue_code)
        self.recent_cues.append(decision.message)
        return decision


class FakeLLM:
    """Offline stand-in: wraps the template so the full flow can be demonstrated without an API key."""

    async def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        default = json.loads(prompt)["default_cue"]
        return json.dumps({"message": f"Coach: {default}"})


if __name__ == "__main__":  # pragma: no cover - manual tool
    import argparse
    from pathlib import Path

    from app.services.posture import PostureConfig, analyze

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(description="Replay a sensor_update .jsonl file and phrase each warning with Gemini.")
    p.add_argument("path", type=Path)
    p.add_argument("--fake", action="store_true", help="use an offline fake instead of calling Gemini")
    p.add_argument("--model", default=None, help=f"default: $GEMINI_MODEL or {DEFAULT_MODEL}")
    p.add_argument("--mounting-offset", type=float, default=0.0)
    p.add_argument("--debug", action="store_true", help="print what is sent to the model and what it returns")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    if args.debug:
        logger.setLevel(logging.DEBUG)

    msgs = [json.loads(line) for line in args.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions = analyze(msgs, PostureConfig(mounting_offset_deg=args.mounting_offset)).decisions
    llm: LLM = FakeLLM() if args.fake else GeminiLLM(model=args.model)
    coach = AICoach(llm)
    print(f"{len(decisions)} warning(s) from {args.path.name} (SIMULATED data); model: {getattr(llm, 'model', 'fake')}")

    async def run() -> None:
        for d in decisions:
            start = time.perf_counter()
            out = await coach.phrase(d)
            ms = (time.perf_counter() - start) * 1000
            print(f"\nrep {d.rep_number} [{d.issue_code}]")
            print(f"  rules : {d.message}")
            note = "" if out.source == "agent" else f"  (fallback: {coach.last_fallback_reason})"
            print(f"  {out.source:6}: {out.message}  [{ms:.0f} ms]{note}")

    asyncio.run(run())
