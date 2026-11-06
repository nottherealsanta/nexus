"""Core-owned PTYs. Terminal data is separate from the session event log."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import os
import pty
import secrets
import signal
import struct
import termios
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte

from .runtime import Conflict, NotFound

MAX_TERMINALS = 16
MAX_ATTACHMENTS = 16
RING_BYTES = 1024 * 1024
MAX_INPUT = 16 * 1024
LEASE_SECONDS = 30


class Screen(pyte.Screen):
    """pyte screen with the two compatibility fixes identified in the PTY spike."""

    def __init__(self, columns: int, lines: int) -> None:
        super().__init__(columns, lines)
        self._normal: tuple[Any, Any] | None = None

    def select_graphic_rendition(self, *attrs: int, private: bool = False) -> None:
        if not private:
            super().select_graphic_rendition(*attrs)

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and any(m in (47, 1047, 1049) for m in modes):
            if self._normal is None:
                self._normal = (self.buffer, self.cursor)
                self.buffer = pyte.screens.defaultdict(
                    lambda: pyte.screens.defaultdict(lambda: self.default_char))
                self.cursor = pyte.screens.Cursor(0, 0)
                self.erase_in_display(2)
            modes = tuple(m for m in modes if m not in (47, 1047, 1049))
        if modes:
            super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and any(m in (47, 1047, 1049) for m in modes):
            if self._normal is not None:
                self.buffer, self.cursor = self._normal
                self._normal = None
                self.dirty.update(range(self.lines))
            modes = tuple(m for m in modes if m not in (47, 1047, 1049))
        if modes:
            super().reset_mode(*modes, **kwargs)


@dataclass
class Attachment:
    client_id: str
    created: float
    connected: bool = False
    input_seq: int = 0
    digest: str = ""
    queue: asyncio.Queue[dict[str, Any] | None] | None = None


class Terminal:
    def __init__(self, *, project_id: str, workspace: str, creator: str, session_id: str | None,
                 rows: int, cols: int) -> None:
        self.id = secrets.token_urlsafe(18)
        self.project_id, self.workspace, self.creator, self.session_id = project_id, workspace, creator, session_id
        self.created = time.time()
        self.ended: float | None = None
        self.status = "running"
        self.exit_code: int | None = None
        self.signal: int | None = None
        self.rows, self.cols = rows, cols
        self.screen = Screen(cols, rows)
        self.parser = pyte.ByteStream(self.screen)
        self.frames: deque[tuple[dict[str, Any], int]] = deque()
        self.retained = 0
        self.discarded = 0
        self.seq = 0
        self.attachments: dict[str, Attachment] = {}
        self.controller: str | None = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(workspace)
                os.environ["TERM"] = "xterm-256color"
                shell = os.environ.get("SHELL") or "/bin/sh"
                os.execl(shell, shell, "-i")
            except (OSError, ValueError):
                os._exit(127)
        os.set_blocking(self.fd, False)
        self._winsize(rows, cols)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.fd, self._read)
        self.waiter = self.loop.create_task(self._wait())

    def metadata(self) -> dict[str, Any]:
        controller = self.attachments.get(self.controller or "")
        return {"id": self.id, "project_id": self.project_id, "workspace": self.workspace,
                "session_id": self.session_id, "creator": self.creator, "status": self.status,
                "created": self.created, "ended": self.ended, "rows": self.rows, "cols": self.cols,
                "exit_code": self.exit_code, "signal": self.signal,
                "controller": controller.client_id if controller else None}

    def _emit(self, kind: str, **payload: Any) -> None:
        self.seq += 1
        frame = {"version": 1, "seq": self.seq, "type": kind, **payload}
        size = len(base64.b64decode(payload["data"])) if kind == "output" else 0
        self.frames.append((frame, size))
        self.retained += size
        removed = 0
        while self.frames and (self.retained > RING_BYTES or len(self.frames) > 4096):
            _, n = self.frames.popleft()
            self.retained -= n
            removed += n
        self.discarded += removed
        for attachment in list(self.attachments.values()):
            if attachment.queue is not None:
                try:
                    attachment.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # A disconnected slow consumer must reconnect and resync.
                    while not attachment.queue.empty():
                        attachment.queue.get_nowait()
                    attachment.queue.put_nowait(None)

    def _read(self) -> None:
        try:
            data = os.read(self.fd, 16384)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.loop.remove_reader(self.fd)
            return
        self.parser.feed(data)
        before = self.discarded
        self._emit("output", data=base64.b64encode(data).decode("ascii"))
        if self.discarded != before:
            self._emit("discard", bytes=self.discarded, floor=self.floor())

    def floor(self) -> int:
        return self.frames[0][0]["seq"] if self.frames else self.seq + 1

    def checkpoint(self) -> dict[str, Any]:
        return {"version": 1, "seq": self.seq, "type": "checkpoint", "rows": self.rows,
                "cols": self.cols, "display": self.screen.display,
                "cursor": {"x": self.screen.cursor.x, "y": self.screen.cursor.y},
                "alternate": self.screen._normal is not None,
                "limitations": ["cell attributes, modes, hidden buffer and parser state are not serialized"],
                "discarded": self.discarded}

    async def _wait(self) -> None:
        _, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        self.status = "exited" if self.status == "running" else self.status
        self.ended = time.time()
        if os.WIFEXITED(status):
            self.exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.signal = os.WTERMSIG(status)
        self._emit("exit", status=self.status, exit_code=self.exit_code, signal=self.signal)

    def attach(self, client_id: str) -> str:
        self.expire()
        if len(self.attachments) >= MAX_ATTACHMENTS:
            raise Conflict("too many terminal attachments")
        aid = secrets.token_urlsafe(18)
        self.attachments[aid] = Attachment(client_id, time.monotonic())
        return aid

    def expire(self) -> None:
        for aid, attachment in list(self.attachments.items()):
            if not attachment.connected and time.monotonic() - attachment.created > LEASE_SECONDS:
                self.detach(aid)

    def attachment(self, aid: str) -> Attachment:
        self.expire()
        if aid not in self.attachments:
            raise Conflict("attachment expired or disconnected")
        return self.attachments[aid]

    def detach(self, aid: str) -> None:
        if self.controller == aid:
            self.controller = None
            self._emit("control", controller=None)
        attachment = self.attachments.pop(aid, None)
        if attachment and attachment.queue:
            try:
                attachment.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def control(self, aid: str, action: str) -> None:
        attachment = self.attachment(aid)
        if not attachment.connected:
            raise Conflict("attachment stream is not connected")
        if action == "acquire":
            if self.controller not in (None, aid):
                raise Conflict("terminal already has a controller")
            self.controller = aid
        elif action == "release":
            if self.controller != aid:
                raise Conflict("not the controller")
            self.controller = None
        else:
            raise ValueError("invalid control action")
        self._emit("control", controller=self.metadata()["controller"])

    def require_control(self, aid: str) -> Attachment:
        attachment = self.attachment(aid)
        if not attachment.connected or self.controller != aid or self.status != "running":
            raise Conflict("terminal is not controlled by this attachment")
        return attachment

    def input(self, aid: str, seq: int, data: bytes) -> None:
        attachment = self.require_control(aid)
        if not data or len(data) > MAX_INPUT:
            raise ValueError("input must be between 1 and 16384 bytes")
        digest = hashlib.sha256(data).hexdigest()
        if seq == attachment.input_seq and seq > 0 and digest == attachment.digest:
            return
        if seq != attachment.input_seq + 1:
            raise Conflict("invalid or reused input sequence")
        try:
            written = os.write(self.fd, data)
        except OSError as exc:
            self.detach(aid)
            raise Conflict("input outcome unknown; attachment revoked") from exc
        if written != len(data):
            self.detach(aid)
            raise Conflict("partial input; attachment revoked")
        attachment.input_seq, attachment.digest = seq, digest

    def _winsize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, aid: str, rows: int, cols: int) -> None:
        self.require_control(aid)
        dimensions(rows, cols)
        self._winsize(rows, cols)
        self.rows, self.cols = rows, cols
        self.screen.resize(lines=rows, columns=cols)
        self._emit("resize", rows=rows, cols=cols)

    async def close(self) -> None:
        if self.status == "running":
            self.status = "closed"
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self.waiter), 1)
            except TimeoutError:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.waiter
        self.loop.remove_reader(self.fd)
        try:
            os.close(self.fd)
        except OSError:
            pass
        for aid in list(self.attachments):
            self.detach(aid)


def dimensions(rows: int, cols: int) -> None:
    if type(rows) is not int or type(cols) is not int or not (1 <= rows <= 200 and 1 <= cols <= 500):
        raise ValueError("rows must be 1..200 and cols 1..500")


class TerminalService:
    def __init__(self) -> None:
        self.terminals: dict[str, Terminal] = {}

    def create(self, *, project_id: str, workspace: str, creator: str, session_id: str | None,
               rows: int, cols: int) -> Terminal:
        dimensions(rows, cols)
        if len(self.terminals) >= MAX_TERMINALS:
            raise Conflict("terminal limit reached")
        if not Path(workspace).is_dir():
            raise ValueError("workspace does not exist")
        term = Terminal(project_id=project_id, workspace=workspace, creator=creator,
                        session_id=session_id, rows=rows, cols=cols)
        self.terminals[term.id] = term
        return term

    def get(self, terminal_id: str) -> Terminal:
        try:
            return self.terminals[terminal_id]
        except KeyError as exc:
            raise NotFound("unknown terminal") from exc

    async def shutdown(self) -> None:
        await asyncio.gather(*(t.close() for t in self.terminals.values()))
