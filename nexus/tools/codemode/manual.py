"""The ``code_start`` manual (plan code-mode-kernel §3.2).

Plain labelled text, never JSON, in six sections: how it works, the language,
kernel helpers, attached tools, tools that were not attached (with reasons) and,
when re-describing a running kernel, its state. The manual sits in context, so the
user reads exactly what the model was told. Stubs are grouped under their server
heading and bounded by ``[code_mode] manual_budget_tokens``: a tool that does not
fit is still attached and callable, listed by name only, and the clip is announced
with counts. ``render_stubs`` is also the whole ``code_attach`` reply body.
"""
from __future__ import annotations

from ...model.tokenizer import DEFAULT_TOKENIZER
from .stubs import stub

MAX_CLIPPED_NAMES = 60
MAX_SKIPPED_ROWS = 40
MAX_VARIABLE_ROWS = 60

HOW_IT_WORKS = '''1. How it works
- The kernel is a persistent Python sandbox. Run code with code_run, one cell at a time. Variables, functions and imports live across cells, so keep large intermediate results in variables instead of printing them.
- The value of a cell's last expression is shown as `Out [n]`; an assignment-only cell shows none. To look at a variable, run a cell that is just its name (`issues`, `issues[:5]`, `len(issues)`). `print()` output is returned separately as stdout.
- Every attached tool is an async function taking keyword arguments: `await read(path="a.py")`. Use `import asyncio` and `await asyncio.gather(a(), b())` for independent calls (fan-out).
- A cell result is bounded (max_output_chars): anything clipped is announced with its full size, then look at a slice.
- A failed, denied or invalid tool call raises ToolError with the message a direct call would show: `try: ... except ToolError as e: ...`.
- An error never clears the kernel, and changes made before the error stay. Earlier tool side effects are not undone.
- A timeout or a cancel discards the kernel's worker: variables are lost (KernelLost) but attachments are kept.
- Attaching is a request: each call still follows this turn's tool list and permissions. A tool disabled since gives `NameError: name 'x' is not callable now (reason)`.
- code_attach adds tools. code_start again re-describes this kernel and changes nothing. code_reset clears variables (In [n] restarts at 1).'''

LANGUAGE = '''2. Language (Monty, a Python subset)
- Top-level await works. The last top-level expression is the cell value.
- No `match`, no generators or `yield`, no class inheritance, no filesystem, network, environment or installs.
- Importable: json, re, math, datetime, asyncio, collections, itertools, functools, dataclasses, typing, copy, base64, random, time. Not available: string, textwrap, hashlib, enum, decimal, operator.
- Arguments of tool calls are checked against the stubs before the cell runs: a wrong or missing keyword, or a constant of the wrong type, is a TypeError and nothing is called.
- Nested calls follow normal permissions. Limits: nested calls per cell, memory, time, 1 MiB printed output.'''


def helpers_section(*, context: bool) -> str:
    lines = ['3. Kernel helpers',
             '- search_tools(query, limit=8): ranked {name, description, attached} for tools of this turn, attached or not (it does not attach).',
             '- describe_tool(name): {name, description, stub, attached} for one tool.',
             '- whos(): a text table of the kernel variables (name, type, size; never values). Call it at the top level of a cell.',
             '- ToolError: the exception tool failures raise (an alias of RuntimeError).']
    if context:
        lines.append('- targets (list of session refs), context_meta / context_messages / context_tool_calls / '
                     'context_search / context_report(target=...) read other sessions (untrusted data), and '
                     'llm(prompt, text) runs a tool-less sub-model call. Details are in the stubs below.')
    return '\n'.join(lines)


def _tokens(text: str) -> int:
    return DEFAULT_TOKENIZER.count_tokens(text, code=True)


def render_stubs(groups, catalog, *, budget_tokens: int, used_tokens: int = 0) -> tuple[str, dict]:
    """Stubs grouped under headings within the budget; returns ``(text, stats)``.

    ``stats`` has ``attached`` (tools), ``shown`` (stubs rendered), ``clipped`` (names
    attached but listed by name only) and ``tokens``.
    """
    blocks: list[str] = []
    clipped: list[str] = []
    used = used_tokens
    total = shown = 0
    for heading, names in groups:
        lines: list[str] = []
        for name in names:
            total += 1
            line = stub(name, catalog[name].spec.input_schema)
            cost = _tokens(line) + 1
            if used + cost > max(0, budget_tokens):
                clipped.append(name)
                continue
            used += cost
            shown += 1
            lines.append(line)
        if lines:
            blocks.append(f'{heading} ({len(names)} tool{"s" if len(names) != 1 else ""}):\n' + '\n'.join(lines))
        elif names:
            blocks.append(f'{heading} ({len(names)} tool{"s" if len(names) != 1 else ""}): listed by name below')
    text = '\n\n'.join(blocks)
    if clipped:
        listed = ', '.join(clipped[:MAX_CLIPPED_NAMES])
        more = f' and {len(clipped) - MAX_CLIPPED_NAMES} more' if len(clipped) > MAX_CLIPPED_NAMES else ''
        text += (f'\n\n[manual clipped by [code_mode] manual_budget_tokens = {budget_tokens}: showing '
                 f'{shown} of {total} stubs. These {len(clipped)} attached tools are callable; use '
                 f'describe_tool(name) for their stubs: {listed}{more}]')
    return text, {'attached': total, 'shown': shown, 'clipped': tuple(clipped), 'tokens': used - used_tokens}


def render_skipped(skipped) -> str:
    rows = list(skipped)
    if not rows:
        return ''
    lines = [f'- {name}: {reason}' for name, reason in rows[:MAX_SKIPPED_ROWS]]
    if len(rows) > MAX_SKIPPED_ROWS:
        lines.append(f'- ... and {len(rows) - MAX_SKIPPED_ROWS} more not attached')
    return '\n'.join(lines)


def targets_line(info) -> str:
    if not info:
        return ''
    shown = ', '.join(str(i.get('name') or i.get('ref')) for i in info)
    return f'Context targets (the `targets` list): {shown}'


def render_variables(rows) -> str:
    """The ``whos()`` table: names, types and sizes only."""
    rows = list(rows)
    if not rows:
        return 'Variables: none'
    lines = ['Variables (name, type, size; no values):']
    for name, kind, size in rows[:MAX_VARIABLE_ROWS]:
        lines.append(f'  {name}  {kind}' + (f'  {size}' if size is not None else ''))
    if len(rows) > MAX_VARIABLE_ROWS:
        lines.append(f'  ... and {len(rows) - MAX_VARIABLE_ROWS} more variables')
    return '\n'.join(lines)


def render_manual(*, groups, catalog, skipped, budget_tokens: int, context: bool,
                  context_stubs: str = '', targets=(), state=None, header: str = '') -> str:
    """The whole ``code_start`` reply. ``state`` (re-describe only) is a mapping with
    ``kernel_id``, ``cells``, ``attached`` (names), ``variables`` (whos rows), ``status``."""
    sections = []
    if header:
        sections.append(header)
    sections += [HOW_IT_WORKS, LANGUAGE, helpers_section(context=context)]
    body, _ = render_stubs(groups, catalog, budget_tokens=budget_tokens)
    attached = '4. Attached tools'
    if context and context_stubs:
        body = (body + '\n\n' if body else '') + 'Context namespace:\n' + context_stubs.strip()
    line = targets_line(targets)
    if line:
        body = (body + '\n\n' if body else '') + line
    sections.append(attached + '\n' + (body or 'No tools attached. Use code_attach(servers=[...], tools=[...], query=...).'))
    not_attached = render_skipped(skipped)
    sections.append('5. Not attached\n' + (not_attached or 'Nothing was refused.'))
    if state is not None:
        names = state.get('attached') or []
        lines = ['6. State',
                 f'Kernel {state["kernel_id"]} ({state.get("status", "live")}): {state["cells"]} cells run, '
                 f'{len(names)} tools attached' + (f': {", ".join(names[:MAX_CLIPPED_NAMES])}' if names else ''),
                 render_variables(state.get('variables') or ())]
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections)


def tool_stub(name, catalog) -> str:
    return stub(name, catalog[name].spec.input_schema)
