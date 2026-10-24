"""Plan 03 section 3.4 / 3.11: exposure, exclusions, collisions (the inline catalog is gone; see test_codemode_manual)."""
from __future__ import annotations

from types import SimpleNamespace


from nexus.tools.codemode.catalog import build_catalog, declarations, describe, excluded_reason


def tool(name, *, mutates=False, bundle='fs', schema=None):
    return SimpleNamespace(spec=SimpleNamespace(
        name=name, description=f'{name} tool', bundle=bundle, mutates=mutates,
        input_schema=schema or {'type': 'object', 'required': ['a'], 'properties': {'a': {'type': 'string'}}}))


TOOLS = [tool('read'), tool('edit', mutates=True), tool('code', bundle='code'),
         *(tool(n, bundle='code') for n in ('code_start', 'code_attach', 'code_run', 'code_reset')),
         tool('subagent'), tool('question'), tool('skill'),
         tool('McpCall'), tool('McpSearch'), tool('ReloadExtensions', bundle='meta'),
         tool('mcp__gh__search', bundle='mcp'), tool('mcp__gh__create', bundle='mcp', mutates=True),
         tool('mcp__fs__read', bundle='mcp')]


def test_read_only_default_excludes_mutating_and_loop_tools():
    assert set(build_catalog(TOOLS)) == {'read', 'mcp__gh__search', 'mcp__fs__read'}


def test_all_includes_mutating_but_never_excluded_set():
    names = set(build_catalog(TOOLS, 'all'))
    assert {'edit', 'mcp__gh__create'} <= names
    assert not names & {'code', 'code_start', 'code_attach', 'code_run', 'code_reset', 'subagent', 'question', 'skill', 'McpCall',
                        'McpSearch', 'ReloadExtensions'}


def test_read_only_flag_overrides_all_and_lists():
    assert 'edit' not in build_catalog(TOOLS, 'all', read_only=True)
    assert 'mcp__gh__create' not in build_catalog(TOOLS, ['mcp__gh__*'], read_only=True)


def test_glob_list():
    assert set(build_catalog(TOOLS, ['mcp__gh__*', 'edit'])) == {
        'mcp__gh__search', 'mcp__gh__create', 'edit'}
    assert set(build_catalog(TOOLS, ['code', 'skill'])) == set()


def test_unsafe_reserved_and_colliding_names_are_skipped_not_raised():
    catalog = build_catalog([tool('a-b'), tool('a_b'), tool('whos'), tool('bad name'),
                             tool('class'), tool('ok')], 'all')
    assert set(catalog) == {'a_b', 'ok'}
    assert catalog['a_b'].spec.name == 'a-b'
    reasons = dict(catalog.skipped)
    assert set(reasons) == {'a_b', 'whos', 'bad name', 'class'}
    assert 'collides' in reasons['a_b']
    assert 'reserved' in reasons['whos']


def test_declarations_and_describe():
    catalog = build_catalog(TOOLS)
    out = declarations(catalog)
    assert out.startswith('from typing import Any, Literal')
    assert 'async def read(*, a: str) -> Any: ...' in out
    assert describe(catalog, 'read')['description'] == 'read tool'
    assert describe(catalog, 'nope') is None


def test_kernel_tools_can_never_be_called_from_a_cell():
    for name in ('code_start', 'code_attach', 'code_run', 'code_reset'):
        assert name not in build_catalog([tool(name, bundle='code')], 'all')
        assert 'cell' in excluded_reason(tool(name, bundle='code'))
    assert excluded_reason(tool('read')) is None
    assert 'meta' in excluded_reason(tool('x', bundle='meta'))
