"""A fake ``claude`` executable for offline worker tests; never runs the real CLI.

The fake answers ``--version``/``--help``, records its argv/cwd/tty state and
every byte written to its stdin (``stdin.log``), writes a Claude-shaped JSONL
transcript, and fires the hook command found in the ``--settings`` file with a
realistic stdin payload. Its behaviour comes from ``fake.json`` next to it
(the worker filters the environment, so env vars cannot carry it).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = r'''#!%(python)s
import json, os, re, subprocess, sys, threading, time, tty
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
HELP = ("--restricted --tools --strict-mcp-config --mcp-config --setting-sources --settings "
        "--session-id --permission-mode manual acceptEdits plan auto dontAsk --no-chrome "
        "--allowedTools --disallowedTools --disable-slash-commands")
if args == ["--version"]:
    print("2.1.296 (Claude Code)"); sys.exit()
if args == ["--help"]:
    print(HELP); sys.exit()
cfg = json.load(open(os.path.join(here, "fake.json")))
mode = cfg["mode"]
with open(os.path.join(here, "argv.json"), "w") as f:
    json.dump({"args": args, "cwd": os.getcwd(), "pid": os.getpid(), "tty": all(os.isatty(fd) for fd in (0, 1, 2))}, f)
with open(os.path.join(here, "pid"), "w") as f:
    f.write(str(os.getpid()))
tty.setraw(0)
def reader():
    while True:
        try:
            data = os.read(0, 1024)
        except OSError:
            return
        if not data:
            return
        with open(os.path.join(here, "stdin.log"), "ab") as f:
            f.write(data)
threading.Thread(target=reader, daemon=True).start()
session = args[args.index("--session-id") + 1]
settings = json.load(open(args[args.index("--settings") + 1]))
munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.getcwd()))
if cfg.get("derived"):
    tdir = os.path.join(cfg["config_dir"], "projects", munged)
else:
    tdir = os.path.join(here, "transcripts")
os.makedirs(tdir, exist_ok=True)
tpath = os.path.join(tdir, session + ".jsonl")
n = [0]
def entry(kind, content, sid=None, **extra):
    n[0] += 1
    return {"type": kind, "sessionId": sid or session, "uuid": "u%%d" %% n[0], "isSidechain": False,
            "message": {"role": kind, "model": "claude-haiku-4-5", "id": "msg_1", "content": content}, **extra}
def write(*entries, partial=None):
    with open(tpath, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        if partial:
            f.write(partial)
def hook(event, **extra):
    value = {"session_id": session, "hook_event_name": event, "cwd": os.getcwd(), **extra}
    command = settings["hooks"][event][0]["hooks"][0]["command"]
    subprocess.run(command, shell=True, input=json.dumps(value).encode(), check=True)
def text(t):
    return entry("assistant", [{"type": "text", "text": t}])
def tool(i, name, inp):
    return entry("assistant", [{"type": "tool_use", "id": i, "name": name, "input": inp}])
def result(i, content, err=False):
    return entry("user", [{"type": "tool_result", "tool_use_id": i, "content": content, "is_error": err}])
def start():
    if not cfg.get("derived"):
        hook("SessionStart", transcript_path=tpath)
def sleep():
    time.sleep(cfg.get("sleep", 30))
if cfg.get("dsr"):
    os.write(1, b"\x1b[6n")
    time.sleep(0.3)
if mode == "success":
    start()
    if cfg.get("touch"):
        open(os.path.join(os.getcwd(), cfg["touch"]), "w").write("changed by claude")
    write(text("Looking around."), tool("t1", "Read", {"file_path": "a.py"}),
          result("t1", [{"type": "text", "text": "print(1)"}]))
    time.sleep(0.2)
    write(text("All done."))
    hook("Stop", transcript_path=None if cfg.get("derived") else tpath, last_assistant_message="All done.")
    sleep()
elif mode == "multi":
    start()
    write(text("first"), tool("t1", "Grep", {"pattern": "x"}), result("t1", "no matches"), text("second"),
          tool("t2", "Bash", {"command": "ls"}), result("t2", "denied", True), text("third"))
    hook("Stop", transcript_path=tpath, last_assistant_message="third")
    sleep()
elif mode == "stale":
    start()
    write(entry("assistant", [{"type": "text", "text": "FOREIGN TEXT"}], sid="other-session"),
          text("mine"), partial='{"type": "assistant", "sessionId": "' + session + '", "mess')
    time.sleep(0.4)
    with open(tpath, "a") as f:
        f.write('age": {"role": "assistant", "content": [{"type": "text", "text": "late line"}]}}\n')
    time.sleep(0.4)
    hook("Stop", transcript_path=tpath, last_assistant_message="late line")
    sleep()
elif mode == "big":
    start()
    def prose(n):  # non-secret text: long single-character runs are redacted as tokens
        return ("The quick brown fox jumps over the lazy dog. " * (n // 45 + 1))[:n]
    write(tool("t1", "Read", {"file_path": "big", "extra": prose(9000)}), result("t1", prose(20000)),
          text(prose(70000)))
    hook("Stop", transcript_path=tpath, last_assistant_message=prose(70000))
    sleep()
elif mode == "echo":
    # The TUI echoes the assignment, boxed and wrapped, before the session starts.
    prompt = args[-1]
    lines = [prompt[i:i + 30] for i in range(0, len(prompt), 30)]
    os.write(1, ("\r\n".join("│ " + line for line in lines) + "\r\n").encode())
    time.sleep(0.6)
    start()
    write(text("echo ok"))
    hook("Stop", transcript_path=tpath, last_assistant_message="echo ok")
    sleep()
elif mode == "classifier":
    start()
    write(tool("t1", "Bash", {"command": "git push --force"}),
          result("t1", "Permission for this action was denied by the Claude Code auto mode classifier. "
                 "Reason: [Git Destructive]. Let the user decide.", True),
          text("I stopped: the push was denied."))
    hook("Stop", transcript_path=tpath, last_assistant_message="I stopped: the push was denied.")
    sleep()
elif mode == "failure":
    start()
    write(text("partial before failure"))
    hook("StopFailure", transcript_path=tpath, error="rate_limit sk-ant-abcdefghijklmnop1234")
    sleep()
elif mode == "hookloss":
    start()
    write(text("answer but no hook"))
    sleep()
elif mode == "exit":
    os.write(1, b"\x1b[31mboom token sk-ant-abcdefghijklmnopqrstu\x1b[0m\r\n")
    time.sleep(0.2)
    sys.exit(3)
elif mode == "trust":
    os.write(1, b"\x1b[1mAccessing workspace:\x1b[0m SECRET_PATH\r\nYes, I trust this folder\r\n")
    sleep()
elif mode == "permission":
    start()
    os.write(1, b"Do you want to proceed?\r\n \xe2\x9d\xaf 1. Yes\r\n   2. No\r\nEsc to cancel\r\n")
    sleep()
elif mode == "hang":
    start()
    write(text("working"))
    sleep()
elif mode == "stoponly":
    start()
    hook("Stop", transcript_path=tpath, last_assistant_message="from hook only")
    sleep()
'''


def install(directory: Path, **config) -> str:
    """Write the fake CLI plus its ``fake.json`` into ``directory``; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "fake-claude"
    path.write_text(SCRIPT % {"python": sys.executable})
    path.chmod(0o700)
    configure(directory, **config)
    return str(path)


def configure(directory: Path, **config) -> None:
    (directory / "fake.json").write_text(json.dumps({"mode": "success", **config}))


def invocation(directory: Path) -> dict:
    return json.loads((directory / "argv.json").read_text())


def stdin_log(directory: Path) -> bytes:
    path = directory / "stdin.log"
    return path.read_bytes() if path.exists() else b""
