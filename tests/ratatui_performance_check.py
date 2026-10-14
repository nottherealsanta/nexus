"""Synthetic controlling-PTY trace: 2,000 turns, 300 wheels and 50 patches/s.

Build the release binary first. Produces ignored artifacts/ratatui-parity timings;
this measures the native client, not Python projection or a live provider.
"""
from __future__ import annotations

import hashlib
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


ROWS, COLS = 50, 200


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


ACTIVITY_MODES = {
    # mode -> (tui.json content, extra env). Appearance is read from $HOME/.config/nexus/tui.json
    # (rust/tui/src/config.rs); NO_COLOR / NEXUS_ASCII=1 also disable animation.
    "live": ({"animate_activity": True}, {}),
    "static": ({"animate_activity": False}, {}),
    "ascii": ({"animate_activity": True}, {"NEXUS_ASCII": "1"}),
}


def activity_main(mode, binary):
    """Opt-in: same running fixture, no input, 3 s, with animations on vs off (one process per mode)."""
    import re
    import tempfile
    if mode not in ACTIVITY_MODES:
        sys.exit(f"error: NEXUS_TUI_PERF_ACTIVITY must be one of {sorted(ACTIVITY_MODES)} (got {mode!r})")
    prefs, extra = ACTIVITY_MODES[mode]
    duration = 3.0
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".config/nexus").mkdir(parents=True)
        (home / ".config/nexus/tui.json").write_text(json.dumps(prefs))
        artifact = Path(tmp) / "trace.log"
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (ROWS, COLS))
        setup = "import os,fcntl,termios;os.setsid();fcntl.ioctl(2,termios.TIOCSCTTY,0);os.execv(" + repr(str(binary)) + ",['nexus-ratatui'])"
        env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "NEXUS_ASCII")}
        env.update(HOME=str(home), NEXUS_TUI_TRACE="1", NEXUS_TUI_TRACE_FILE=str(artifact), TERM="xterm-256color", **extra)
        process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave, env=env)
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
        print(f"binary: {binary}\nharness sha256: {sha256(__file__)}\nmode: {mode} prefs={prefs} env={extra}\n"
              f"fixture: running status + running tool_group + Thinking + live bar active; no input for {duration:.0f}s", flush=True)
        try:
            blocks = [
                {"id": "u", "rev": "1", "kind": "user", "title": "Inspect the parser", "text": "Inspect the parser", "gap": 1, "status": "completed"},
                {"id": "t", "rev": "1", "kind": "markdown", "title": "", "text": "Thinking", "gap": 1, "status": "running"},
                {"id": "g", "rev": "1", "kind": "tool_group", "title": "", "text": "Read nexus/parse.py", "gap": 1, "status": "running", "count": 1},
            ]
            snapshot = {"schema": 2, "revision": 1, "blocks_from": 0, "blocks": blocks, "status": "running", "agent": "build",
                        "live": {"active": True, "state": "active", "phase": "listening"}}
            process.stdin.write((json.dumps(snapshot, separators=(",", ":")) + "\n").encode())
            process.stdin.flush()
            time.sleep(1)  # settle the first frame
            before = cpu()
            started = time.monotonic()
            time.sleep(duration)
            elapsed = time.monotonic() - started
            used = cpu() - before
            os.write(master, b"\x11")
            process.wait(timeout=5)
            trace = artifact.read_text() if artifact.exists() else ""
            print(trace)
            draw = re.search(r"^draw: .*p95 ([\d.]+).*\(n=(\d+)\)", trace, re.M)
            print(f"activity[{mode}] CPU: {used / elapsed * 100:.2f}% of one core over {elapsed:.2f}s (ps time has ~10 ms resolution)")
            print("draw p95 {} ms, frames (whole run incl. 1 s settle) {}".format(*(draw.groups() if draw else ("n/a", "n/a"))))
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


def main():
    # NEXUS_TUI_PERF_ACTIVITY=live|static|ascii runs only the opt-in animated-activity idle mode.
    # NEXUS_TUI_BINARY: binary under test (e.g. a clean-HEAD build); NEXUS_TUI_TRACE_FILE: trace output path.
    binary = Path(os.environ.get("NEXUS_TUI_BINARY") or ROOT / "rust/tui/target/release/nexus-ratatui").expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        sys.exit(f"error: native binary not found or not executable: {binary}\n"
                 "build it (cargo build --release in rust/tui) or set NEXUS_TUI_BINARY")
    if os.environ.get("NEXUS_TUI_PERF_ACTIVITY"):
        return activity_main(os.environ["NEXUS_TUI_PERF_ACTIVITY"], binary)
    artifact = Path(os.environ.get("NEXUS_TUI_TRACE_FILE") or ROOT / "artifacts/ratatui-parity/native-2000-trace.log").expanduser().resolve()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.unlink(missing_ok=True)  # never mix a previous run's trace into this one
    print(f"binary: {binary}\nbinary sha256: {sha256(binary)}\nharness sha256: {sha256(__file__)}\n"
          f"trace file: {artifact}\nviewport: {ROWS}x{COLS} (rows x cols), TERM=xterm-256color, "
          "fixture: 2000 turns/6000 blocks, 50 patches/s, 100 wheels/s x300, ~16 keys/s", flush=True)
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (ROWS, COLS))
    setup = "import os,fcntl,termios;os.setsid();fcntl.ioctl(2,termios.TIOCSCTTY,0);os.execv(" + repr(str(binary)) + ",['nexus-ratatui'])"
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
