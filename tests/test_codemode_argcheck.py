"""Static argument check of a cell against tool schemas (kernel plan §3.5 step 1; no Monty needed)."""
from __future__ import annotations

from nexus.tools.codemode.argcheck import check_cell, describe

SCHEMAS = {
    'read': {'type': 'object', 'required': ['path'],
             'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}, 'raw': {'type': 'boolean'}}},
    'grep': {'type': 'object', 'required': ['pattern'],
             'properties': {'pattern': {'type': 'string'}, 'mode': {'enum': ['files', 'lines']},
                            'ctx': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}}},
    'open_ended': {'type': 'object', 'properties': {'x': {}}},
}


def problems(source):
    return [text for _, text in check_cell(source, SCHEMAS)]


def test_valid_calls_pass():
    ok = ("import asyncio\n"
          "a = await read(path='a.py', limit=-3, raw=True)\n"
          "b = await grep(pattern='x', mode='files', ctx=None)\n"
          "c = await asyncio.gather(read(path=p) for p in paths)\n"
          "d = await open_ended(x=[1, 2])\n")
    assert problems(ok) == []


def test_unknown_missing_and_positional_arguments_are_reported_with_lines():
    found = check_cell("x = 1\nawait read(pth='a')\nawait read('a')\nawait grep(mode='files')", SCHEMAS)
    assert found == [
        (2, "read() got an unexpected argument 'pth' (expects: path, limit, raw)"),
        (2, 'read() is missing required argument: path'),
        (3, 'read() takes keyword arguments only'),
        (4, 'grep() is missing required argument: pattern'),
    ]


def test_constants_of_the_wrong_type_or_outside_an_enum_are_reported():
    found = problems("await read(path=5, limit='3', raw=1)\nawait grep(pattern='p', mode='all', ctx='x')")
    assert "read() argument 'path' must be string, got int" in found
    assert "read() argument 'limit' must be integer, got str" in found
    assert "read() argument 'raw' must be boolean, got int" in found
    assert "grep() argument 'mode' must be one of 'files', 'lines', got 'all'" in found
    assert "grep() argument 'ctx' must be integer/null, got str" in found


def test_only_certain_mistakes_are_reported():
    assert problems("p = 5\nawait read(path=p)") == []            # a variable is never judged
    assert problems("kw = {'pth': 1}\nawait read(**kw)") == []     # **kwargs
    assert problems("args = ['a']\nawait read(*args)") == []      # *args
    assert problems("def read(**kw):\n    return 1\nawait read(pth=1)") == []  # the cell rebinds the name
    assert problems("read = 3\nread(1)") == []
    assert problems("await other(pth=1)") == []                   # not an attached tool


def test_unparsable_source_is_left_to_the_sandbox():
    assert problems("await read(path=") == []
    assert problems("match x:\n    case 1: pass") == []


def test_at_most_five_problems_and_describe_builds_a_typeerror():
    found = check_cell("\n".join(f"await read(a{i}=1)" for i in range(20)), SCHEMAS)
    assert len(found) == 5
    diagnostic = describe(found)
    assert diagnostic['kind'] == 'TypeError' and diagnostic['line'] == found[0][0]
    assert diagnostic['message'].count('\n') == 4 and diagnostic['message'].startswith('line 1: read()')
