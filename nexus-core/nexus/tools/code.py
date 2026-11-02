"""The code task-tool spec; the daemon binds execution to its kernel registry."""
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError

SPEC = ToolSpec(
    name="code",
    description=(
        "Run Python in a persistent Monty sandbox. Variables survive cells and are shared by the session tree. "
        "Prefer this for multi-step inspection, independent tool batches, and filtering large results before returning evidence. Use direct calls for single simple operations. "
        "Use await tool_name(keyword=value) for the task tools granted in this request; results are strings. import asyncio and asyncio.gather allow concurrent calls. "
        "No filesystem or network access except through these tools. Control tools and code are unavailable inside. "
        "Example: import asyncio; results = await asyncio.gather(grep(pattern='TODO', path='src'), ls(path='tests')); print(results[0]). "
        "Reads inside cells do not establish read-before-edit coverage, even if printed; read affected lines directly before editing. "
        "Print or return only the results you need. Omit kernel_id for the tree's main kernel; use an id to share another."
    ),
    parameters={"type": "object", "properties": {
        "code": {"type": "string"}, "kernel_id": {"type": "string"},
        "timeout_s": {"type": "number", "description": "Whole-cell timeout in seconds (default 300, maximum 600)"},
    }, "required": ["code"]},
)


async def unbound(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("code requires a daemon kernel registry")


TOOL = Tool(SPEC, unbound)
