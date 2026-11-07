# Adding an exercise (backend)

Exercises are defined in **one place**: the registry in `server/app/services/exercises.py`.

## 1. Make it available for logging (a few lines, no migration)

Add an entry to `_REGISTRY`:

```python
Exercise("overhead-press", "Overhead press", aliases=("shoulder press", "ohp"),
         primary_angles=("left_elbow_angle", "right_elbow_angle")),
```

That is enough for:

* request validation and name matching (`"Overhead Press"`, `"overheadpress"`, `"OHP"` all resolve to `overhead-press`);
* `GET /api/exercises` (the mobile app's picker updates itself);
* creating, streaming to, finishing and listing workouts of that exercise;
* the Supabase `exercises` row, which is **created automatically the first time someone uses it** (no SQL migration).

Rules the registry enforces at import time: no two exercises may claim the same spelling. Separators and case never
matter (`"Push Up"`, `"push-up"`, `"PUSH_UP"` are the same).

Update the list in `tests/test_exercises.py::REQUESTED` and the supported list in `docs/event-contracts.md`.

## 2. Make it *analyzed* (rep counting + coaching): real work, only when it exists

Leave `counter` unset until you have actually built and tested the analysis. With no counter the exercise reports
`analysis_supported: false`, the server never counts reps for it, and the app tells the user tracking isn't
available. To turn analysis on:

1. For an "angle goes down, then comes back up" movement you usually need no new code: subclass the config
   (`PushUpConfig` in `services/rep_counter.py` is the example: `joint="elbow"` plus its own thresholds) and the
   counter (`PushUpRepCounter`). `joint` is `"knee"` or `"elbow"`; for another joint (hip, shoulder) extend
   `PoseSample.pair` and the `PoseSample(...)` mapping in `api/websocket.py`, and the `joint` `Literal` in
   `schemas/events.py`. A movement that is not a down-and-up cycle needs a new counter with the same interface:
   `update(PoseSample) -> StepResult`, `reset_tracking()`, `rep_count`, `dropped_frames`, `aborted_attempts`.
2. Coaching: `RuleCoach(joint=...)` already works for any joint (depth loss and left/right difference, with the
   body part swapped in the messages). New kinds of cue go in `services/coaching.py`. The optional LLM layer is
   knee-only (`Exercise.llm_supported`); other joints use the rule text until `build_cue_context` is extended.
3. Set `counter=lambda saved: YourCounter(rep_count=saved)` on its registry entry. `analysis_supported` flips to
   `true` automatically (it is derived from `counter`, not a separate flag that can drift).
4. Add tests like `tests/test_rep_counter.py` and `tests/test_pushup.py` (thresholds, bad frames, duplicates, the
   full WebSocket flow) and a mutation check that the counter is really used.
5. Tiger Data analytics (`analytics.py`) default to knee angles; pass the exercise's `primary_angles` as `metrics`
   for others.

Until step 3, the API will not claim the exercise can be analyzed.
