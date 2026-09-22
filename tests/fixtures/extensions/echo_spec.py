"""A well-formed extension using the ``SPEC`` + ``run`` contract.

This is the canonical starting point an agent is meant to read and copy (the
``.nexus/tools/_template.py`` shape from plan section 6.5). It declares one
read-only tool and implements it as an async ``run(args, ctx)``.
"""

from __future__ import annotations

from typing import Any

from nexus.tools.spec import ToolContext, ToolExecutionResult

SPEC = {
    "name": "EchoFixture",
    "description": "Echo a short message back. A fixture for the loader tests.",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    "bundle": "fs",
    "mutates": False,
}


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult.text(f"echo:{args.get('message', '')}")
