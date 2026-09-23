"""Example Nexus tool. Copy this file into `.nexus/tools/` to make it live.

A workspace tool is a ``.py`` file under ``.nexus/tools/`` that declares a
module-level ``SPEC`` and an async ``run(args, ctx)``. Call ``ReloadExtensions``
after copying it so it becomes callable on the next loop iteration. Loading is
gated by the same quarantine as every other extension: a broken file leaves the
previous manifest untouched and returns its error to the model.
"""
from __future__ import annotations

from typing import Any

from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="Greet",
    description="Return a friendly greeting for a name. Useful as a smoke test.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Who to greet.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    # A bundle from nexus.tools.bundles: fs | shell | task | meta | ext. The
    # active profile must include it for the tool to be selectable.
    bundle="ext",
    # mutates=True would make the tool run exclusively and default to `ask`.
    mutates=False,
)


async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:
    """One call. ``ctx`` exposes workspace, session/turn id, and emit()."""
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return ToolExecutionResult.text("Greet: 'name' is required", is_error=True)
    return ToolExecutionResult.text(f"Hello, {name.strip()}!")
