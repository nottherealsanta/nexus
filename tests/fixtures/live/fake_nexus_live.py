#!/usr/bin/env python3
"""A tiny stand-in for the ``nexus-live`` helper (JSONL on stdio), for UI tests.

``FAKE_LIVE_MODE``: ``ok`` (default), ``silent`` (never answers ``offer``), ``close_on_offer``
(``closed`` with ``audio_error``), ``flood`` (an over-long line before the offer), ``crash_after_ready``.
It records every command it receives to ``FAKE_LIVE_LOG`` when set.
"""
import json
import os
import sys

mode = os.environ.get("FAKE_LIVE_MODE", "ok")
log = os.environ.get("FAKE_LIVE_LOG")


def emit(**event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        command = json.loads(line)
    except ValueError:
        continue
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(command) + "\n")
    kind = command.get("type")
    if kind == "offer":
        if mode == "silent":
            continue
        if mode == "close_on_offer":
            emit(type="error", message="no input device: permission denied")
            emit(type="closed", reason="audio_error")
            break
        if mode == "flood":
            sys.stdout.write("x" * 400_000 + "\n")
            sys.stdout.flush()
        emit(type="offer", sdp="v=0\r\nfake-offer")
    elif kind == "answer":
        emit(type="ready")
        emit(type="phase", phase="speaking")
        emit(type="levels", **{"in": 0.25, "out": 0.5})
        emit(type="dropped", frames=3)
        emit(type="dc_error", message="boom")
        if mode == "crash_after_ready":
            sys.exit(3)
    elif kind == "mute":
        emit(type="levels", **{"in": 0.0, "out": 0.0})
    elif kind == "stop":
        emit(type="closed", reason="stop")
        break
else:
    emit(type="closed", reason="stdin_eof")
