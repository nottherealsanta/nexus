"""Deterministic, bounded ranking and exact selection."""
from types import SimpleNamespace

from nexus.mcp.search import SearchIndex, tokens
from nexus.tools.spec import RegisteredTool, ToolSpec


async def unused(data, ctx):
    raise AssertionError("Search must not execute")


def tool(name, description="Tool", parameters=()):
    return RegisteredTool(ToolSpec(name="mcp__s__"+name, description=description,
        bundle="task", input_schema={"type": "object", "properties": {p: {"type": "string"} for p in parameters}}), unused)


def test_tokens_ranking_and_determinism():
    assert tokens("getHTTP_issue-id.v2") == {"get", "http", "issue", "id", "v2"}
    tools = (tool("createIssue"), tool("other", "create issue"), tool("params", parameters=("issue",)))
    index = SearchIndex({"s": SimpleNamespace(tools=tools)})
    assert [m.local_name for m in index.search("issue").matches] == ["createIssue", "params", "other"]
    assert index.search("createIssue").matches[0].local_name == "createIssue"
    other = SearchIndex({"s": SimpleNamespace(tools=tools[::-1])})
    assert [m.name for m in index.search("issue").matches] == [m.name for m in other.search("issue").matches]


def test_select_and_limits():
    index = SearchIndex({"s": SimpleNamespace(tools=(tool("one"), tool("two")))})
    result = index.search("select:one,s/two,missing", limit=1)
    assert [m.name for m in result.matches] == ["s/one", "s/two"]
    assert "not found: missing" in result.errors[0]
    bounded = SearchIndex({"s": SimpleNamespace(tools=tuple(tool(f"tool{i}") for i in range(2001)))})
    assert len(bounded.matches) == 2000
    assert "1 tools omitted" in bounded.errors[0]
