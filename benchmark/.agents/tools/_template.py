"""Example Nexus extension tool. Copy this file, rename it, and edit.

A workspace extension is a ``.py`` file under ``.agents/tools/`` that declares a
tool with a module-level ``SPEC`` and an async ``run(args, ctx)``. Call
ReloadExtensions after writing it so it becomes callable on the next iteration.
Files whose name starts with an underscore (like this one) are never loaded.
"""

from typing import Any

from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="ExampleTool",
    description="One-line description of what this tool does and when to use it.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The value this example tool operates on.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    # A bundle name from nexus.tools.bundles: fs, shell, task, meta, or ext.
    # The active profile must include it for the tool to be selectable.
    bundle="ext",
    # mutates=True makes the tool run alone (exclusive) and is gated by the
    # permission engine like every other tool.
    mutates=False,
)


async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:
    """Run one call. ``ctx`` exposes workspace, session/turn id, and emit()."""
    query = args.get("query")
    if not isinstance(query, str) or not query:
        return ToolExecutionResult.text("ExampleTool: 'query' is required", is_error=True)
    return ToolExecutionResult.text(f"example: {query}")
