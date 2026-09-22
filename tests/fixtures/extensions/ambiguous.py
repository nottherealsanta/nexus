"""A module that declares both SPEC and register(): ambiguous, so refused."""

from __future__ import annotations

from typing import Any

from nexus.tools.spec import ToolContext, ToolExecutionResult

SPEC = {
    "name": "BothFixture",
    "description": "declares both forms",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult.text("both")


def register():
    return []
