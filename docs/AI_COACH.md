# The AI coach (LLM layer)

**Status: Gemini is the only provider (the Anthropic adapter was removed on 2026-11-07).** First live runs, 2026-11-07: `python -m scripts.check_coach_llm` passed (the recap was worded by Gemini; the camera cue came back `no_action`, see *Known issue* below), and `python -m scripts.check_posture_llm` got all four posture cues worded by Gemini, passing every check (`gemini-3.5-flash-lite` 0.7-2.1 s per cue; `gemini-3.8-flash` 3.8-5.3 s, close to the 6 s timeout). Everything else is verified offline: the guardrails, every failure path, the exact HTTP request the adapter sends (the real SDK against a fake HTTP server), and the behavior inside the live workout loop.

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

The guardrails and fallbacks live in `CoachAgent`, not in the vendor adapter. Models that
think before answering can take a few seconds, which is why a cue can arrive a moment after its `rep_completed`, and why
there is a timeout with a rule fallback. You can set `COACH_LLM_MODEL` to a cheaper or faster model if latency or cost
matters (your call).

The recap endpoint is `GET /api/workouts/{id}/coach-summary` (see `event-contracts.md`).

## Not enabled on purpose

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

## Posture cues (upper-back IMU)

Posture cues now go through this same layer: no second LLM client, no second set of rules.

* `services/posture.py` decides (deterministic): it finds squats in the IMU's tilt signal and flags
  `forward_lean_increasing`, `excessive_forward_lean`, `lateral_shift` or `rounded_standing_posture`, with the
  measured numbers as `evidence`.
* `websocket.py` (`_coach_posture`) feeds each `sensor_update` of a squat workout to a per-device `PostureCoach`, then
  hands a decision to the same `_spawn_agent_delivery` / `_deliver_coaching` path as camera cues.
* `build_posture_context` is what the model sees. Two extra rules apply only to posture cues, through the context:
  `numbers_allowed: false` (the tilt angles are uncalibrated, so a reply with any number falls back) and `side` (a
  lateral shift must name the measured side, and only that side). Spine words are banned for every cue.
* No posture cue within 6 s of any other cue (camera cues are never held back), and a pain report stops them.
* Details, thresholds and the simulated test data: `docs/posture-coach.md`.

## Known issue: the camera cue lists itself as already given

`RuleCoach.observe` adds the cue to `history` before `build_cue_context` runs, so the model sees the cue it is asked to
word under `feedback_already_given` and may treat it as a repeat (`no_action`). The live check's camera cue came back
`no_action`, which fits this (not proven). The posture path had the same problem and now excludes the cue being worded (live: 3 of 4 posture cues were
`no_action` before the fix, 0 of 4 after). The same fix for camera cues is a one-line change in `build_cue_context`
(skip the last history entry when it is the candidate's); not made yet, because it changes the rule coach's path.
