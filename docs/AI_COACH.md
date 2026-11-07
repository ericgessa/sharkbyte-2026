# The AI coach (LLM layer)

**Status: built and tested offline; NOT yet run against a real model.** No API key existed when this was written, so
nothing has been sent to a real API. Two providers are implemented: **Gemini** (the team's plan) and Anthropic's Claude.
What is verified for both: the guardrails, every failure path, the exact HTTP requests each adapter sends and how it reads
replies (the vendors' real SDKs run against a fake HTTP server), and the behavior inside the live workout loop. What is
not: that the real APIs accept these requests and that real model output passes the safety checks.
`python -m scripts.check_coach_llm` is the first live test (2 short requests, small cost); run it as soon as you have a
key, and treat a failure there as a bug in this layer.

It is **off by default**. With it off, the app behaves exactly as before: the rule-based coach gives the feedback.

## What it does (and does not)

```
 rep completed ──▶ rule engine decides: "this issue warrants a cue"   (deterministic; always the fallback)
                        │
                        ▼  only when COACH_LLM_PROVIDER is set
                  LLM, in the background:  send_feedback (its wording) | no_action | recommend_rest
                        │   schema-validated JSON, then CHECKED (see below)
                        ▼
        user sees the LLM's wording ("source":"agent")  OR the rule message ("source":"rules")
```

* The rules decide **that** and **which** (`reduced_depth`, `asymmetry`). The LLM only decides whether to say it and
  how to word it. It never counts reps, never invents an issue, never changes any stored number.
* **Event-triggered and bounded.** The LLM is called only when the rules produce a cue (at most 6 per workout, never
  every frame) and for end-of-workout recaps (cached, and limited to a few per minute per user).
* **Background.** Cues are worded in a background task: a slow or failing model never delays rep counting or the
  rep events (tested with the model "stuck" while 5 reps arrive).
* **Re-checked before sending.** If the user reported pain, finished the workout, or reconnected while the model was
  thinking, the late cue is dropped (or sent to the new connection). It never coaches after a pain report.
* **Pain is rules-only.** The stop-and-see-a-professional message and the recap after a pain report never involve
  the LLM.
* **Only measurements reach the model.** Its input is a few server-computed numbers and fixed strings (exercise, last 6
  reps, the detected issue, cues already given). No text typed by the user or sent by the client is in the prompt, so
  there is nothing to inject into. The prompt also tells it to ignore instructions inside the data.

### Checks every LLM answer must pass, or the rule message is used instead

| Check | Why |
| --- | --- |
| action is one of `send_feedback` / `no_action` / `recommend_rest` (schema-constrained) | it chooses only among approved actions |
| message non-empty, at most 200 characters (recap: 700) | short cues |
| no medical language (injury, pain, diagnosis, doctor, risk, dangerous, ...) | no medical advice (AGENTS.md) |
| no links, markup or JSON in the text | nothing clickable or injectable |
| every number in the text appears in the data it was given | no invented numbers |
| `recommend_rest` only after 5+ reps | no early "you're tired" |

Fallback also happens on: no credentials, timeout (default 6 s), API error or rate limit, refusal, truncated or invalid
JSON, any schema violation. A failed call is not retried (the rule message is the fast fallback). A misconfiguration
(no key, bad model id) logs one clear warning and every user simply gets rule messages.

## Turn it on

In `server/.env` (backend only; never in the mobile app). Get a Gemini key from Google AI Studio.

```
COACH_LLM_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-key
# optional:
COACH_LLM_MODEL=gemini-3.5-flash-lite
COACH_LLM_TIMEOUT_S=6
```

Restart the server, then run `python -m scripts.check_coach_llm`.

* **Gemini** (`COACH_LLM_PROVIDER=gemini`, alias `google`): key `GEMINI_API_KEY` (the SDK also accepts `GOOGLE_API_KEY`; if both
  are set it prefers `GOOGLE_API_KEY`). Default model `gemini-3.5-flash-lite`, which Google's model list shows as stable,
  fast and low cost. Model names change often (older 2.5 models are being retired), so if the live check reports
  `NotFoundError`, set `COACH_LLM_MODEL` to a current id from Google's model list.
* **Anthropic** (`COACH_LLM_PROVIDER=anthropic`): key `ANTHROPIC_API_KEY`, default `claude-opus-5-5`.

The guardrails and fallbacks are identical for both (they live in `CoachAgent`, not the vendor adapters). Models that
think before answering can take a few seconds, which is why a cue can arrive a moment after its `rep_completed`, and why
there is a timeout with a rule fallback. You can set `COACH_LLM_MODEL` to a cheaper or faster model if latency or cost
matters (your call).

The recap endpoint is `GET /api/workouts/{id}/coach-summary` (see `event-contracts.md`).

## Not enabled on purpose

* **Vendor-side refusal fallbacks** (Anthropic's server-side `fallbacks` option, which re-runs a declined request on
  another model) are not used. This layer already has its own fallback for a refusal: the deterministic rule message, with no second
  model call and no surprise cost. Tell me if you want the extra model hop as well.
* **Gemini's newer "Interactions" API** is not used. The adapter uses the SDK's established `generate_content` call (still
  shipped in `google-genai`), because its request shape could be verified offline. Google's current docs lead with the
  Interactions API; switching later means changing only `GeminiProvider._ask`.
* **No streaming, no tool loop.** A cue is one short structured answer.

## Plugging in a different LLM vendor

Subclass `_StructuredProvider` in `app/services/agent.py` and implement one method, `_ask(system, schema, payload)`, which
calls the vendor and returns the parsed JSON object (see `GeminiProvider`, ~50 lines), then add it to `agent_from_env()`. Every guardrail, the
fallback, the timeout and the background delivery are in `CoachAgent` and apply to any provider.

## Limits

* Only squat cues are ever worded by the model. Push-ups are analyzed too, but the model's context and wording are
  knee-specific, so push-up cues and recaps use the rule text (`source: "rules"`). Extend `build_cue_context` and the
  prompts per joint before turning the model on for other exercises.
* The wording and the checks have only been exercised with scripted fake answers. Real output may trip the strict
  checks (for example by citing a number that is not in the data); that safely falls back to the rule message, and
  `agent.stats` / `fallback_reasons` show how often.
* Cost scales with workouts, not frames: a few short requests per workout plus occasional recaps.

## Two coach modules (as of the merge with `main`)

There are currently two separate, independent LLM coach implementations in `server/app/services/`:

- `agent.py` (this document): rewords the **rule coach's rep cues** and writes **workout recaps**. It is wired into
  the live WebSocket and REST endpoints, off by default, with Anthropic and Gemini providers.
- `ai_coach.py` + `posture.py` (added on `main`, see their docstrings): turn **sensor-based posture decisions** into a
  short Gemini-worded warning. Standalone: **nothing in the app imports them yet**, so they are not part of the live
  workout flow. They are tested with a fake model (`tests/test_ai_coach.py`, `tests/test_posture.py`) and never run
  against a real key.

They do not share code. When the posture warnings are wired in, route them through the same guardrails as `agent.py`
(length, no medical advice, numbers must come from the measured context, pain stops coaching) rather than keeping two
sets of rules.
