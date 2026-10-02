"""Synthetic controlling-PTY trace: 2,000 turns, 300 wheels and 50 patches/s.

Build the release binary first. Produces ignored artifacts/ratatui-parity timings;
this measures the native client, not Python projection or a live provider.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    artifact = ROOT / "artifacts/ratatui-parity/native-2000-trace.log"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (50, 200))
    setup = "import os,fcntl,termios;os.setsid();fcntl.ioctl(2,termios.TIOCSCTTY,0);os.execv(" + repr(str(ROOT / "rust/tui/target/release/nexus-ratatui")) + ",['nexus-ratatui'])"
    process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave,
        env=dict(os.environ, NEXUS_TUI_TRACE="1", NEXUS_TUI_TRACE_FILE=str(artifact), TERM="xterm-256color"))
    stop = threading.Event()
    def drain():
        while not stop.is_set():
            ready, _, _ = select.select([master, process.stdout], [], [], .05)
            for stream in ready:
                try:
                    if not os.read(stream if isinstance(stream, int) else stream.fileno(), 65536):
                        return
                except OSError:
                    return
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    def cpu():
        value = subprocess.check_output(["ps", "-p", str(process.pid), "-o", "time="], text=True).strip()
        minutes, seconds = value.rsplit(":", 1)
        return int(minutes) * 60 + float(seconds)
    def send(value):
        process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        process.stdin.flush()
    try:
        blocks = []
        for turn in range(2000):
            for kind, text in (("user", "Inspect the parser"), ("markdown", "I will inspect the parser and preserve every diagnostic."), ("tool_group", "Read nexus/parse.py · 412 lines")):
                blocks.append({"id": f"{turn}:{kind}", "rev": "1", "kind": kind, "title": text if kind == "user" else "", "text": text,
                               "gap": 1, "status": "completed", "count": 7})
        send({"schema": 2, "revision": 1, "blocks_from": 0, "blocks": blocks, "status": "idle", "agent": "build"})
        time.sleep(1)
        before_idle = cpu()
        time.sleep(1)
        idle = cpu() - before_idle
        before_stream = cpu()
        started = time.monotonic()
        next_patch, next_wheel, next_key = started, started, started
        patches = wheels = keys = 0
        while wheels < 300:
            now = time.monotonic()
            if now >= next_patch:
                patches += 1
                send({"schema": 2, "revision": patches + 1, "blocks_from": len(blocks) - 1,
                    "blocks": [{**blocks[-1], "rev": str(patches + 1), "text": f"Read parser · {patches} lines"}],
                    "status": "running", "agent": "build"})
                next_patch += .02
            if now >= next_wheel:
                wheels += 1
                os.write(master, b"\x1b[<64;30;12M")
                next_wheel += .01
            if now >= next_key:
                keys += 1
                os.write(master, b"a")
                next_key += .06
            time.sleep(.001)
        time.sleep(.05)
        elapsed = time.monotonic() - started
        streaming = cpu() - before_stream
        os.write(master, b"\x11")
        process.wait(timeout=5)
        print(artifact.read_text())
        print(f"idle CPU: {idle * 100:.2f}% of one core over 1s; streaming CPU: {streaming / elapsed * 100:.2f}% over {elapsed:.2f}s")
        print(f"sent {patches} patches, {wheels} wheels, {keys} keys; optimized binary, PTY sink")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stop.set()
        thread.join(timeout=1)
        process.stdin.close()
        process.stdout.close()
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    main()
