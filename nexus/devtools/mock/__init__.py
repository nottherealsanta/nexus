"""Mock scenarios: a scripted provider, a scenario catalogue and a sandbox (MOCK_PLAN)."""
from __future__ import annotations

from .dsl import Scenario
from .provider import MOCK_PROVIDER, MockProvider, MockRouteViolation

__all__ = ["MOCK_PROVIDER", "MockProvider", "MockRouteViolation", "Scenario", "catalog"]


def catalog() -> dict[str, Scenario]:
    from .scenarios import CATALOG

    return dict(CATALOG)
