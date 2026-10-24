"""Static argument check of a cell against the attached tools' schemas (plan code-mode-kernel §3.5 step 1).

The kernel runs without Monty's static type checker: it infers every variable's type
from earlier cells, so rebinding one (``x = 1`` then ``x = 2``) fails and earlier imports
are forgotten, which a REPL cannot live with. Instead this pass parses the cell with
``ast`` and checks every call of an attached tool *before any host call*: positional
arguments, unknown or missing keywords, and constant arguments of the wrong JSON type
or outside an enum. Only what is certain is reported: a name the cell itself rebinds, a
``*args`` / ``**kwargs`` call, or a non-constant value is never judged, and unparsable
source is left to Monty, which reports the ``SyntaxError``. The call itself is validated
again by the tool's own input check, so this only moves the failure ahead of side effects.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

MAX_ERRORS = 5
MAX_ENUM_SHOWN = 8


def _binds(tree) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split('.')[0] for a in node.names)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _constant(node):
    """``(True, value)`` for a literal constant argument (including a negative number)."""
    if isinstance(node, ast.Constant):
        return True, node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)):
        return True, -node.operand.value
    return False, None


def _matches(value: Any, schema: Mapping[str, Any]) -> bool:
    """Whether a constant fits ``schema``; unknown shapes accept."""
    if not isinstance(schema, Mapping):
        return True
    if 'enum' in schema and isinstance(schema['enum'], list):
        return value in schema['enum']
    if 'const' in schema:
        return value == schema['const']
    for key in ('anyOf', 'oneOf'):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return any(_matches(value, o) for o in options)
    kind = schema.get('type')
    kinds = kind if isinstance(kind, list) else ([kind] if isinstance(kind, str) else [])
    if not kinds:
        return True
    for k in kinds:
        if k == 'string' and isinstance(value, str):
            return True
        if k == 'integer' and type(value) is int:
            return True
        if k == 'number' and type(value) in (int, float):
            return True
        if k == 'boolean' and type(value) is bool:
            return True
        if k == 'null' and value is None:
            return True
        if k in ('array', 'object'):
            return False  # a scalar constant is never an array or an object
        if k not in ('string', 'integer', 'number', 'boolean', 'null'):
            return True
    return False


def _wanted(schema) -> str:
    """A short name of the types ``schema`` accepts, e.g. ``integer/null``."""
    if not isinstance(schema, Mapping):
        return 'a different type'
    kind = schema.get('type')
    if isinstance(kind, list):
        return '/'.join(str(k) for k in kind)
    if isinstance(kind, str):
        return kind
    for key in ('anyOf', 'oneOf'):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return '/'.join(dict.fromkeys(_wanted(o) for o in options))
    return 'a different type'


def _kind_name(value):
    return 'None' if value is None else type(value).__name__


def check_cell(source: str, schemas: Mapping[str, Mapping[str, Any]]) -> list[tuple[int, str]]:
    """Problems as ``(line, message)``, at most ``MAX_ERRORS``, in source order.

    ``schemas`` maps each callable name to its JSON input schema.
    """
    try:
        tree = compile(source, '<cell>', 'exec', flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    shadowed = _binds(tree)
    problems: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        name = node.func.id
        schema = schemas.get(name)
        if schema is None or name in shadowed:
            continue
        line = node.lineno
        if node.args:
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            problems.append((line, f'{name}() takes keyword arguments only'))
            continue
        if any(k.arg is None for k in node.keywords):
            continue  # **kwargs: nothing certain to say
        properties = schema.get('properties') if isinstance(schema, Mapping) else None
        if not isinstance(properties, dict):
            continue
        given = {k.arg: k.value for k in node.keywords}
        for key, value in given.items():
            if key not in properties:
                expected = ', '.join(list(properties)[:MAX_ENUM_SHOWN])
                problems.append((line, f"{name}() got an unexpected argument '{key}' (expects: {expected})"))
                continue
            is_const, constant = _constant(value)
            if is_const and not _matches(constant, properties[key]):
                sub = properties[key] if isinstance(properties[key], Mapping) else {}
                if isinstance(sub.get('enum'), list):
                    shown = ', '.join(repr(e) for e in sub['enum'][:MAX_ENUM_SHOWN])
                    problems.append((line, f"{name}() argument '{key}' must be one of {shown}, got {constant!r}"))
                else:
                    problems.append((line, f"{name}() argument '{key}' must be {_wanted(sub)}, got {_kind_name(constant)}"))
        required = schema.get('required') if isinstance(schema.get('required'), list) else []
        missing = [r for r in required if isinstance(r, str) and r not in given]
        if missing:
            problems.append((line, f"{name}() is missing required argument{'s' if len(missing) > 1 else ''}: "
                                   + ', '.join(missing)))
    problems.sort(key=lambda p: p[0])
    return problems[:MAX_ERRORS]


def describe(problems: list[tuple[int, str]]) -> dict[str, Any]:
    """A ``TypeError`` diagnostic (first problem's line) listing every problem found."""
    first = problems[0][0]
    message = '\n'.join(f'line {line}: {text}' for line, text in problems)
    return {'kind': 'TypeError', 'message': message, 'line': first, 'traceback': ''}
