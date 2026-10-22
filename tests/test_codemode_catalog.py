"""Plan 03 section 3.4 / 3.11: exposure, exclusions, collisions, budgeted rendering."""
from __future__ import annotations

from types import SimpleNamespace


from nexus.tools.codemode.catalog import build_catalog, declarations, describe, render_catalog


def tool(name, *, mutates=False, bundle='fs', schema=None):
    return SimpleNamespace(spec=SimpleNamespace(
        name=name, description=f'{name} tool', bundle=bundle, mutates=mutates,
        input_schema=schema or {'type': 'object', 'required': ['a'], 'properties': {'a': {'type': 'string'}}}))


TOOLS = [tool('read'), tool('edit', mutates=True), tool('code', bundle='code'),
         tool('subagent'), tool('question'), tool('skill'),
         tool('McpCall'), tool('McpSearch'), tool('ReloadExtensions', bundle='meta'),
         tool('mcp__gh__search', bundle='mcp'), tool('mcp__gh__create', bundle='mcp', mutates=True),
         tool('mcp__fs__read', bundle='mcp')]


def test_read_only_default_excludes_mutating_and_loop_tools():
    assert set(build_catalog(TOOLS)) == {'read', 'mcp__gh__search', 'mcp__fs__read'}


def test_all_includes_mutating_but_never_excluded_set():
    names = set(build_catalog(TOOLS, 'all'))
    assert {'edit', 'mcp__gh__create'} <= names
    assert not names & {'code', 'subagent', 'question', 'skill', 'McpCall',
                        'McpSearch', 'ReloadExtensions'}


def test_read_only_flag_overrides_all_and_lists():
    assert 'edit' not in build_catalog(TOOLS, 'all', read_only=True)
    assert 'mcp__gh__create' not in build_catalog(TOOLS, ['mcp__gh__*'], read_only=True)


def test_glob_list():
    assert set(build_catalog(TOOLS, ['mcp__gh__*', 'edit'])) == {
        'mcp__gh__search', 'mcp__gh__create', 'edit'}
    assert set(build_catalog(TOOLS, ['code', 'skill'])) == set()


def test_unsafe_reserved_and_colliding_names_are_skipped_not_raised():
    catalog = build_catalog([tool('a-b'), tool('a_b'), tool('store'), tool('bad name'),
                             tool('class'), tool('ok')], 'all')
    assert set(catalog) == {'a_b', 'ok'}
    assert catalog['a_b'].spec.name == 'a-b'
    reasons = dict(catalog.skipped)
    assert set(reasons) == {'a_b', 'store', 'bad name', 'class'}
    assert 'collides' in reasons['a_b']
    assert 'reserved' in reasons['store']


def test_declarations_and_describe():
    catalog = build_catalog(TOOLS)
    out = declarations(catalog)
    assert out.startswith('from typing import Any, Literal')
    assert 'async def read(*, a: str) -> Any: ...' in out
    assert describe(catalog, 'read')['description'] == 'read tool'
    assert describe(catalog, 'nope') is None


def test_render_counts_namespaces_and_pointer():
    catalog = build_catalog(TOOLS)
    out = render_catalog(catalog, budget_tokens=0)
    assert 'built-in: 1 tool' in out
    assert 'mcp:gh: 1 tool' in out and 'mcp:fs: 1 tool' in out
    assert 'async def' not in out
    assert '3 more tools: use search_tools(query) / describe_tool(name)' in out


def test_render_budget_and_pinned_first():
    many = [tool(f'mcp__s__t{i}', bundle='mcp') for i in range(40)]
    catalog = build_catalog(many)
    full = render_catalog(catalog, budget_tokens=100_000)
    assert 'more tool' not in full and full.count('async def') == 40
    small = render_catalog(catalog, budget_tokens=200, pinned=['mcp__s__t39'])
    assert 0 < small.count('async def') < 40
    assert 'async def mcp__s__t39(' in small
    assert small.index('mcp__s__t39(') < small.index('async def mcp__s__t0(') if 'async def mcp__s__t0(' in small else True
    assert 'more tools: use search_tools' in small


def test_render_empty():
    assert render_catalog(build_catalog([]), budget_tokens=100)
