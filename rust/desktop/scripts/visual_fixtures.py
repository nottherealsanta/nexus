#!/usr/bin/env python3
"""Write labelled, read-only visual fixtures; never connect to a daemon.

Use the real host's mock scenarios for integration. These snapshots cover
approval and image layouts without executing commands or requesting credentials.
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
    for name, snapshot in (("approval", approval), ("image", image), ("question", question)):
        (args.output / f"{name}.json").write_text(json.dumps(snapshot), encoding="utf-8")


if __name__ == "__main__":
    main()
