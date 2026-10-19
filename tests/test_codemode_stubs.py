"""Plan 03 section 7 test 5: JSON Schema to stub conversion and type checking."""
from __future__ import annotations

import ast
import asyncio
import importlib.util

import pytest

from nexus.tools.builtin import BUILTIN_TOOLS, OPT_IN_TOOLS
from nexus.tools.codemode.catalog import build_catalog, declarations
from nexus.tools.codemode.stubs import annotation, stub

MONTY = importlib.util.find_spec('pydantic_monty') is not None

# Shapes recorded from MCP servers (filesystem / github style).
MCP_SCHEMAS = {
    'mcp__fs__read_text_file': {
        'type': 'object', 'required': ['path'], 'additionalProperties': False,
        'properties': {'path': {'type': 'string', 'description': 'x\nasync def evil(): ...'},
                       'head': {'type': 'number'}, 'tail': {'type': 'number'}}},
    'mcp__github__search_issues': {
        'type': 'object', 'required': ['q'],
        'properties': {'q': {'type': 'string'},
                       'sort': {'type': 'string', 'enum': ['comments', 'created']},
                       'labels': {'type': 'array', 'items': {'type': 'string'}},
                       'page': {'type': ['integer', 'null']},
                       'filter': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
                       'extra': {'$ref': '#/definitions/x'}}},
}


def _compiles(source):
    ast.parse(source)


def test_required_optional_enum_union_array():
    out = stub('t', {'type': 'object', 'required': ['a'], 'properties': {
        'a': {'type': 'integer'}, 'b': {'type': 'string'},
        'c': {'enum': ['x', 'y']}, 'd': {'type': ['string', 'null']},
        'e': {'type': 'array', 'items': {'type': 'string'}},
        'f': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'integer'}}},
        'g': {'type': 'object', 'properties': {'z': {'type': 'string'}}},
        'h': {'oneOf': [{'type': 'string'}, {'type': 'boolean'}]},
        'i': {'anyOf': [{'type': 'string'}, {'$ref': '#/x'}]}}})
    assert out == (
        "async def t(*, a: int, b: str | None = None, c: Literal['x', 'y'] | None = None, "
        "d: str | None = None, e: list[str] | None = None, f: list[list[int]] | None = None, "
        "g: dict[str, Any] | None = None, h: str | bool | None = None, i: Any = None) -> Any: ...")
    _compiles(out)


def test_never_rejects_and_never_emits_prose():
    for schema in (None, [], 5, {'properties': 3}, {'properties': {'a b': {}}},
                   {'properties': {'class': {}}}, {'properties': {'a': {'enum': [{'k': 1}]}}},
                   {'properties': {'a': {'enum': ['x' * 500]}}}, {'required': 'a'}):
        _compiles(stub('t', schema))
    assert annotation({'type': 'weird'}) == 'Any'
    assert annotation({'enum': ["a'\nb"]}) == "Literal[\"a'\\nb\"]"
    out = stub('t', MCP_SCHEMAS['mcp__fs__read_text_file'])
    assert 'evil' not in out
    assert '\n' not in out


def test_unsafe_name_is_refused():
    with pytest.raises(ValueError):
        stub('not-ok', {})


@pytest.mark.parametrize('tool', list(BUILTIN_TOOLS) + list(OPT_IN_TOOLS), ids=lambda t: t.spec.name)
def test_every_builtin_tool_has_valid_stub(tool):
    out = stub(tool.spec.name.replace('-', '_'), tool.spec.input_schema)
    _compiles(out)
    for key in tool.spec.input_schema.get('properties', {}):
        assert key in out


def _fake(name, schema):
    from types import SimpleNamespace
    spec = SimpleNamespace(name=name, description='d', input_schema=schema, bundle='mcp', mutates=False)
    return SimpleNamespace(spec=spec)


def test_mcp_declarations_parse():
    tools = [_fake(n, s) for n, s in MCP_SCHEMAS.items()]
    _compiles(declarations(build_catalog(tools, 'all')))


@pytest.mark.skipif(not MONTY, reason='pydantic_monty not installed')
def test_monty_accepts_all_stubs_and_rejects_wrong_type_before_host_call():
    from pydantic_monty import AsyncMonty

    tools = list(BUILTIN_TOOLS) + list(OPT_IN_TOOLS) + [_fake(n, s) for n, s in MCP_SCHEMAS.items()]
    stubs = declarations(build_catalog(tools, 'all'))
    calls = []

    async def host(*args, **kwargs):
        calls.append(kwargs)
        return 'ok'

    async def go():
        async with AsyncMonty(min_processes=0, max_processes=1) as pool:
            async with pool.checkout(type_check=True, type_check_stubs=stubs) as session:
                with pytest.raises(Exception) as bad:
                    await session.feed_run('await read(path=5)', external_lookup={'read': host})
                assert 'invalid-argument-type' in str(bad.value) or 'Expected' in str(bad.value)
                assert calls == []
                with pytest.raises(Exception):
                    await session.feed_run('await mcp__github__search_issues(q="a", sort="nope")',
                                           external_lookup={'mcp__github__search_issues': host})
                assert calls == []
                ok = await session.feed_run('await read(path="a", limit=3)', external_lookup={'read': host})
                assert ok == 'ok'
                assert calls == [{'path': 'a', 'limit': 3}]

    asyncio.run(go())
