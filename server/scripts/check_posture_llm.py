"""LIVE check of posture cues: replays SIMULATED upper-back sensor data through the posture rules and the real coach
LLM (the same CoachAgent path the WebSocket uses) and prints the rule message next to the model's wording.

    python -m scripts.check_posture_llm                       (run from server/; every scenario that warns)
    python -m scripts.check_posture_llm lateral_shift --debug (one scenario; print what is sent and returned)

Needs COACH_LLM_PROVIDER=gemini and GEMINI_API_KEY in server/.env. Each warning is one short, real request.
Nothing is written to any database. A "FELL BACK" line is not necessarily a bug: read its reason.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv

from app.sensor_simulation import SCENARIOS, generate
from app.services.agent import agent_from_env, build_posture_context
from app.services.coaching import CoachingDecision
from app.services.posture import PostureCoach


def main() -> int:
    p = argparse.ArgumentParser(description="Replay simulated posture data through the live coach LLM.")
    p.add_argument("scenario", nargs="*", help=f"default: every scenario that warns ({', '.join(SCENARIOS)})")
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
    print(f"model: {agent._provider._model}  (SIMULATED sensor data)")

    async def run() -> int:
        bad = 0
        for name in args.scenario or ["forward_lean", "lean_creep", "lateral_shift", "slouched_standing"]:
            coach = PostureCoach()
            # Ask at the moment the rules decide, as the server does (so the context is what it would be live).
            for found in filter(None, (coach.feed(m) for m in generate(name).messages)):
                candidate = CoachingDecision(found.rep_number, found.issue_code, found.message, event_id=found.event_id)
                before = dict(agent.fallback_reasons)
                t = time.monotonic()
                final = await agent.refine(candidate, build_posture_context("squat", found, coach))
                dt = time.monotonic() - t
                print(f"\n{name}: rep {found.rep_number} [{found.issue_code}]")
                print(f"  rules : {found.message}")
                if final is None:
                    print(f"  agent : (chose no_action: stay quiet)  [{dt:.1f}s]")
                elif final.source == "agent":
                    print(f"  agent : {final.message}  [{dt:.1f}s, {final.action}]")
                else:
                    bad += 1
                    new = {k: v - before.get(k, 0) for k, v in agent.fallback_reasons.items() if v != before.get(k, 0)}
                    print(f"  FELL BACK to the rule message [{dt:.1f}s]: reason = {new}")
        await agent.aclose()
        return 1 if bad else 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
