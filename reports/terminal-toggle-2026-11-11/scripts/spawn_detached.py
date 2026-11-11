#!/usr/bin/env python3
"""Start a command in its own session so the capture shell does not wait for it."""
from __future__ import annotations

import argparse
import os
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pidfile", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("no command")
    log = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(
            command,
            cwd=args.cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log)
    with open(args.pidfile, "w") as handle:
        handle.write(str(child.pid))
    print(child.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
