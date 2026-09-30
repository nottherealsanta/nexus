"""Tests for the browser serving layer that makes Shift+Enter a newline.

Plain ``textual serve``'s xterm frontend collapses every Enter variant onto a
bare carriage return, so a served browser cannot distinguish send from newline.
``tests/browser_serve.py`` fixes that at the serving layer by injecting a small
keyboard bridge that reports Shift/Ctrl+Enter as Kitty CSI-u. These tests cover
the pieces without a browser: the template injection, the fixture submission
log, the core parsing of the sequences the bridge emits, and the core's
independence from textual-serve.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
from pathlib import Path

from textual._xterm_parser import XTermParser

from nexus.ui.tui.widgets import ChatEditor

ROOT = Path(__file__).resolve().parents[1]

#: Kitty CSI-u sequences the bridge emits for the Enter variants.
SHIFT_ENTER = "\x1b[13;2u"
CTRL_ENTER = "\x1b[13;5u"
BARE_ENTER = "\r"


def _load_browser_serve():
    spec = importlib.util.spec_from_file_location("tests_browser_serve", ROOT / "tests" / "browser_serve.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _parse(sequence: str) -> list[str]:
    parser = XTermParser()
    events = list(parser.feed(sequence)) + list(parser.tick())
    return [event.key for event in events]


def test_bridge_sequences_parse_as_modified_enter():
    # What the serving bridge sends is exactly what Textual already understands.
    assert _parse(SHIFT_ENTER) == ["shift+enter"]
    assert _parse(CTRL_ENTER) == ["ctrl+enter"]
    assert _parse(BARE_ENTER) == ["enter"]


def test_editor_newline_contract_covers_browser_and_terminal_keys():
    assert ChatEditor.NEWLINE_KEYS >= {"shift+enter", "ctrl+shift+enter", "ctrl+j"}
    assert not ChatEditor.NEWLINE_KEYS & {"enter", "ctrl+enter", "alt+enter"}


def test_injected_template_places_bridge_before_textual_js():
    module = _load_browser_serve()
    template = module._augmented_template()
    assert module._KEYBOARD_BRIDGE in template
    assert template.index(module._KEYBOARD_BRIDGE) < template.index(module._TEMPLATE_MARKER)
    # The bridge must not have displaced the original script tag.
    assert template.count(module._TEMPLATE_MARKER) == 1


def test_injected_template_targets_installed_textual_serve():
    template = _load_browser_serve()._augmented_template()
    # The injected bridge is present alongside the untouched template body.
    assert "NexusWebSocket" in template
    assert "xterm-helper-textarea" not in template  # xterm builds its own DOM
    assert "\\u001b[13;" in template  # the CSI-u enter sequence prefix
    assert "textual-terminal" in template  # original template still rendered


def test_bridge_source_sends_the_sequences_textual_parses():
    bridge = _load_browser_serve()._KEYBOARD_BRIDGE
    # The JS builds the sequence from the modifier bits; assert the literals are
    # present so a refactor cannot silently change the emitted protocol.
    assert '"\\u001b[13;"' in bridge
    assert "(modifiers + 1)" in bridge


def test_fixture_records_submissions_verbatim(tmp_path, monkeypatch):
    log = tmp_path / "submitted.jsonl"
    monkeypatch.setenv("NEXUS_VISUAL_SUBMIT_LOG", str(log))
    demo = runpy.run_path(
        str(ROOT / "tests" / "visual_tui_demo.py"), run_name="visual_demo_under_test"
    )

    demo["_record_submission"]("alpha\nbeta")
    demo["_record_submission"]("plain")

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows == ["alpha\nbeta", "plain"]


def test_core_does_not_import_textual_serve():
    # The browser adapter lives in tests/; the shipped core only knows the key
    # names. Importing the core must not pull textual-serve into the process.
    script = "import nexus.ui.tui.widgets; import sys; assert 'textual_serve' not in sys.modules"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
