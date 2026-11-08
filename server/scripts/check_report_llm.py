"""LIVE check of the end-of-workout report: sends SIMULATED finished workouts to the real coach LLM (the same
``CoachAgent.summarize`` the ``coach-summary`` endpoint uses) and prints the deterministic rules report next to the
model's version, with which source was used.

    python -m scripts.check_report_llm            (run from server/)
    python -m scripts.check_report_llm --debug    (also print what is sent to the model and what it returns)

Needs COACH_LLM_PROVIDER=gemini and GEMINI_API_KEY in server/.env. One short request per workout below.
Nothing is written to any database. "rules" in the output is not necessarily a bug: the reason is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv

from app.services.agent import agent_from_env, rules_summary

# Simulated finished workouts, in the shape services/coach_context.py builds (Supabase part only).
WORKOUTS = {
    "squats, depth fading, lower back rounding": {
        "supabase": {
            "stats": {"rep_count": 10, "avg_min_knee_angle_deg": 92.4, "deepest_knee_angle_deg": 84.0,
                      "shallowest_knee_angle_deg": 101.0, "depth_change_deg": 9.5, "avg_rep_duration_ms": 2100,
                      "max_asymmetry_deg": 4.0},
            "coaching_events": [
                {"rep_number": 4, "issue_code": "lower_back_rounding", "action": "send_feedback"},
                {"rep_number": 7, "issue_code": "reduced_depth", "action": "send_feedback"},
                {"rep_number": 9, "issue_code": "lower_back_rounding", "action": "send_feedback"},
            ],
        },
        "timeseries": {"available": False, "reason": "not_configured"},
    },
    "push-ups, short reps and sagging hips": {
        "supabase": {
            "stats": {"rep_count": 12, "avg_min_angle_deg": 96.0, "deepest_angle_deg": 88.0, "shallowest_angle_deg": 99.0,
                      "depth_change_deg": 3.0, "avg_rep_duration_ms": 1500, "max_asymmetry_deg": 3.0},
            "coaching_events": [
                {"rep_number": 5, "issue_code": "partial_range", "action": "rep_rejected"},
                {"rep_number": 6, "issue_code": "hips_sagging", "action": "send_feedback"},
                {"rep_number": 10, "issue_code": "partial_range", "action": "rep_rejected"},
                {"rep_number": 11, "issue_code": "partial_range", "action": "rep_rejected"},
            ],
        },
        "timeseries": {"available": False, "reason": "not_configured"},
    },
    "clean squats": {
        "supabase": {
            "stats": {"rep_count": 8, "avg_min_knee_angle_deg": 86.0, "deepest_knee_angle_deg": 84.0,
                      "shallowest_knee_angle_deg": 88.0, "depth_change_deg": 1.0, "avg_rep_duration_ms": 2200,
                      "max_asymmetry_deg": 3.0},
            "coaching_events": [],
        },
        "timeseries": {"available": False, "reason": "not_configured"},
    },
}


def main() -> int:
    p = argparse.ArgumentParser(description="Send simulated finished workouts to the live report LLM.")
    p.add_argument("--debug", action="store_true", help="print each request sent to the model and its reply")
    args = p.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    if args.debug:
        os.environ["COACH_LLM_DEBUG"] = "1"
    agent = agent_from_env()
    if not agent.enabled:
        print("The coach LLM is OFF. Set COACH_LLM_PROVIDER=gemini and GEMINI_API_KEY in server/.env (see .env.example).")
        return 2
    print(f"model: {agent._provider._model}  (SIMULATED workouts)")

    async def run() -> int:
        bad = 0
        for name, ctx in WORKOUTS.items():
            fallback = rules_summary(ctx)
            before = dict(agent.fallback_reasons)
            t = time.monotonic()
            text, source = await agent.summarize(ctx, fallback)
            dt = time.monotonic() - t
            print(f"\n== {name}")
            print(f"  rules : {fallback}")
            if source == "agent":
                print(f"  gemini: {text}  [{dt:.1f}s]")
            else:
                bad += 1
                new = {k: v - before.get(k, 0) for k, v in agent.fallback_reasons.items() if v != before.get(k, 0)}
                print(f"  FELL BACK to the rules report [{dt:.1f}s]: reason = {new}")
        await agent.aclose()
        return 1 if bad else 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
