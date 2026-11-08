"""The exercise registry: the ONE place that defines which exercises exist.

To add an exercise, add an ``Exercise(...)`` entry to ``_REGISTRY`` below. Everything else follows:
request validation, name normalization ("Push Up" == "pushup" == "push-up"), the GET /api/exercises
listing, and the Supabase ``exercises`` row (created on first use; no migration needed).

Two different levels of support, kept deliberately separate:

  * LOGGING (every exercise): a workout session can be created, streamed to (camera/ESP32 readings are
    recorded in Tiger Data if configured), finished, and shown in history. The user can report pain.
  * ANALYSIS (``analysis=True``, only exercises that have a ``counter``): the server counts reps from the
    pose stream and the rule-based coach gives feedback. Today that is squat and push-up. For every other
    exercise the server does NOT count reps or coach; the API reports ``analysis_supported: false``
    so the app can say so. Do not set ``analysis=True`` until a real counter and rules exist.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.rep_counter import NoopRepCounter, PushUpRepCounter, SquatRepCounter

MAX_NAME_LENGTH = 64


class UnknownExerciseError(ValueError):
    """Raised for a name that matches no exercise. The message is safe to show to API users."""


def _key(name: str) -> str:
    """Normalize for matching: lower-case and drop everything but letters and digits.

    "Push Up", "push-up", "PUSH_UP" and "pushup" all become "pushup".
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass(frozen=True)
class Exercise:
    slug: str  # canonical name: stored in Supabase and used in the API. lowercase, hyphenated
    display_name: str
    aliases: tuple[str, ...] = ()  # other spellings people use ("pushups"); separators/case never matter
    # Joint angles worth capturing for this exercise (documentation / future analysis; stored when sent).
    primary_angles: tuple[str, ...] = ()
    # The joint whose angle the counter reads ("knee" or "elbow"). Only meaningful when there is a counter.
    joint: str = "knee"
    # Builds the rep counter. None = no analysis implemented: reps are NOT counted, no coaching.
    counter: Callable[[int], Any] | None = field(default=None, compare=False)

    @property
    def analysis_supported(self) -> bool:
        return self.counter is not None

    @property
    def llm_supported(self) -> bool:
        """The optional LLM coach layer only knows knee wording and context so far; other joints use the rule messages."""
        return self.analysis_supported and self.joint == "knee"

    def make_counter(self, saved_reps: int = 0) -> Any:
        return self.counter(saved_reps) if self.counter else NoopRepCounter()

    def db_config(self) -> dict[str, Any]:
        """The ``exercises.config`` JSON used when this exercise's row is first created in Supabase."""
        cfg: dict[str, Any] = {
            "display_name": self.display_name,
            "analysis_supported": self.analysis_supported,
            "primary_angles": list(self.primary_angles),
        }
        if self.counter is not None:  # detection thresholds (mirror the counter's config; see supabase migration 3)
            c = self.make_counter().config
            cfg.update(
                standing_deg=c.standing_deg, descent_start_deg=c.descent_start_deg, bottom_deg=c.bottom_deg,
                bottom_exit_deg=c.bottom_exit_deg, min_rep_ms=c.min_rep_ms,
            )
        return cfg


# ---------------------------------------------------------------------------------------------
# THE REGISTRY. Add new exercises here.
# ---------------------------------------------------------------------------------------------
_REGISTRY: tuple[Exercise, ...] = (
    Exercise(
        "squat", "Squat", aliases=("squats", "air squat", "bodyweight squat"),
        primary_angles=("left_knee_angle", "right_knee_angle"),
        counter=lambda saved: SquatRepCounter(rep_count=saved),
    ),
    Exercise(
        "push-up", "Push-up", aliases=("pushups", "press up"),
        primary_angles=("left_elbow_angle", "right_elbow_angle"),
        joint="elbow",
        counter=lambda saved: PushUpRepCounter(rep_count=saved),
    ),
    Exercise("pull-up", "Pull-up", aliases=("pullups", "chin up"), primary_angles=("left_elbow_angle", "right_elbow_angle")),
    Exercise("deadlift", "Deadlift", aliases=("deadlifts",), primary_angles=("left_hip_angle", "right_hip_angle", "left_knee_angle", "right_knee_angle")),
    Exercise("bench-press", "Bench press", aliases=("bench", "benchpresses"), primary_angles=("left_elbow_angle", "right_elbow_angle")),
    Exercise("lunge", "Lunge", aliases=("lunges",), primary_angles=("left_knee_angle", "right_knee_angle")),
    Exercise("plank", "Plank", aliases=("planks",), primary_angles=("left_hip_angle", "right_hip_angle")),
    # Logged only: the app counts jumping jacks on the phone from arm/leg positions (the server gets no shoulder angle).
    Exercise("jumping-jack", "Jumping jack", aliases=("jumping jacks", "star jump", "star jumps"), primary_angles=("left_hip_angle", "right_hip_angle")),
)

_BY_SLUG: dict[str, Exercise] = {e.slug: e for e in _REGISTRY}
_BY_KEY: dict[str, Exercise] = {}
for _e in _REGISTRY:
    for _spelling in (_e.slug, _e.display_name, *_e.aliases):
        _k = _key(_spelling)
        if _BY_KEY.setdefault(_k, _e) is not _e:  # two exercises claiming one spelling would be ambiguous
            raise RuntimeError(f"exercise registry: {_spelling!r} is claimed by more than one exercise")


def all_exercises() -> tuple[Exercise, ...]:
    return _REGISTRY


def supported_names() -> list[str]:
    return [e.slug for e in _REGISTRY]


def find(name: object) -> Exercise | None:
    """The exercise a user-supplied name refers to, or None."""
    if not isinstance(name, str):
        return None
    return _BY_KEY.get(_key(name[:MAX_NAME_LENGTH * 2]))


def resolve(name: object) -> Exercise:
    """Like ``find`` but raises UnknownExerciseError with a helpful, user-safe message."""
    if not isinstance(name, str):
        raise UnknownExerciseError(f"exercise must be text, one of: {', '.join(supported_names())}")
    shown = name.strip()[:MAX_NAME_LENGTH]
    if not shown:
        raise UnknownExerciseError(f"exercise must not be empty. Supported: {', '.join(supported_names())}")
    found = _BY_KEY.get(_key(name[: MAX_NAME_LENGTH * 2]))
    if found:
        return found
    close = difflib.get_close_matches(_key(shown), list(_BY_KEY), n=1, cutoff=0.7)
    hint = f" Did you mean '{_BY_KEY[close[0]].slug}'?" if close else ""
    raise UnknownExerciseError(f"Unsupported exercise '{shown}'.{hint} Supported: {', '.join(supported_names())}")


def for_stored_name(name: str) -> Exercise:
    """Exercise for a name read back from the database. A name no longer in the registry (e.g. a row from
    an older version) gets a logging-only placeholder, so history still loads and nothing is miscounted."""
    return find(name) or Exercise(slug=name, display_name=name)


DEFAULT_EXERCISE = "squat"
