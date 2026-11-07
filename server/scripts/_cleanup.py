"""Shared by the live scripts: remove the Tiger Data readings of workouts they created.

Supabase rows vanish when the throwaway user is deleted (cascade), but Tiger Data has no link to Supabase,
so the scripts must delete their readings explicitly. Best effort: never raises.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable

from app.services.timeseries import TigerStore


def purge_tiger_readings(session_ids: Iterable[str], settle_s: float = 0.0) -> int:
    """Delete readings for these session ids. ``settle_s`` waits for any still-buffered writes to land first."""
    ids = [s for s in session_ids if s]
    store = TigerStore()
    if not ids or not store.configured:
        return 0
    if settle_s:
        time.sleep(settle_s)
    removed = 0
    try:
        for sid in ids:
            removed += store.delete_session_readings(sid)
    except Exception as exc:
        print(f"WARNING: could not clean up Tiger Data readings ({type(exc).__name__}); delete them by session id")
    finally:
        store.close()
    return removed


def arm_watchdog(seconds: float, cleanup: Callable[[], object]) -> threading.Timer:
    """If a live script is still running after ``seconds`` (e.g. it waits forever for a message that never
    comes), run ``cleanup`` and exit, so a hang can never leave test users or readings behind.
    Call ``.cancel()`` on the returned timer when the script finishes normally."""

    def fire() -> None:
        print(f"\nTIMEOUT: still running after {seconds:.0f} s; cleaning up and exiting", flush=True)
        try:
            cleanup()
            print("cleanup after timeout done", flush=True)
        except Exception as exc:
            print(f"cleanup after timeout FAILED ({type(exc).__name__}); remove the test user and its readings by hand", flush=True)
        finally:
            os._exit(1)

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t
