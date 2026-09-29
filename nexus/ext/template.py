"""The packaged workspace tool template (plan section 6.5).

The harness seeds ``.agents/tools/_template.py`` so the model can ``Read`` it to
learn the extension contract before authoring a tool. Two properties matter:

* the file name begins with an underscore, so the loader treats it as a support
  module and **never** imports or registers it as a tool;
* it is written only when absent -- a user edit or an agent-authored template is
  never overwritten.

The source below is the canonical example: a validated ``SPEC`` and an async
``run(args, ctx)``. Keeping it in the package (rather than only in a gitignored
workspace) makes it available from a clean checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "TOOL_TEMPLATE_FILENAME",
    "TOOL_TEMPLATE_SOURCE",
    "ensure_tool_template",
    "read_tool_template",
]

#: The reserved template filename; the loader ignores leading-underscore files.
TOOL_TEMPLATE_FILENAME = "_template.py"

#: The exact example seeded into ``.agents/tools/_template.py``.
TOOL_TEMPLATE_SOURCE = '''"""Example Nexus extension tool. Copy this file, rename it, and edit.

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
'''

#: The template bytes, computed once so seeding and tests agree exactly.
_TEMPLATE_BYTES = TOOL_TEMPLATE_SOURCE.encode("utf-8")


def read_tool_template() -> str:
    """The canonical template source (never read from the workspace)."""
    return TOOL_TEMPLATE_SOURCE


def ensure_tool_template(tools_dir: str | os.PathLike[str]) -> Path | None:
    """Write ``_template.py`` into ``tools_dir`` if it does not already exist.

    Returns the path when it was created, or ``None`` when a file was already
    present (a user edit is never overwritten) or the directory could not be
    created. Never follows or replaces a symlink: an existing symlink is left
    untouched.
    """
    directory = Path(tools_dir)
    target = directory / TOOL_TEMPLATE_FILENAME
    try:
        if target.exists() or target.is_symlink():
            return None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive create so a race cannot clobber a file written in between.
        try:
            with target.open("xb") as handle:
                handle.write(_TEMPLATE_BYTES)
        except FileExistsError:
            return None
    except OSError:
        return None
    return target
