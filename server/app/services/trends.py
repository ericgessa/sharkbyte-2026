"""Trends for one exercise across a user's sets: the facts shown as bubbles, and the one piece of advice.

Deterministic and pure (no I/O): the route loads the rows, this computes. Every number shown to the user comes from
here; the optional LLM only words a headline and the advice from these facts (agent.CoachAgent.trends), under the
same guardrails as the workout report. Descriptive, never medical: "back" wording, never anatomy claims.

Inputs are Supabase rows: sessions (``id, started_at, ended_at, status, total_reps``) and, per session, its
repetitions (``metrics`` JSON: min_angle_deg, duration_ms, max_asymmetry_deg, joint, and ``spine`` when the wearable
was on: lumbar_max_deg, issues...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any

MAX_SETS = 8  # how many recent sets are looked at in detail
MAX_FACTS = 7

SPINE_ADVICE = {
    "hips_sagging": "Keep your body in one straight line: squeeze your glutes so your hips don't drop.",
    "hips_piking": "Keep your body in one straight line: lower your hips a little.",
    "lower_back_rounding": "Keep your chest up and your back flat on the way down.",
}


@dataclass
class SetStats:
    session_id: str
    at: datetime
    reps: int
    duration_s: float | None
    depth_deg: float | None = None  # average of each rep's deepest angle (smaller = deeper)
    depth_spread_deg: float | None = None
    tempo_s: float | None = None
    balance_deg: float | None = None
    back_peak_deg: float | None = None  # average per-rep peak lower-back bend (wearable)
    back_issues: dict[str, int] = field(default_factory=dict)
    joint: str | None = None
    # Fatigue inside the set: last third vs first third of the reps (needs 6+ measured reps).
    slowdown_pct: int | None = None  # + = the last reps took longer
    depth_drop_deg: float | None = None  # + = the last reps were shallower (bigger lowest angle)
    # The set's rep wave, in rep order: d = deepest angle (smaller = deeper), t = rep duration in ms.
    bars: list[dict[str, Any]] = field(default_factory=list)


def _parse(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


MAX_BARS = 60
MIN_REPS_FOR_FATIGUE = 6


def _fatigue(metrics: list[dict[str, Any]]) -> tuple[int | None, float | None]:
    """(slowdown %, depth drop deg) between the first and last third of the reps, or None when not measurable."""
    durs = [m["duration_ms"] for m in metrics if isinstance(m.get("duration_ms"), (int, float))]
    deps = [m["min_angle_deg"] for m in metrics if isinstance(m.get("min_angle_deg"), (int, float))]
    slow = drop = None
    if len(durs) >= MIN_REPS_FOR_FATIGUE:
        k = len(durs) // 3
        first, last = mean(durs[:k]), mean(durs[-k:])
        slow = round((last - first) / first * 100) if first > 0 else None
    if len(deps) >= MIN_REPS_FOR_FATIGUE:
        k = len(deps) // 3
        drop = round(mean(deps[-k:]) - mean(deps[:k]), 1)
    return slow, drop


def set_stats(row: dict[str, Any], reps: list[dict[str, Any]]) -> SetStats:
    started, ended = _parse(row.get("started_at")), _parse(row.get("ended_at"))
    reps = sorted(reps, key=lambda r: r.get("rep_number") or 0)
    metrics = [r.get("metrics") or {} for r in reps]
    slowdown, depth_drop = _fatigue(metrics)
    bars = [
        {
            "d": m["min_angle_deg"] if isinstance(m.get("min_angle_deg"), (int, float)) else None,
            "t": m["duration_ms"] if isinstance(m.get("duration_ms"), (int, float)) else None,
        }
        for m in metrics[:MAX_BARS]
    ]
    depths = [m["min_angle_deg"] for m in metrics if isinstance(m.get("min_angle_deg"), (int, float))]
    tempos = [m["duration_ms"] / 1000 for m in metrics if isinstance(m.get("duration_ms"), (int, float))]
    asym = [m["max_asymmetry_deg"] for m in metrics if isinstance(m.get("max_asymmetry_deg"), (int, float))]
    spine = [m["spine"] for m in metrics if isinstance(m.get("spine"), dict)]
    back = [s["lumbar_max_deg"] for s in spine if isinstance(s.get("lumbar_max_deg"), (int, float))]
    issues: dict[str, int] = {}
    for s in spine:
        for i in s.get("issues") or []:
            issues[i] = issues.get(i, 0) + 1
    joint = next((m.get("joint") for m in metrics if m.get("joint")), None)
    return SetStats(
        session_id=str(row["id"]),
        at=started or datetime.min,
        reps=int(row.get("total_reps") or 0),
        duration_s=(ended - started).total_seconds() if started and ended else None,
        depth_deg=round(mean(depths), 1) if depths else None,
        depth_spread_deg=round(pstdev(depths), 1) if len(depths) >= 3 else None,
        tempo_s=round(mean(tempos), 1) if tempos else None,
        balance_deg=round(mean(asym), 1) if asym else None,
        back_peak_deg=round(mean(back), 1) if back else None,
        back_issues=issues,
        joint=joint,
        slowdown_pct=slowdown,
        depth_drop_deg=depth_drop,
        bars=bars,
    )


def _streak_days(days: list[date]) -> int:
    """Consecutive days with a set, ending on the most recent set's day."""
    unique = sorted(set(days), reverse=True)
    if not unique:
        return 0
    n = 1
    for a, b in zip(unique, unique[1:]):
        if a - b == timedelta(days=1):
            n += 1
        else:
            break
    return n


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _avg(xs: list[float]) -> float | None:
    return round(mean(xs), 1) if xs else None


def build_trends(label: str, sets: list[SetStats], all_time_reps: int, all_time_sets: int, focus_id: str | None = None) -> dict[str, Any]:
    """``sets`` newest first (completed only). ``focus_id``: the set just finished (results screen); default = newest.

    Returns {facts: [{key, big, small, tone}], advice, advice_key, context} where context is the bounded, numbers-only
    payload an LLM may word (and whose numbers its text is checked against)."""
    noun = label.lower()
    if not sets:
        return {
            "facts": [{"key": "start", "big": "0", "small": f"sets of {noun} so far: your first one starts the story", "tone": "calm"}],
            "advice": f"Do your first set of {noun} and your trends start here.",
            "advice_key": "first_set",
            "context": {"exercise": label, "sets": 0},
        }

    focus = next((s for s in sets if s.session_id == focus_id), sets[0])
    others = [s for s in sets if s is not focus]
    previous = next((s for s in sets if s.at < focus.at), None)
    best = max(s.reps for s in sets)
    facts: list[dict[str, str]] = []

    # 1. this set vs the one before
    if previous is None:
        facts.append({"key": "first", "big": str(focus.reps), "small": f"{noun} in your first logged set", "tone": "hype"})
    else:
        d = focus.reps - previous.reps
        if d > 0:
            facts.append({"key": "delta", "big": f"+{d}", "small": "more reps than your last set", "tone": "hype"})
        elif d < 0:
            facts.append({"key": "delta", "big": str(d), "small": "fewer reps than your last set", "tone": "calm"})
        else:
            facts.append({"key": "delta", "big": "=", "small": f"same {focus.reps} reps as your last set", "tone": "calm"})

    # 2. best set
    if focus.reps == best and len(sets) > 1 and focus.reps > 0 and sum(1 for s in sets if s.reps == best) == 1:
        facts.append({"key": "best", "big": str(best), "small": "new personal best!", "tone": "hype"})
    else:
        facts.append({"key": "best", "big": str(best), "small": "your best set", "tone": "warm"})

    # 3. all time
    facts.append({"key": "total", "big": str(all_time_reps), "small": f"{noun}, all time", "tone": "warm"})

    # 4. streak or sets count
    streak = _streak_days([s.at.date() for s in sets])
    if streak >= 2:
        facts.append({"key": "streak", "big": f"{streak} days", "small": "in a row", "tone": "hype"})
    else:
        facts.append({"key": "sets", "big": str(all_time_sets), "small": "sets logged" if all_time_sets != 1 else "set logged", "tone": "warm"})

    # 5. tempo
    other_tempo = _avg([s.tempo_s for s in others if s.tempo_s is not None])
    if focus.tempo_s is not None:
        small = "per rep"
        if other_tempo is not None and abs(focus.tempo_s - other_tempo) >= 0.3:
            small = "per rep, quicker than usual" if focus.tempo_s < other_tempo else "per rep, slower than usual"
        facts.append({"key": "tempo", "big": f"{focus.tempo_s:.1f}s", "small": small, "tone": "calm"})

    # 6. depth (lower angle = deeper)
    other_depth = _avg([s.depth_deg for s in others if s.depth_deg is not None])
    joint = focus.joint or "joint"
    if focus.depth_deg is not None:
        small = f"lowest {joint} angle (smaller = deeper)"
        if other_depth is not None and abs(focus.depth_deg - other_depth) >= 3:
            small = "deeper than your other sets" if focus.depth_deg < other_depth else "shallower than your other sets"
        facts.append({"key": "depth", "big": f"{focus.depth_deg:.0f}°", "small": small, "tone": "calm"})

    # 7. balance, back, time moving (only when measured)
    if focus.balance_deg is not None and focus.balance_deg >= 3:
        facts.append({"key": "balance", "big": f"{focus.balance_deg:.0f}°", "small": "left vs right difference", "tone": "calm"})
    if focus.back_peak_deg is not None:
        facts.append({"key": "back", "big": f"{focus.back_peak_deg:.0f}°", "small": "peak lower-back bend per rep", "tone": "calm"})
    if focus.slowdown_pct is not None and focus.slowdown_pct >= 15:
        facts.append({"key": "fatigue", "big": f"+{focus.slowdown_pct}%", "small": "slower over your last reps", "tone": "calm"})
    elif focus.slowdown_pct is not None and focus.slowdown_pct <= -10:
        facts.append({"key": "fatigue", "big": f"{focus.slowdown_pct}%", "small": "your last reps got quicker", "tone": "hype"})
    moving = sum(s.duration_s for s in sets if s.duration_s)
    if moving >= 60:
        facts.append({"key": "moving", "big": _mmss(moving), "small": f"spent doing {noun} lately", "tone": "warm"})

    facts = facts[:MAX_FACTS]

    # ---- the one piece of advice, chosen by code
    recent_avg = _avg([float(s.reps) for s in others[:3]])
    issues_total: dict[str, int] = {}
    for k, v in focus.back_issues.items():
        issues_total[k] = issues_total.get(k, 0) + v
    if issues_total:
        top = max(issues_total, key=lambda k: issues_total[k])
        advice_key, advice = f"back_{top}", SPINE_ADVICE.get(top, "Keep your back steady through each rep.")
    elif focus.depth_deg is not None and other_depth is not None and focus.depth_deg - other_depth >= 5:
        advice_key, advice = "depth", f"Your reps were shallower than usual: aim to go as low as your other sets ({other_depth:.0f}°)."
    elif focus.balance_deg is not None and focus.balance_deg >= 8:
        advice_key, advice = "balance", "Even out your left and right sides: face the camera square and move both sides together."
    elif focus.depth_drop_deg is not None and focus.depth_drop_deg >= 8:
        advice_key, advice = "fade", "Your last reps got shallower as you tired: stop the set when you can't reach full depth."
    elif recent_avg is not None and focus.reps < 0.8 * recent_avg:
        advice_key, advice = "recover", f"Fewer reps than usual: rest a bit longer before your next set and aim for {round(recent_avg)}."
    else:
        advice_key, advice = "push", f"Next time, aim for {best + 1}: one more than your best."

    context = {
        "exercise": label,
        "facts": [{"value": f["big"], "meaning": f["small"]} for f in facts],
        "advice_chosen_by_code": advice,
        "best_set": best,
        "next_target": best + 1,
        "recent_sets_newest_first": [s.reps for s in sets[:MAX_SETS]],
        "this_set_slowdown_percent": focus.slowdown_pct,
        "this_set_depth_drop_degrees": focus.depth_drop_deg,
    }
    # Rep waves of the recent sets, oldest first (the app draws one mini bar chart per set).
    waves = [
        {"session_id": s.session_id, "at": s.at.isoformat(), "reps": s.reps, "bars": s.bars}
        for s in reversed(sets[:6])
    ]
    return {"facts": facts, "advice": advice, "advice_key": advice_key, "context": context, "waves": waves}


def rules_headline(trends: dict[str, Any]) -> str:
    """The headline when the LLM is off or its text failed a check."""
    first = trends["facts"][0]
    return f"{first['big']} {first['small']}".strip()
