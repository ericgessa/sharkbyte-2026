"""Shared test setup.

Tests must never touch a real Tiger Data database by accident: if a developer has TIGERDATA_URL in
server/.env, it is hidden from every test except the opt-in live ones (which use REAL_TIGERDATA_URL).
"""

import os

import pytest

import app.services.timeseries  # noqa: F401  (loads server/.env so the URL below is captured)

try:  # includes TIGERDATA_PASSWORD if the password is kept separate
    REAL_TIGERDATA_URL = app.services.timeseries.tigerdata_url()
except app.services.timeseries.TigerDataConfigError:
    REAL_TIGERDATA_URL = ""


@pytest.fixture(autouse=True)
def _hide_real_tigerdata(monkeypatch):
    monkeypatch.delenv("TIGERDATA_URL", raising=False)
    monkeypatch.delenv("TIGERDATA_PASSWORD", raising=False)
