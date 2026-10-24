"""Authority-intersected callable catalogs (plan 03 section 3.4, 3.11; kernel plan §3.6).

``build_catalog`` never raises: names that are unsafe identifiers, reserved, or
collide after ``-`` to ``_`` normalisation are skipped and listed in
``Catalog.skipped`` so the surface can show why a tool is not callable. The
kernel tools build one catalog per iteration (``expose = "all"``, the superset a
named tool may come from) and one capped by ``[code_mode] expose`` (the ceiling
for implicit attachment). ``excluded_reason`` explains a name that is in neither.
"""
from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase

from ..names import tool_rule_names
from .stubs import HEADER, identifier, stub

#: The four kernel tools (plan §3.1) can never be called from a cell. ``code`` is the
#: removed one-shot tool; a tool by that name is still never callable.
CODE_TOOLS = ('code_start', 'code_attach', 'code_run', 'code_reset')
EXCLUDED = frozenset({'code', *CODE_TOOLS, 'subagent', 'subagent_continue',
                      'question', 'skill',
                      'McpCall', 'McpSearch', 'mcp_call', 'mcp_search'})
RESERVED = frozenset({'search_tools', 'describe_tool', 'whos', 'ToolError', 'llm',
                      'targets', 'context_meta', 'context_messages',
                      'context_tool_calls', 'context_search', 'context_report'})
_EXCLUDED_BUNDLES = ('meta', 'manager')
BUILTIN_NAMESPACE = 'built-in'


class Catalog(Mapping):
    """Immutable ``callable name -> tool`` mapping plus the skip list."""

    __slots__ = ('_tools', 'skipped')

    def __init__(self, tools, skipped=()):
        self._tools = dict(tools)
        self.skipped = tuple(skipped)  # ((tool name, reason), ...)

    def __getitem__(self, key):
        return self._tools[key]

    def __iter__(self):
        return iter(self._tools)

    def __len__(self):
        return len(self._tools)


def build_catalog(tools, expose='read_only', *, read_only=False):
    selected = {}
    skipped = []
    for tool in tools:
        spec = tool.spec
        if spec.name in EXCLUDED or spec.bundle in _EXCLUDED_BUNDLES:
            continue
        if (read_only or expose == 'read_only') and spec.mutates:
            continue
        if isinstance(expose, (list, tuple)) and not any(
            fnmatchcase(alias, p) for p in expose for alias in tool_rule_names(spec.name)
        ):
            continue  # ``expose = ["bash"]`` still exposes ``shell``
        if isinstance(expose, (list, tuple)) and read_only and spec.mutates:
            continue
        name = spec.name.replace('-', '_')
        if not identifier(name):
            skipped.append((spec.name, 'not a valid identifier'))
        elif name in RESERVED:
            skipped.append((spec.name, 'reserved name'))
        elif name in selected:
            skipped.append((spec.name, f'collides with {selected[name].spec.name}'))
        else:
            selected[name] = tool
    return Catalog(selected, skipped)


def declarations(catalog):
    return HEADER + '\n' + '\n'.join(
        stub(name, tool.spec.input_schema) for name, tool in catalog.items())


def describe(catalog, name):
    tool = catalog.get(name)
    if tool is None:
        return None
    return {'name': name, 'description': tool.spec.description,
            'stub': stub(name, tool.spec.input_schema)}


def namespace(name):
    """``mcp:<server>`` for ``mcp__<server>__tool`` names, else ``built-in``."""
    if name.startswith('mcp__'):
        server, sep, _ = name[5:].partition('__')
        if sep and server:
            return f'mcp:{server}'
    return BUILTIN_NAMESPACE


EXCLUDED_REASON = 'drives the loop, the user or the agent tree: call it directly, not from a cell'


def excluded_reason(tool):
    """Why ``tool`` (a registered tool) is never callable from a cell, else ``None``."""
    spec = tool.spec
    if spec.name in EXCLUDED:
        return EXCLUDED_REASON
    if spec.bundle in _EXCLUDED_BUNDLES:
        return f'the {spec.bundle} bundle is never callable from a cell'
    return None
