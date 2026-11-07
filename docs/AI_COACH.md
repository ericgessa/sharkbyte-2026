# The AI coach (LLM layer)

**Status: built and tested offline; NOT yet run against a real model.** No Anthropic credential existed when this was
written, so nothing has been sent to the real API. What is verified: the guardrails, every failure path, the exact HTTP
requests the adapter sends and how it reads replies (against a fake transport), and the behavior inside the live workout
loop. What is not: that the real API accepts these requests and that real model output passes the safety checks.
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

In `server/.env` (backend only; never in the mobile app):

```
COACH_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-key
# optional:
COACH_LLM_MODEL=claude-opus-5-5
COACH_LLM_TIMEOUT_S=6
```

Restart the server, then run `python -m scripts.check_coach_llm`. The default model is `claude-opus-5-5`; you can set
`COACH_LLM_MODEL` to a cheaper or faster model if latency or cost matters (that is your call; the checks above apply
to any model). Opus-class models always think before answering, so a cue may take a few seconds: that is why a cue can
arrive a moment after its `rep_completed`, and why there is a timeout with a rule fallback.

The recap endpoint is `GET /api/workouts/{id}/coach-summary` (see `event-contracts.md`).

## Not enabled on purpose

* **Server-side refusal fallbacks** (Anthropic's `fallbacks` option, which re-runs a declined request on another model)
  are not used. This layer already has its own fallback for a refusal: the deterministic rule message, with no second
  model call and no surprise cost. Tell me if you want the extra model hop as well.
* **No streaming, no tool loop.** A cue is one short structured answer.

## Plugging in a different LLM vendor

Implement `CoachProvider` in `app/services/agent.py` (`decide`, `summarize`, `aclose`: each takes the same context
dict and returns the same validated `AgentOutput` / text) and add it to `agent_from_env()`. Every guardrail, the
fallback, the timeout and the background delivery are in `CoachAgent` and apply to any provider.

## Limits

* Only squat produces cues today (it is the only analyzed exercise), so only squat cues are ever worded.
* The wording and the checks have only been exercised with scripted fake answers. Real output may trip the strict
  checks (for example by citing a number that is not in the data); that safely falls back to the rule message, and
  `agent.stats` / `fallback_reasons` show how often.
* Cost scales with workouts, not frames: a few short requests per workout plus occasional recaps.
