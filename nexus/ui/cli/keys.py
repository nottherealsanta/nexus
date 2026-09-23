"""prompt_toolkit binding and the fallback line reader (PLAN section 14.11).

prompt_toolkit is an **optional** extra (``nexus[cli]``). Nothing in this package
imports it at module scope; :func:`_load` is the single lazy import point, so
``import nexus.ui.cli`` stays cheap and works with the extra uninstalled.

When the extra is absent the interactive surface still works with
:class:`StdinReader`, a plain ``input()``-backed reader run off the event loop.
The enhanced editor (history, completion, key bindings) is a convenience, not a
requirement, and one-shot and JSONL runs never build a prompt session at all.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

#: ``read(prompt) -> str``; raises ``EOFError`` at end of input.
Reader = Callable[[str], Awaitable[str]]


class CliDependencyError(RuntimeError):
    """The interactive editor was requested but the ``cli`` extra is absent."""


def _load() -> SimpleNamespace:
    """Import prompt_toolkit lazily; the only import point in the package."""
    import prompt_toolkit
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory

    return SimpleNamespace(
        version=getattr(prompt_toolkit, "__version__", "?"),
        PromptSession=PromptSession,
        FileHistory=FileHistory,
        AutoSuggestFromHistory=AutoSuggestFromHistory,
    )


def available() -> bool:
    """Whether the optional editor is importable in this interpreter."""
    try:
        _load()
    except Exception:  # noqa: BLE001 - any import failure means "absent"
        return False
    return True


def require() -> SimpleNamespace:
    """Return the loaded editor, or raise a clear, actionable error."""
    try:
        return _load()
    except Exception as exc:
        raise CliDependencyError(
            "the interactive prompt needs the optional 'cli' extra: "
            "pip install 'nexus-harness[cli]'"
        ) from exc


def build_prompt_session(
    *,
    theme: dict[str, str] | None = None,
    history_path: Path | None = None,
    stdout: TextIO | None = None,
) -> Any:
    """Build a configured ``PromptSession``; requires the ``cli`` extra."""
    from .theme import build_style

    pt = require()
    history = pt.FileHistory(str(history_path)) if history_path else None
    return pt.PromptSession(
        history=history,
        auto_suggest=pt.AutoSuggestFromHistory(),
        style=build_style(theme),
        output=stdout,
    )


class StdinReader:
    """A blocking ``input()`` reader, offloaded so it never blocks the loop."""

    def __init__(self, stdout: TextIO | None = None) -> None:
        self.stdout = stdout if stdout is not None else sys.stdout

    async def __call__(self, prompt: str) -> str:
        if prompt:
            self.stdout.write(prompt)
            self.stdout.flush()
        return await asyncio.to_thread(input)


class PromptToolkitReader:
    """A prompt_toolkit-backed reader with history and completion."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def __call__(self, prompt: str) -> str:
        return await self.session.prompt_async(prompt)


def make_reader(
    *,
    use_prompt_toolkit: bool = True,
    history_path: Path | None = None,
    theme: dict[str, str] | None = None,
    stdout: TextIO | None = None,
) -> Reader:
    """Return the best available reader, degrading to stdin when needed."""
    if use_prompt_toolkit and available():
        session = build_prompt_session(
            theme=theme, history_path=history_path, stdout=stdout
        )
        return PromptToolkitReader(session)
    return StdinReader(stdout)


__all__ = [
    "CliDependencyError",
    "PromptToolkitReader",
    "Reader",
    "StdinReader",
    "available",
    "build_prompt_session",
    "make_reader",
    "require",
]
