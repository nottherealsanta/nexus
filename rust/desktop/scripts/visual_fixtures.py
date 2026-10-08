#!/usr/bin/env python3
"""Write labelled, read-only visual fixtures; never connect to a daemon.

Use the real host's mock scenarios for integration. These snapshots cover
conversation, error, approval and image layouts without executing commands or requesting credentials.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import struct
import zlib


def test_pattern() -> bytes:
    width, height = 720, 360
    colors = ((39, 64, 52), (145, 202, 176), (228, 202, 152), (192, 119, 116))
    raw = b"".join(b"\0" + b"".join(bytes(colors[x // 180]) for x in range(width)) for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = {"schema": 2, "generation": 1, "composer_key": "fixture", "theme": "nexus-dark",
            "title": "Visual fixture", "agent": "build", "model": "scripted/demo",
            "breadcrumb": "Read-only visual fixture · no host connection",
            "blocks": [{"id": "intro", "kind": "markdown", "text": "## A clear view of the work\n\nEvery request, parameter, and result stays inspectable."}]}
    approval = {**base, "prompt": {"kind": "permission", "id": "fixture-approval",
                 "lines": ["Tool: bash", "Command: git push origin feature/desktop", "Workspace: /demo/nexus",
                           "This approval fixture never executes the command."],
                 "choices": [{"label": "Allow once", "value": "allow", "key": "y", "disabled": False},
                             {"label": "Allow for this session", "value": "session", "key": "s", "disabled": True},
                             {"label": "Deny", "value": "deny", "key": "n", "disabled": False}]}}
    image = {**base, "panel_title": "Attachment · color-pattern.png", "preview_image_media": "image/png",
             "preview_image": base64.b64encode(test_pattern()).decode(),
             "panel_lines": ["Name: color-pattern.png", "Type: image/png", "Dimensions: 720 × 360",
                             "Fixture only · no attachment is sent to a model"]}
    question = {**base, "prompt": {"kind": "question", "id": "fixture-question",
                "lines": ["Which direction should we explore first?"],
                "choices": [{"label": label, "value": label, "key": str(i + 1), "disabled": False}
                            for i, label in enumerate(("Architecture", "Interface", "Tests"))]}}
    conversation = {**base, "title": "Desktop visual review", "sessions_sidebar": True,
                    "details_sidebar": True,
                    "sessions": [{"id": "fixture", "title": "Desktop visual review", "workspace": "/demo/nexus", "active": True}],
                    "blocks": [{"id": "request", "kind": "user", "gap": 1,
                                "text": "Review the desktop interface and keep every parameter and output inspectable."}]}
    for i in range(12):
        conversation["blocks"].extend([
            {"id": f"reply-{i}", "kind": "markdown", "gap": 1,
             "text": f"Step {i + 1}: inspect the workspace. The reading column keeps long explanations comfortable while tool parameters and complete results remain available through labelled disclosures."},
            {"id": f"tool-{i}", "kind": "tool", "title": f"read · src/module_{i}.py",
             "status": "running" if i == 10 else "failed" if i == 11 else "completed",
             "collapsed": i != 11, "gap": 1,
             "detail": "Path: src/module_11.py\nResult: file not found" if i == 11 else "",
             "operation": {"kind": "tool_toggle", "id": f"tool-{i}"}},
        ])
    error = {**conversation, "restore": "A multiline draft remains editable.\nThe notice must stay above this frame.\nThird line of the draft.",
             "toasts": [{"id": 1, "level": "error", "title": "Request failed",
                         "body": "The selected mock scenario could not be found. Choose a supported scenario and retry. " * 5 +
                                 "Diagnostic path: /demo/" + "long-path-segment/" * 16}]}
    history = {**conversation, "title": "History scroll regression", "sessions": [],
               "sessions_sidebar": False, "details_sidebar": False, "blocks": []}
    for i in range(4):
        history["blocks"].extend([
            {"id": f"user-{i}", "kind": "user", "gap": 1,
             "title": f"Request {i + 1}: Keep the complete conversation visible.",
             "text": "User messages follow the same left edge as replies."},
            {"id": f"earlier-reply-{i}", "kind": "markdown", "gap": 1,
             "text": f"Reply {i + 1}: Earlier history remains available by scrolling upward."},
        ])
    history["blocks"].append({"id": "long-final", "kind": "markdown", "gap": 1,
                              "text": "## A long final reply\n\n" + "\n".join(
                                  f"Line {i + 1}: Scroll upward to reach the earlier requests and replies."
                                  for i in range(90))})
    tui_layout = {**conversation, "title": "Inspect the desktop", "status": "done",
                  "breadcrumb": "/private/tmp/nexus-desktop-review/sandbox/workspace › main",
                  "tabs": [{"id": "fixture", "title": "Inspect the desktop ⟦mock scenario=tool-marathon actor=main speed=12 seed=0⟧", "workspace": "/demo/nexus", "active": True},
                           {"id": "second", "title": "A longer conversation title that remains available in full", "workspace": "/demo/nexus"}],
                  "sessions": [{"id": "fixture", "title": "Inspect the desktop ⟦mock scenario=tool-marathon actor=main speed=12 seed=0⟧", "workspace": "/demo/nexus", "active": True, "sub": "4 · 2m"}],
                  "blocks": [{"id": "context", "kind": "context_header", "members": [
                      {"title": label, "counts": [count], "operation": {"kind": "context_header", "key": label}}
                      for label, count in [("System prompt", 1), ("Environment", 1), ("AGENTS.md", 1), ("Tools", 8), ("MCP", 0)]]},
                             {"id": "request", "kind": "user", "title": "Make the desktop feel like the TUI.", "text": "Keep context and complete tool output inspectable.", "number": 1, "gap": 1, "operation": {"kind": "turn_toggle", "id": "fixture-turn"}},
                             {"id": "response", "kind": "markdown", "text": "The same session, context, conversation and details hierarchy works here. Tools stay compact beneath the reply.\n\n```python\ndef inspect_workspace():\n    return \"complete context\"\n```", "gap": 1},
                             {"id": "read", "kind": "tool", "title": "Read docs/desktop.md", "status": "completed", "collapsed": True, "operation": {"kind": "tool_toggle", "id": "read"}},
                             {"id": "search", "kind": "tool", "title": "Search desktop shortcuts", "status": "running", "collapsed": True, "operation": {"kind": "tool_toggle", "id": "search"}}]}
    for name, snapshot in (("approval", approval), ("image", image), ("question", question),
                           ("conversation", conversation), ("error", error), ("history", history), ("tui-layout", tui_layout)):
        for theme in ("dark", "light"):
            themed = {**snapshot, "theme": f"nexus-{theme}"}
            (args.output / f"{name}-{theme}.json").write_text(json.dumps(themed), encoding="utf-8")
        (args.output / f"{name}.json").write_text(json.dumps(snapshot), encoding="utf-8")


if __name__ == "__main__":
    main()
