"""Queued database writes that can be retried later (e.g. when finishing a workout).

A write is ``(kind, args)``. Keeping them as data (not closures) makes the retry
queue easy to inspect and test. Every kind is idempotent (upserts), so retrying
a write that actually succeeded the first time is harmless.
"""

from __future__ import annotations

from typing import Any

REP = "rep"
COACHING_EVENT = "coaching_event"

Write = tuple[str, tuple[Any, ...]]


def perform(store: Any, write: Write) -> None:
    kind, args = write
    if kind == REP:
        store.save_repetition(*args)  # (session_id, rep_number, metrics)
    elif kind == COACHING_EVENT:
        store.save_coaching_event(*args)  # (session_id, event_id, rep_number, issue_code, message, action)
    else:
        raise ValueError(f"unknown write kind {kind!r}")
