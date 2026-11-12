"""Interactive Claude Code PTY + isolated hooks, referenced from nexus01 c3f18489.

Only cursor-position replies are ever written to the PTY. Stop hooks, not quiet
terminal output or process exit, establish completion. No SDK or proxy is used.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .antigravity import Emit
from .base import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_CHARS,
    MAX_PROMPT_BYTES,
    MAX_RESULT_CHARS,
    WorkerError,
    WorkerEvent,
    WorkerResult,
    child_env,
    clip,
    diagnostic_text,
    probe,
    protected_cleanup,
    stop_process,
)
from .claude_transcript import SubagentReader, TranscriptReader

RECONCILE_SECONDS = 2.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DSR = re.compile(rb"\x1b\[(\??)6n")

HOOK = r'''
import json, os, sys
from uuid import UUID
root, expected, resumed = sys.argv[1:]
try:
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024: raise ValueError("hook input too large")
    data = json.loads(raw)
    event, sid = data.get("hook_event_name"), data.get("session_id")
    if event not in ("SessionStart", "Stop", "StopFailure"): raise ValueError("event")
    if not isinstance(sid, str) or str(UUID(sid)) != sid: raise ValueError("session id")
    pin = os.path.join(root, "session")
    if resumed == "yes":
        if event == "SessionStart":
            try:
                fd = os.open(pin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as out: out.write(sid)
            except FileExistsError: pass
        with open(pin) as source: expected = source.read(100)
    if sid != expected: raise ValueError("foreign session")
    record = {"event": event, "session_id": sid}
    for key in ("transcript_path", "last_assistant_message", "error", "reason"):
        if isinstance(data.get(key), str): record[key] = data[key][:65536]
    target = os.path.join(root, event + ".json")
    temporary = target + "." + str(os.getpid()) + ".part"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out: json.dump(record, out)
    os.replace(temporary, target)
except Exception:
    sys.exit(1)
'''


@dataclass(frozen=True)
class ClaudePolicy:
    permission_mode: str = "auto"
    effort: str | None = None

    def __post_init__(self):
        if self.permission_mode not in ("auto", "dontAsk"):
            raise ValueError("Claude ZMX permits auto or dontAsk only; never bypassPermissions")
        if self.effort is not None and not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", self.effort):
            raise ValueError("invalid Claude reasoning effort")

    def fingerprint(self, model: str, workspace: Path) -> str:
        return hashlib.sha256(json.dumps({"mode": self.permission_mode, "model": model, "effort": self.effort,
                                          "directory": str(workspace), "disallowed": ["AskUserQuestion"],
                                          "settings": "inherited"}, sort_keys=True).encode()).hexdigest()

    def authority(self, workspace: Path) -> str:
        return (f"ZMX · Claude Code ran its own tools in {workspace} (--permission-mode {self.permission_mode}); "
                "CLI settings, memory, MCP servers and plugins are inherited. Nexus permission rules, tool restrictions "
                "and step limits do not apply. The CLI adds context Nexus cannot see; this is not its full model context.")


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkerError(f"ZMX state directory must be owned, non-symlink and mode 0700: {path}")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise WorkerError("ZMX state parent must be owned and not writable by other users")
    return path


def transcript_path(path: str, config_dir: Path) -> Path:
    candidate = Path(path).resolve()
    if not Path(path).is_absolute() or not candidate.is_relative_to(config_dir.resolve()) or candidate.suffix != ".jsonl":
        raise WorkerError("Claude hook transcript path is outside the CLI config directory")
    return candidate


def derived_transcript(config_dir: Path, workspace: Path, session: str) -> Path:
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    return config_dir / "projects" / munged / f"{session}.jsonl"


def _normalized(text: str) -> str:
    return re.sub(r"[\s|│┃┌┐└┘─━╭╮╰╯]+", "", text).lower()


def attention_for(terminal: str, prompt: str, replies: list[str]) -> str | None:
    text = _normalized(terminal)
    for echo in (prompt, *replies):
        if echo:
            text = text.replace(_normalized(echo), "")
    if any(s in text for s in ("yes,itrustthisfolder", "doyoutrustthefilesinthisfolder", "trustthisworkspace")):
        return "Workspace trust required. Run `claude` interactively in this directory, approve trust, then retry."
    if any(s in text for s in ("notloggedin", "pleaselogin", "signintoclaude", "loginrequired", "selectlog inmethod".replace(" ", ""))):
        return "Sign-in required. Run `claude auth login` interactively, then retry."
    if "1.yes" in text and any(s in text for s in ("doyouwanttoproceed", "allowthistool", "allowthiscommand", "permissionrequired")):
        return "Claude Code is asking for approval. Run `claude` interactively and review its permissions; Nexus approved nothing."
    if "selectthetextstyle" in text or "chooseatheme" in text:
        return "Claude Code onboarding required. Run `claude` interactively once, then retry."
    return None


class ClaudeCodeWorker:
    def __init__(self, executable: str, *, state_dir: Path, config_dir: Path | None = None,
                 environ: Mapping[str, str] | None = None):
        self.executable = executable
        self.env = child_env(environ)
        source = os.environ if environ is None else environ
        self.config_dir = config_dir or Path(source.get("CLAUDE_CONFIG_DIR") or str(Path(self.env.get("HOME", str(Path.home()))) / ".claude"))
        if "CLAUDE_CONFIG_DIR" in source:
            self.env["CLAUDE_CONFIG_DIR"] = source["CLAUDE_CONFIG_DIR"]
        self.env.update(TERM="xterm-256color", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
        self.state_dir = state_dir

    async def preflight(self) -> str:
        if os.name != "posix":
            raise WorkerError("Claude ZMX requires a POSIX PTY")
        cwd = Path(tempfile.gettempdir())
        version = await probe(self.executable, "--version", env=self.env, cwd=cwd)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
        if not match or tuple(map(int, match.groups())) < (2, 1, 296):
            raise WorkerError("Claude Code 2.1.296 or newer is required")
        help_text = await probe(self.executable, "--help", env=self.env, cwd=cwd)
        required = ("--model", "--session-id", "--resume", "--permission-mode", "--settings", "--disallowedTools")
        if any(not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) for flag in required):
            raise WorkerError("Claude Code lacks the required interactive worker flags; check `claude --help`")
        return match.group(0)

    def build_argv(self, *, prompt: str, model: str, session: str, settings: Path, policy: ClaudePolicy,
                   resume: bool, instructions: str = "") -> list[str]:
        argv = [self.executable, "--model", model, "--resume" if resume else "--session-id", session,
                "--permission-mode", policy.permission_mode, "--settings", str(settings), "--disallowedTools", "AskUserQuestion"]
        if policy.effort:
            argv += ["--effort", policy.effort]
        # Appending through a labelled user prompt works on older tested CLIs too.
        assignment = ("[Nexus assignment instructions]\n" + instructions + "\n\n[Assignment]\n" + prompt) if instructions else prompt
        return argv + ["--", assignment]

    async def run(self, *, prompt: str, model: str, workspace: Path, policy: ClaudePolicy | None = None,
                  emit: Emit | None = None, resume: str | None = None, previous_fingerprint: str | None = None,
                  previous_transcript: str | None = None, instructions: str = "") -> WorkerResult:
        policy = policy or ClaudePolicy()
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir() or not _ID.fullmatch(model):
            raise ValueError("Claude ZMX needs a workspace directory and exact model ID")
        if "\0" in prompt + instructions or len((prompt + instructions).encode()) + 100 > MAX_PROMPT_BYTES:
            raise ValueError("Claude ZMX assignment exceeds 64 KiB or contains NUL; refusing to truncate")
        fingerprint = policy.fingerprint(model, workspace)
        if resume and (str(UUID(resume)) != resume or previous_fingerprint != fingerprint):
            raise ValueError("Claude ZMX resume requires the recorded session and unchanged model/directory/permission/effort policy")
        await self.preflight()
        state = private_directory(self.state_dir)
        session = resume or str(uuid4())
        expected = transcript_path(previous_transcript, self.config_dir) if previous_transcript else derived_transcript(self.config_dir, workspace, session)
        baseline = expected.stat().st_size if resume and expected.exists() else 0
        reader = TranscriptReader(expected, session, offset=baseline)
        sidechains = SubagentReader(expected)
        notes: list[str] = []
        replies: list[str] = []
        text = ""
        usage = None
        error = attention = None
        terminal = bytearray()
        dsr = b""
        stop = None
        ended_at = None
        process = None
        master = slave = None

        async def send(event: WorkerEvent):
            nonlocal text, usage
            if event.kind == "notice" and event.data.get("text") not in notes:
                notes.append(event.data["text"])
            if event.kind == "assistant":
                parts = event.data["parts"]
                answer = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                if answer:
                    text, note = clip(answer, MAX_RESULT_CHARS)
                    replies.append(text)
                    replies[:] = replies[-200:]
                    if note:
                        notes.append("answer " + note)
                if isinstance(event.data.get("usage"), dict):
                    usage = event.data["usage"]
            if emit:
                await emit(event)

        with tempfile.TemporaryDirectory(prefix="claude-", dir=state) as run_dir:
            root = Path(run_dir)
            hook = root / "hook.py"
            hook.write_text(HOOK)
            command = shlex.join([sys.executable, "-I", str(hook), str(root), session, "yes" if resume else "no"])
            settings = root / "settings.json"
            settings.write_text(json.dumps({"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]
                                                      for event in ("SessionStart", "Stop", "StopFailure")}}))
            seen_hooks = set()
            try:
                import fcntl
                import pty
                import struct
                import termios
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.set_blocking(master, False)
                argv = self.build_argv(prompt=prompt, model=model, session=session, settings=settings, policy=policy,
                                       resume=bool(resume), instructions=instructions)
                process = await asyncio.create_subprocess_exec(*argv, cwd=workspace, env=self.env,
                                                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                slave = None
                await send(WorkerEvent("process", {"pid": process.pid, "session_id": session, "transcript_path": str(expected),
                                                    "policy_fingerprint": fingerprint}))
                while True:
                    try:
                        chunk = os.read(master, 65536)
                    except BlockingIOError:
                        chunk = b""
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if chunk:
                        terminal.extend(chunk)
                        if len(terminal) > MAX_DIAGNOSTIC_BYTES:
                            del terminal[:-MAX_DIAGNOSTIC_BYTES]
                            if "terminal diagnostics clipped" not in notes:
                                notes.append("terminal diagnostics clipped")
                        dsr += chunk
                        while match := _DSR.search(dsr):
                            os.write(master, b"\x1b[" + match[1] + b"1;1R")
                            dsr = dsr[match.end():]
                        dsr = dsr[-8:]
                    for event in ("SessionStart", "Stop", "StopFailure"):
                        path = root / f"{event}.json"
                        if event in seen_hooks or not path.exists():
                            continue
                        with path.open("rb") as source:
                            raw = source.read(256 * 1024 + 1)
                        if len(raw) > 256 * 1024:
                            raise WorkerError("oversized Claude hook record")
                        value = json.loads(raw)
                        if not isinstance(value, dict) or value.get("event") != event:
                            raise WorkerError("malformed Claude hook record")
                        actual = value.get("session_id")
                        if not isinstance(actual, str) or str(UUID(actual)) != actual:
                            raise WorkerError("invalid Claude hook session")
                        if event == "SessionStart" and resume:
                            session = actual
                        if actual != session:
                            raise WorkerError("foreign Claude hook session")
                        seen_hooks.add(event)
                        await send(WorkerEvent("wire", raw=raw))
                        supplied = value.get("transcript_path")
                        selected = transcript_path(supplied, self.config_dir) if supplied else derived_transcript(self.config_dir, workspace, session)
                        if selected != reader.path or session != reader.session:
                            reader = TranscriptReader(selected, session, offset=baseline if selected == expected else 0)
                            sidechains = SubagentReader(selected)
                        if event in ("Stop", "StopFailure"):
                            stop = value
                            ended_at = time.monotonic()
                    for event in (*reader.poll(), *sidechains.poll()):
                        await send(event)
                    if stop and stop["event"] == "StopFailure":
                        error = "Claude Code StopFailure: " + diagnostic_text(str(stop.get("error") or stop.get("reason") or "unknown failure").encode("utf-8", "replace"))
                    if not stop:
                        attention = attention_for(diagnostic_text(bytes(terminal)), argv[-1], replies)
                    if attention or error:
                        break
                    if process.returncode is not None and ended_at is None:
                        ended_at = time.monotonic()
                    if ended_at is not None and time.monotonic() - ended_at >= RECONCILE_SECONDS:
                        break
                    await asyncio.sleep(.03)
                if not stop:
                    error = error or "Claude Code exited without a Stop hook; completion is not known"
                elif stop["event"] == "Stop" and not text:
                    answer = stop.get("last_assistant_message")
                    if isinstance(answer, str):
                        text, note = clip(answer, MAX_RESULT_CHARS)
                        if note:
                            notes.append("hook answer " + note)
                    if not text:
                        attention = "Claude Code stopped without an answer. Run `claude` interactively to review its permissions."
            except (WorkerError, OSError, ValueError) as exc:
                error = str(exc)
            finally:
                if process is not None:
                    await protected_cleanup(stop_process(process))
                for fd in (master, slave):
                    if fd is not None:
                        with suppress(OSError):
                            os.close(fd)
        diagnostics, note = clip(diagnostic_text(bytes(terminal)), MAX_DIAGNOSTIC_CHARS)
        if note:
            notes.append("diagnostics " + note)
        return WorkerResult("needs_attention" if attention else "failed" if error else "completed", text,
                            policy.authority(workspace), session_id=session, usage=usage, error=attention or error,
                            diagnostics=diagnostics, notes=tuple(dict.fromkeys(notes)), transcript_path=str(reader.path),
                            policy_fingerprint=fingerprint)
