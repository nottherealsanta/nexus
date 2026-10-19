"""Frozen, authority-intersected callable catalogs (plan 03 section 3.4, 3.11).

``build_catalog`` never raises: names that are unsafe identifiers, reserved, or
collide after ``-`` to ``_`` normalisation are skipped and listed in
``Catalog.skipped`` so the surface can show why a tool is not callable.
``render_catalog`` produces the description tail within an inline token budget.
"""
from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase

from ...model.tokenizer import DEFAULT_TOKENIZER
from .stubs import HEADER, identifier, stub

EXCLUDED = frozenset({'code', 'subagent', 'question', 'todowrite', 'skill',
                      'McpCall', 'McpSearch', 'mcp_call', 'mcp_search'})
RESERVED = frozenset({'search_tools', 'describe_tool', 'store', 'load', 'llm',
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
        if isinstance(expose, (list, tuple)) and not any(fnmatchcase(spec.name, p) for p in expose):
            continue
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


def _tokens(text):
    return DEFAULT_TOKENIZER.count_tokens(text, code=True)


def render_catalog(catalog, *, budget_tokens, pinned=()):
    """Namespace counts, stubs (pinned first) that fit the budget, then a pointer."""
    if not catalog:
        return 'No tools are callable.'
    counts = {}
    for name in catalog:
        counts[namespace(name)] = counts.get(namespace(name), 0) + 1
    lines = [f'{ns}: {n} tool{"s" if n != 1 else ""}' for ns, n in counts.items()]
    pins = [p.replace('-', '_') for p in pinned]
    order = [n for n in dict.fromkeys(pins) if n in catalog]
    order += [n for n in catalog if n not in set(order)]
    used = _tokens('\n'.join(lines))
    shown = []
    for name in order:
        line = stub(name, catalog[name].spec.input_schema)
        cost = _tokens(line) + 1
        if used + cost > max(0, budget_tokens):
            continue  # a later, smaller stub may still fit; pinned order is kept
        used += cost
        shown.append(line)
    out = '\n'.join(lines)
    if shown:
        out += '\n\n' + '\n'.join(shown)
    more = len(catalog) - len(shown)
    if more:
        out += f'\n\n{more} more tool{"s" if more != 1 else ""}: use search_tools(query) / describe_tool(name)'
    return out
