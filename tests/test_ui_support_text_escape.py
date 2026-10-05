"""The ASCII fast path in escape_controls must equal the per-character reference."""
from __future__ import annotations

import random
import unicodedata

from nexus.ui_support.text import _CONTROL, escape_controls


def _reference(text: str) -> str:
    text = _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) == "Cf" else c for c in text)


def test_escape_controls_matches_the_reference_on_mixed_text():
    alphabet = ["a", "Z", " ", "\n", "\t", "\x00", "\x1b", "\x7f", "\x85", "é", "漢", "​", "‮", "﻿", "­", "😀"]
    rng = random.Random(7)
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert escape_controls(text) == _reference(text)


def test_ascii_text_is_returned_unchanged_apart_from_controls():
    assert escape_controls("plain\ttext\nwith lines") == "plain\ttext\nwith lines"
    assert escape_controls("a\x1b[31mb") == "a\\x1b[31mb"
