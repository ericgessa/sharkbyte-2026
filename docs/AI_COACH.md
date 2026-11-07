# The AI coach: Gemini as the end-of-workout reporter

**Team decision, 2026-11-07: everything during a workout is deterministic; Gemini is only the reporter.** Counting,
rejecting attempts (`rep_rejected`), camera cues, posture cues and back-shape cues are all decided and worded by code,
and arrive instantly. After the user finishes, Gemini writes a short report from those deterministic results:
what went well, what came up, and one thing to focus on next time.

**Status.** Gemini is the only provider (the Anthropic adapter was removed on 2026-11-07). Live check on 2026-11-07
with `gemini-3.8-flash` (`python -m scripts.check_report_llm`, three simulated finished workouts): every report was
written by Gemini and passed every check, in 7-10 s each. Everything else is verified offline: the guardrails, every
failure path, the exact HTTP request the adapter sends (the real SDK against a fake HTTP server).

It is **off by default**. With it off, the report is the deterministic one (`source: "rules"`), and nothing else changes.

## What it does (and does not)

```
 workout (all deterministic): reps, rep_rejected, coaching cues ──▶ saved to Supabase as reps + coaching_events
                                                                          │
 user presses Finish ──▶ POST /finish ──▶ GET /coach-summary ──▶ report context (stats + issues + focus, by code)
                                                                          │
                                       Gemini words it ("source":"agent")  OR the rules report ("source":"rules")
```

* **Code decides the content.** `report_context` adds, from the saved coaching events: each issue that came up (most
  frequent first) with a plain label, its meaning and what its count counts (warnings given, or attempts not
  counted), plus `suggested_focus`: one concrete thing to work on, chosen by code from the most frequent issue.
  Gemini only words it. `rules_summary` is the same content without the model, and is always the fallback.
* **One call per finished workout,** cached once the workout is completed, limited to a few per minute per user, with a
  20 s budget (twice `COACH_LLM_TIMEOUT_S`). The app shows the stats from `finish` first and fills the report in when
  it arrives (`docs/frontend-integration.md`).
* **Pain is rules-only.** After a pain report the report is always the rules text (with the advice to talk to a
  qualified health professional), the model is not called, and no technique focus is given.
* **Only measurements reach the model.** Server-computed numbers and fixed strings: no text typed by the user or sent
  by the client is in the prompt, and the prompt tells it to ignore instructions inside the data.
* Exercises without rep counting get the deterministic "your workout was logged" text; the model is not called.

### Checks the report must pass, or the rules report is used instead

| Check | Why |
| --- | --- |
| non-empty, at most 700 characters | a short report |
| no medical language (injury, pain, diagnosis, doctor, risk, dangerous, ...) or spine words (spine, spinal, scoliosis, ...) | no medical advice (AGENTS.md); the wearable measures bending, not the spine's health |
| no links, markup or JSON in the text | nothing clickable or injectable |
| every number in the text appears in the data it was given | no invented numbers |

Fallback also happens on: no credentials, timeout, API error or rate limit, refusal, truncated or invalid JSON, any
schema violation. A failed call is not retried. A misconfiguration (no key, bad model id) logs one clear warning, and
every user simply gets the rules report.

Found on the live run and fixed: the model first read "2 warnings" as "2 reps had the issue". Each issue now says what
its count counts, and the prompt says a warning count is not a number of reps.

## Turn it on

In `server/.env` (backend only; never in the mobile app). Get a Gemini key from Google AI Studio.

```
COACH_LLM_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-key
# optional:
COACH_LLM_MODEL=gemini-3.8-flash
COACH_LLM_TIMEOUT_S=10
```

Restart the server, then run `python -m scripts.check_report_llm` (add `--debug` to see what is sent and returned).

* **Gemini** (`COACH_LLM_PROVIDER=gemini`, alias `google`): key `GEMINI_API_KEY` (the SDK also accepts `GOOGLE_API_KEY`; if both
  are set it prefers `GOOGLE_API_KEY`). Default model `gemini-3.8-flash` (the team's choice; 7-10 s per report live), with `COACH_LLM_TIMEOUT_S=10`
  (reports get twice that). `gemini-3.5-flash-lite` is several times faster if speed matters more. Model names change often (older 2.5 models are being retired), so if the live check reports
  `NotFoundError`, set `COACH_LLM_MODEL` to a current id from Google's model list.

The guardrails and fallbacks live in `CoachAgent`, not in the vendor adapter. `COACH_LLM_DEBUG=1` logs every request
sent to the model and its reply (cut at 800 characters; never the key).

The recap endpoint is `GET /api/workouts/{id}/coach-summary` (see `event-contracts.md`).

## Not enabled on purpose

* **Gemini's newer "Interactions" API** is not used. The adapter uses the SDK's established `generate_content` call (still
  shipped in `google-genai`), because its request shape could be verified offline. Google's current docs lead with the
  Interactions API; switching later means changing only `GeminiProvider._ask`.
* **No streaming, no tool loop.** A report is one short structured answer.

## Plugging in a different LLM vendor

Subclass `_StructuredProvider` in `app/services/agent.py` and implement one method, `_ask(system, schema, payload)`, which
calls the vendor and returns the parsed JSON object (see `GeminiProvider`, ~50 lines), then add it to `agent_from_env()`. Every guardrail, the
fallback and the timeout are in `CoachAgent` and apply to any provider.

## Limits

* Reports have been checked against the real API on simulated workouts only, not real ones yet.
* Cost scales with finished workouts: one short request each (cached afterwards).

## Live cue wording: kept but unused

`CoachAgent.refine`, `build_cue_context` and `CUE_SYSTEM` (the model rewording camera cues during a set) and
`scripts/check_coach_llm.py`'s cue check are no longer called by the server. They are kept, with their tests, until the
team confirms the reporter-only decision; then they can be deleted.
