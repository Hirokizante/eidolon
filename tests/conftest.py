"""Deterministic defaults for synthetic test configurations."""

import pytest


@pytest.fixture(autouse=True)
def fixed_config_timezone(monkeypatch):
    # Date fixtures and schedule expectations use Manila time. Production defaults
    # detect the host zone; tests of that detection override TZ explicitly.
    monkeypatch.setenv("TZ", "Asia/Manila")
