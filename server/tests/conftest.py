"""Shared test setup.

Tests must never touch a real Tiger Data database by accident: if a developer has TIGERDATA_URL in
server/.env, it is hidden from every test except the opt-in live ones (which use REAL_TIGERDATA_URL).
"""

import os

import pytest

import app.services.timeseries  # noqa: F401  (loads server/.env so the URL below is captured)

REAL_TIGERDATA_URL = os.environ.get("TIGERDATA_URL", "").strip()


@pytest.fixture(autouse=True)
def _hide_real_tigerdata(monkeypatch):
    monkeypatch.delenv("TIGERDATA_URL", raising=False)
