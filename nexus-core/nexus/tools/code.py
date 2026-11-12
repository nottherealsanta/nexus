"""The code task-tool spec; the daemon binds execution to its kernel registry."""
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError

SPEC = ToolSpec(
    name="code",
    description=(
        "Run restricted Python in a persistent Monty sandbox, not full CPython. Variables survive cells; each session has a private default kernel. "
        "Prefer this for multi-step inspection, independent tool batches, and filtering large results before returning evidence. Use direct calls for single simple operations. "
        "Tools are already bound: use await tool_name(keyword=value), never import functions. Results are strings. "
        "import asyncio and asyncio.gather allow concurrent calls; OS/CPython modules such as subprocess, sqlite3 and pprint are unavailable. "
        "No filesystem or network access except through these tools. Control tools and code are unavailable inside. "
        "Example: import asyncio; results = await asyncio.gather(grep(pattern='TODO', path='src'), ls(path='tests')); print(results[0]). "
        "Inspect current affected text before editing: retained-but-unprinted results have not been inspected by the model. "
        "Print only relevant excerpts or summaries, not every result. A failing call raises RuntimeError and stops the cell; gather has no "
        "return_exceptions, so for a path you have not confirmed wrap the call: async def safe(c):\\n try: return await c\\n except RuntimeError as e: return str(e). "
        "If a cell stops anyway, the completed calls' outputs are in last_outputs[N]; read them there, do not re-run. "
        "Images cannot be viewed inside a cell; call read directly for those. "
        "Omit kernel_id for your private kernel; pass an id to share deliberately. "
        "Forks cannot borrow other sessions' kernels."
    ),
    parameters={"type": "object", "properties": {
        "code": {"type": "string"}, "kernel_id": {"type": "string"},
        "timeout_s": {"type": "number", "description": "Whole-cell timeout in seconds (default 300, maximum 600)"},
    }, "required": ["code"]},
)


async def unbound(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("code requires a daemon kernel registry")


TOOL = Tool(SPEC, unbound)
