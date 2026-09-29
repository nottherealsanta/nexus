"""Shared test isolation (STATE_PLAN §6).

Every test gets a private ``NEXUS_HOME`` so nothing ever writes the real
``~/.nexus/nexus.db`` or per-project state under the user's home.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_nexus_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path_factory.mktemp("nexus-home")))
