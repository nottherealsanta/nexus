"""JSON Schema to Python stub declarations for the code-mode type checker.

Contract (plan 03 section 3.3, 3.11): every callable tool gets one
``async def name(*, ...) -> Any: ...`` stub. Required properties have no
default; optional ones are ``T | None = None``. Enums become ``Literal[...]``,
``type`` lists become unions, arrays keep their item type, nested objects are
``dict[str, Any]``, and anything unrepresentable is ``Any``: conversion never
rejects a schema. Untrusted schema prose (descriptions, titles, defaults) is
never copied into code; only identifiers validated here and ``repr`` of
bounded enum strings appear in the output.
"""
from __future__ import annotations

import keyword

#: Preamble every declaration block needs.
HEADER = 'from typing import Any, Literal'

_SCALARS = {'string': 'str', 'integer': 'int', 'number': 'float',
            'boolean': 'bool', 'null': 'None', 'object': 'dict[str, Any]'}
_MAX_DEPTH = 3
_MAX_ENUM = 32
_MAX_ENUM_STR = 80
_MAX_UNION = 8
_MAX_PARAMS = 64


def identifier(name):
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _literal(values):
    """``Literal[...]`` for a small enum of str/int/bool/None values, else None."""
    if not isinstance(values, list) or not 0 < len(values) <= _MAX_ENUM:
        return None
    parts = []
    for value in values:
        if value is None or isinstance(value, bool):
            parts.append(repr(value))
        elif isinstance(value, int) and abs(value) < 10**12:
            parts.append(repr(value))
        elif isinstance(value, str) and len(value) <= _MAX_ENUM_STR:
            parts.append(repr(value))
        else:
            return None
    return f'Literal[{", ".join(dict.fromkeys(parts))}]'


def _union(parts):
    parts = list(dict.fromkeys(parts))
    if 'Any' in parts or len(parts) > _MAX_UNION:
        return 'Any'
    return ' | '.join(parts)


def annotation(schema, depth=0):
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return 'Any'
    literal = _literal(schema.get('enum')) if 'enum' in schema else None
    if literal:
        return literal
    if 'const' in schema:
        literal = _literal([schema['const']])
        if literal:
            return literal
    for key in ('anyOf', 'oneOf'):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return _union([annotation(o, depth + 1) for o in options])
    kind = schema.get('type')
    if isinstance(kind, list):
        return _union([_one(k, schema, depth) for k in kind if isinstance(k, str)]) if kind else 'Any'
    if isinstance(kind, str):
        return _one(kind, schema, depth)
    return 'Any'


def _one(kind, schema, depth):
    if kind == 'array':
        items = schema.get('items')
        return f'list[{annotation(items, depth + 1)}]' if isinstance(items, dict) else 'list[Any]'
    return _SCALARS.get(kind, 'Any')


def stub(name, schema):
    if not identifier(name):
        raise ValueError('unsafe callable identifier')
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    required = set(k for k in required if isinstance(k, str)) if isinstance(required, list) else set()
    if (not isinstance(properties, dict) or len(properties) > _MAX_PARAMS
            or any(not identifier(k) for k in properties)):
        return f'async def {name}(**kwargs: Any) -> Any: ...'
    args = []
    for key, value in properties.items():
        note = annotation(value)
        if key in required:
            args.append(f'{key}: {note}')
        else:
            if note != 'Any' and 'None' not in note.split(' | '):
                note += ' | None'
            args.append(f'{key}: {note} = None')
    return f'async def {name}({"*, " if args else ""}{", ".join(args)}) -> Any: ...'
