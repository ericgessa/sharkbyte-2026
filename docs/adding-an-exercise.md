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

1. Write a deterministic counter next to `SquatRepCounter` (`services/rep_counter.py`). It needs the same
   interface: `update(PoseSample) -> StepResult`, `reset_tracking()`, `rep_count`, `dropped_frames`,
   `aborted_attempts`. (`PoseSample` currently carries knee angles only; extend it and the `PoseData` field
   mapping for elbow/hip-driven exercises.)
2. Add coaching rules for it in `services/coaching.py` (the current rules read knee depth and left/right difference).
3. Set `counter=lambda saved: YourCounter(rep_count=saved)` on its registry entry. `analysis_supported` flips to
   `true` automatically (it is derived from `counter`, not a separate flag that can drift).
4. Add tests like `tests/test_rep_counter.py` (thresholds, bad frames, duplicates) and a mutation check that the
   counter is really used.
5. Tiger Data analytics (`analytics.py`) default to knee angles; pass the exercise's `primary_angles` as `metrics`
   for others.

Until step 3, the API will not claim the exercise can be analyzed.
