# Claude Code project instructions

Read `AGENTS.md` completely before planning or modifying this repository. Treat it as the shared project specification. Inspect existing code first: recommendations in AGENTS.md are not evidence that components already exist. Follow its data contracts, security requirements, MVP priorities, and testing rules. If you discover a conflict between the documented plan and working code, explain it and propose a minimal compatible change rather than silently rewriting teammates' work.

When working under `mobile/`, also read `mobile/AGENTS.md` (Expo/React Native rules). The backend is in `server/`; run its tests with `server/.venv/Scripts/python -m pytest` (see `server/requirements-dev.txt` to set up the venv).
