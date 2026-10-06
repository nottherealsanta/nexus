"""Skills and MCP end to end: a real stdio MCP call, deferred search and call, then a skill load.

The skill goes last: ``code-review`` declares ``allowed-tools`` and narrows later calls.

Uses the sandbox seeds (``code-review`` skill, ``mock-tracker`` and ``mock-docs`` servers from
``nexus/devtools/mock/mcp_server.py``) so the context header's Skills, Tools and MCP sections
show real data in ``nexus --dev chat``.
"""
from ..checks import no_unexpected_errors, tool_called, tool_errors
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    name="extensions",
    summary="load a skill, call a stdio MCP tool, search deferred MCP tools and call one",
    tags=("skills", "mcp"),
    est_seconds=8,
    prompt="Exercise the seeded skills and MCP servers.",
    actors={
        "main": [
            calls(call("mcp__mock_tracker__list_issues", state="open"), text="Listing open issues over stdio."),
            calls(call("McpSearch", queries=[{"query": "lookup"}]), text="Searching the deferred docs server."),
            calls(call("McpCall", tool="mcp__mock_docs__lookup_mcp", arguments={"query": "servers"}), text="Calling one deferred tool."),
            calls(call("skill", name="code-review"), text="Last, loading the code-review skill."),
            verdict("extensions", intro="Skill and MCP calls all returned.", checks=[
                tool_called("skill", 1), tool_called("mcp__mock_tracker__list_issues", 1),
                tool_called("McpSearch", 1), tool_called("McpCall", 1),
                tool_errors("McpCall", 0), tool_errors("mcp__mock_tracker__list_issues", 0), no_unexpected_errors(),
            ]),
        ]
    },
)
