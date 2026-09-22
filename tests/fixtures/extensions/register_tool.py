"""A well-formed extension using the ``register()`` contract.

``register()`` is synchronous, returns a finite iterable of ready-to-register
``RegisteredTool`` values, and is called by the loader exactly twice: once in
the isolated child for validation, once in-process to bind the live runnables.
"""

from __future__ import annotations

from typing import Any

from nexus.tools.spec import (
    RegisteredTool,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)


async def _run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult.text("registered:ok")


def register() -> list[RegisteredTool]:
    return [
        RegisteredTool(
            spec=ToolSpec(
                name="RegisterFixture",
                description="A tool declared through register().",
                input_schema={"type": "object"},
                bundle="fs",
            ),
            run=_run,
        )
    ]
