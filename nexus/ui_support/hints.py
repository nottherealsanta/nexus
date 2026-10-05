"""Hints shown in the middle of an empty session (both surfaces).

A pure list plus a seeded pick, so the terminal shell and the browser (which
keeps an identical copy in ``ui/web/js/hints.js``) show the same kind of tip.
Hints disappear as soon as the user types; they never carry state.
"""

from __future__ import annotations

import random

#: (keys, what they do). Keys use the same spelling as the shortcut reference.
EMPTY_HINTS: tuple[tuple[str, str], ...] = (
    ("/", "chat commands"),
    ("@", "attach a workspace file to the message"),
    ("ctrl+p", "command palette"),
    ("shift+tab", "cycle the root agent"),
    ("ctrl+t", "cycle reasoning effort"),
    ("ctrl+x m", "choose a model"),
    ("ctrl+i", "inspect exactly what the model will receive"),
    ("ctrl+space", "dictate instead of typing"),
    ("ctrl+b", "show or hide sessions"),
    ("ctrl+l", "show or hide details"),
    ("ctrl+e", "open the logs drawer"),
    ("ctrl+u", "provider usage and limits"),
    ("shift+enter", "new line in the message"),
    ("ctrl+enter", "queue a message for the next turn"),
    ("alt+enter", "interrupt the running turn and send"),
    ("esc esc", "stop the running turn"),
    ("ctrl+f", "fork this session"),
    ("/attach <path>", "attach a local file or image"),
    ("ctrl+v", "paste a clipboard image"),
    ("ctrl+x ?", "every keyboard shortcut"),
)

HINT_COUNT = 4


def pick_hints(seed: object = None, count: int = HINT_COUNT) -> list[tuple[str, str]]:
    """``count`` distinct hints; the same ``seed`` (e.g. a session id) gives the same pick."""
    rng = random.Random(str(seed)) if seed is not None else random.Random()  # noqa: S311 - not security
    return rng.sample(list(EMPTY_HINTS), min(count, len(EMPTY_HINTS)))


__all__ = ["EMPTY_HINTS", "HINT_COUNT", "pick_hints"]
