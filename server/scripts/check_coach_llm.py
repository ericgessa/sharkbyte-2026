"""The FIRST LIVE TEST of the AI coach: makes 2 real LLM calls with synthetic data and shows what happened.

    python -m scripts.check_coach_llm        (run from server/)

Needs COACH_LLM_PROVIDER=gemini (or anthropic) and that vendor's key in server/.env (GEMINI_API_KEY, or
ANTHROPIC_API_KEY). It may cost a small amount of real money (two short requests). Nothing is written to any database.

It checks the one thing the offline tests cannot: that the real API accepts the requests this code sends
(model id, schema-constrained JSON output) and that the replies pass the safety checks. A "FELL BACK"
result is not necessarily a bug (the checks are strict on purpose): read the reason it prints.
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.services.agent import agent_from_env, build_cue_context, rules_summary
from app.services.coaching import CoachingDecision, MESSAGES
from app.services.rep_counter import RepMetrics


def main() -> int:
    agent = agent_from_env()
    if not agent.enabled:
        print("The coach LLM is OFF. Set COACH_LLM_PROVIDER=gemini (or anthropic) and the matching API key in server/.env (see .env.example).")
        return 2

    reps = [RepMetrics(i, d, 400, 400, 1700, 2.0) for i, d in enumerate([85, 85, 85, 98, 98], start=1)]
    candidate = CoachingDecision(5, "reduced_depth", MESSAGES["reduced_depth"])
    cue_ctx = build_cue_context("squat", reps, candidate, [(5, "reduced_depth")])
    summary_ctx = {
        "supabase": {
            "stats": {"rep_count": 5, "avg_min_knee_angle_deg": 90.2, "deepest_knee_angle_deg": 85.0,
                      "shallowest_knee_angle_deg": 98.0, "depth_change_deg": 8.7, "avg_rep_duration_ms": 1703, "max_asymmetry_deg": 2.0},
            "coaching_events": [{"rep_number": 5, "issue_code": "reduced_depth", "action": "send_feedback"}],
        }
    }

    async def run() -> int:
        bad = 0
        try:
            t = time.monotonic()
            final = await agent.refine(candidate, cue_ctx)
            dt = time.monotonic() - t
            if final is None:
                print(f"CUE      ok ({dt:.1f}s): the model chose no_action (stay quiet)")
            elif final.source == "agent":
                print(f"CUE      ok ({dt:.1f}s) [{final.action}]: {final.message}")
            else:
                bad += 1
                print(f"CUE      FELL BACK to the rule message ({dt:.1f}s): reason = {agent.fallback_reasons}")
            t = time.monotonic()
            text, source = await agent.summarize(summary_ctx, rules_summary(summary_ctx))
            dt = time.monotonic() - t
            if source == "agent":
                print(f"RECAP    ok ({dt:.1f}s): {text}")
            else:
                bad += 1
                print(f"RECAP    FELL BACK to the rules recap ({dt:.1f}s): reason = {agent.fallback_reasons}")
        finally:
            await agent.aclose()
        return bad

    bad = asyncio.run(run())
    print("\nLive coach check passed" if not bad else f"\n{bad} of 2 calls fell back to the rules (see reasons above)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
