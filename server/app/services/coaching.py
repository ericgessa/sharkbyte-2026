"""Rule-based coach: turns completed-rep metrics into rare, short feedback.

Deterministic and dependency-free, so it is testable and is the safe fallback
under any future LLM coach (AGENTS.md: "handle provider errors with a safe
rule-based fallback"). It only describes what the camera measured, relative to
the user's OWN early reps; it never diagnoses or calls a movement unsafe.

The thresholds are untuned starting points, not validated biomechanics. Tune
them against real recordings before trusting them.

Rate limiting keeps it from nagging: no feedback before a baseline exists, a
minimum gap between any two messages, a longer cooldown per issue, and a cap
per session.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.services.rep_counter import RepMetrics

ACTION_SEND_FEEDBACK = "send_feedback"

MESSAGES = {
    "reduced_depth": "Your depth has decreased compared with earlier reps. Try your previous comfortable range.",
    "asymmetry": (
        "Your left and right knees are bending by different amounts. "
        "Try to move both sides evenly, and check that your camera angle is straight on."
    ),
}


def messages_for(joint: str) -> dict[str, str]:
    """The cue texts for the joint being counted ("knee" is the default MESSAGES; elbows swap the body part)."""
    if joint == "knee":
        return MESSAGES
    return {code: text.replace("knees", f"{joint}s") for code, text in MESSAGES.items()}


@dataclass(frozen=True)
class CoachConfig:
    baseline_reps: int = 3  # early reps that define "your normal depth"
    depth_loss_deg: float = 10.0  # joint angle this much higher than baseline = shallower
    asymmetry_deg: float = 15.0  # left/right difference worth mentioning
    consecutive: int = 2  # the issue must show in this many reps in a row
    min_reps_between_messages: int = 2
    same_issue_cooldown_reps: int = 4
    max_messages: int = 6


@dataclass(frozen=True)
class CoachingDecision:
    rep_number: int
    issue_code: str
    message: str
    action: str = ACTION_SEND_FEEDBACK
    source: str = "rules"
    # Generated once so a retried save is idempotent (upsert on id).
    event_id: UUID = field(default_factory=uuid4)


PAIN_ISSUE_CODE = "pain_reported"
ACTION_RECOMMEND_REST = "recommend_rest"
PAIN_MESSAGE = (
    "Stop the exercise now. If the pain continues or worries you, "
    "please talk to a qualified health professional before exercising again."
)


class RuleCoach:
    def __init__(self, config: CoachConfig | None = None, joint: str = "knee") -> None:
        self.config = config or CoachConfig()
        self.joint = joint
        self._messages = messages_for(joint)
        # False after the user reports pain: no more technique coaching this session.
        self.enabled = True
        self.reps: list[RepMetrics] = []
        self.history: list[tuple[int, str]] = []  # (rep_number, issue_code) of cues given
        self.messages_sent = 0
        self._last_message_rep: int | None = None
        self._last_rep_by_issue: dict[str, int] = {}

    def observe(self, rep: RepMetrics) -> CoachingDecision | None:
        """Record a completed rep; return feedback to give now, or None."""
        self.reps.append(rep)
        if not self.enabled:
            return None
        for issue in self._detected_issues():  # priority order
            if self._allowed(issue, rep.rep_number):
                self.messages_sent += 1
                self._last_message_rep = rep.rep_number
                self._last_rep_by_issue[issue] = rep.rep_number
                self.history.append((rep.rep_number, issue))
                return CoachingDecision(rep.rep_number, issue, self._messages[issue])
        return None

    # ------------------------------------------------------------------ internals

    def _detected_issues(self) -> list[str]:
        cfg = self.config
        recent = self.reps[-cfg.consecutive :]
        if len(recent) < cfg.consecutive:
            return []
        found: list[str] = []

        if len(self.reps) > cfg.baseline_reps:  # need a baseline plus at least one more rep
            baseline = statistics.median(r.min_angle_deg for r in self.reps[: cfg.baseline_reps])
            # The recent reps must all be after the baseline window.
            if all(r.rep_number > cfg.baseline_reps for r in recent) and all(
                r.min_angle_deg - baseline >= cfg.depth_loss_deg for r in recent
            ):
                found.append("reduced_depth")

        if all(r.max_asymmetry_deg is not None and r.max_asymmetry_deg >= cfg.asymmetry_deg for r in recent):
            found.append("asymmetry")
        return found

    def _allowed(self, issue: str, rep_number: int) -> bool:
        cfg = self.config
        if self.messages_sent >= cfg.max_messages:
            return False
        if self._last_message_rep is not None and rep_number - self._last_message_rep < cfg.min_reps_between_messages:
            return False
        last_same = self._last_rep_by_issue.get(issue)
        return last_same is None or rep_number - last_same >= cfg.same_issue_cooldown_reps
