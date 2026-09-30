"""The mock scenario catalogue (MOCK_PLAN §6.2).

Each module exposes ``SCENARIO``. Add a module here and list it in ``_MODULES``;
``tests/test_mock_scenarios.py`` runs every non-slow scenario end to end.
"""
from __future__ import annotations

from importlib import import_module

from ..dsl import Scenario

_MODULES = (
    "hello",
    "tool_marathon",
    "parallel_tools",
    "parallel_subagents",
    "agent_limits",
    "streaming_rich",
    "errors",
    "provider_failure",
    "cancel",
    "question",
    "context_pressure",
    "diff_review",
    "stress",
    "bash_wait",
)


def _load() -> dict[str, Scenario]:
    catalog: dict[str, Scenario] = {}
    for name in _MODULES:
        scenario: Scenario = import_module(f"{__name__}.{name}").SCENARIO
        catalog[scenario.name] = scenario
    return catalog


CATALOG: dict[str, Scenario] = _load()

__all__ = ["CATALOG"]
