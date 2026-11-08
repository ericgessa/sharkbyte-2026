# Form IQ

Form IQ decides, for every attempt, whether it counts and how the back moved during it. All of it is deterministic:
no LLM is involved during a workout. Gemini only mentions the results in the end-of-workout report
([AI_COACH.md](AI_COACH.md)).

**Status (2026-11-07):** built and tested on SIMULATED data. All thresholds are untuned starting values: they need
labelled recordings from the real camera and the spine wearable before anyone relies on them.

## Who decides what

| Question | Decided by | Signal |
| --- | --- | --- |
| Where is the user in the rep (top, going down, bottom, coming up)? | Camera rep counter, `server/app/services/rep_counter.py` | Knee angle (squat) or elbow angle (push-up), from MediaPipe in the app |
| Does this attempt count? | Same counter | Reached the bottom threshold, and took at least the minimum rep time |
| Why didn't it count? | Same counter (`RejectReason`) | `partial_range` or `too_fast` |
| How did the back move during a counted rep? | `server/app/services/spine_form.py` | The wearable's section angles inside that rep's time window |

The camera decides phases and counting because the spine sensors measure back **shape**, not position: each section
angle compares two neighbouring sensors, so it reads the same standing up or in a plank. That is exactly what makes
them good for form, and why they can't tell the top of a push-up from the bottom.

## Attempts that don't count

The counter already refused partial and too-fast attempts; it now says why, and the app is told:

* `partial_range`: the attempt went at least **halfway** from leaving the top to the bottom threshold, but never
  reached the bottom. Smaller dips (shifting weight, camera jitter) are ignored silently, as before.
* `too_fast`: it reached the bottom, but the whole attempt took less than the exercise's minimum rep time
  (800 ms squat, 600 ms push-up).
* **One attempt per descent.** An attempt only ends once the user has held the top for 250 ms. Wobbling around the
  top threshold, or dipping straight back down, is still the same attempt (and still becomes a rep if it then reaches
  the bottom), so one movement is never reported more than once.

## Glitch guard

Pose tracking sometimes returns nonsense for a few frames. Seen on a real push-up set: one elbow at 18° while the
other read 174°, and 60° jumps that reverse within one frame. Before this guard, that noise counted 3 extra push-ups
within 2 seconds and produced about 14 bogus "didn't count" messages. Before any angle reaches the counter:

1. A side below 30° is dropped: no knee or elbow in these exercises reads that.
2. If left and right differ by more than 40°, one side is mistracked: the counter keeps the side closer to the
   recent angle. Those frames also don't count as left/right asymmetry.
3. The counter runs on the **median of the last 5 usable frames**, so a spike of 1-2 frames can never cross a
   threshold or set the recorded depth.
4. Any attempt shorter than 300 ms is a glitch: never counted and never reported.

On the real push-up set above, this took the result from 6 reps and 20 rejections to 3 reps and 6 rejections.
All values are in `SquatConfig` (`min_plausible_deg`, `max_side_gap_deg`, `smoothing_frames`, `top_hold_ms`,
`glitch_ms`) and are untuned.

The server sends `rep_rejected` (reason, message, deepest angle, joint, duration; `rep_count` unchanged) and saves it
to `coaching_events` with `action: "rep_rejected"`, so it shows in the workout summary and the report. Format:
[event-contracts.md](event-contracts.md).

Rejecting reps is limited to range of motion on purpose: it is objective. Back shape only warns until its thresholds
are tuned, because wrongly rejecting a good rep is the fastest way to lose a user's trust in the count.

## Back shape per counted rep

`SpineFormCoach` buffers the wearable's `spine_upper_deg` / `spine_mid_deg` / `spine_lumbar_deg` on server time. When the
camera completes a rep, it looks at the samples inside that rep's window:

| Exercise | Issue | Rule (untuned) |
| --- | --- | --- |
| Push-up | `hips_sagging` | `lumbar` at or below −8° for at least half the rep (lower back arching) |
| Push-up | `hips_piking` | `lumbar` at or above +10° for at least half the rep (lower back flexing) |
| Squat | `lower_back_rounding` | `lumbar` at or above +15° for at least 30% of the rep |

* A rep with fewer than 5 samples in its window (wearable off, a gap) is **not judged**, rather than guessed.
* A warning needs the issue in 2 judged reps in a row, then the usual limits: 2 reps between warnings, 4 before the
  same one repeats, 6 per workout. At most one cue per rep: a camera cue for the same rep goes first.
* After a pain report reps are still judged and saved, never voiced.
* Every counted rep stores its back-shape record in `repetitions.metrics.spine` (judged, sample count, issues, lumbar
  min/max/mean, mid and upper max).

**Depends on the wearable's schema.** The section-angle fields are added by `Hardware_branch`
(`server/app/schemas/sensors.py`). Until that branch is merged the server accepts no section angles, so this part
does nothing in a live workout (the code reads the fields only if they exist). The logic is fully tested on its own.

## Squat posture from the upper back (`posture.py`)

The earlier single-IMU posture rules still run on `imu_pitch_deg` (the wearable's `torso`): `excessive_forward_lean`,
`forward_lean_increasing`, `rounded_standing_posture`. `lateral_shift` is **off by default**
(`PostureConfig.lateral_shift_enabled`) until the wearable's roll sign is checked, so it can never name the wrong side.
See [posture-coach.md](posture-coach.md).

## Simulated data

`server/app/spine_simulation.py` generates section-angle samples and rep windows for `pushup_good`,
`pushup_hips_sagging`, `pushup_hips_piking`, `squat_good` and `squat_lower_back_rounding`. The shapes are provisional,
taken from the hardware README and one short push-up log; replace them with values from labelled recordings.

## Tests

`server/tests/test_form_iq.py`: rejected attempts at the counter and over the live socket, every spine scenario across
seeds, the two-in-a-row rule and rate limits, unjudged reps, pain, and how the report counts rejected attempts.

## What's needed next

1. Merge `Hardware_branch` (after it is updated from `main`), so section angles reach the server.
2. Labelled recordings: good push-ups and squats, plus deliberate sagging, piking, rounding and partial reps, filmed.
3. Tune every threshold above on those recordings, then decide with the team whether any back-shape issue should
   reject reps.
