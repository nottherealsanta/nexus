"""Core-owned PTYs. Terminal data is separate from the session event log."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import secrets
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte

from . import cargo_env
from .runtime import Conflict, NotFound

MAX_TERMINALS = 16
MAX_RETAINED_TERMINALS = 64
MAX_ATTACHMENTS = 16
RING_BYTES = 1024 * 1024
MAX_INPUT = 16 * 1024
LEASE_SECONDS = 30
DEFAULT_NAME = "Terminal"
MAX_NAME = 80
MAX_CONTROL_CHARS = 4096


class BoundedByteStream(pyte.ByteStream):
    """Bound diagnostic parsing without changing raw bytes or safe replay boundaries."""

    def _parser_fsm(self):
        parser = super()._parser_fsm()
        plain = next(parser)
        count = 0
        prefix = ""
        dropping = False
        escaped = False
        while True:
            char = yield plain
            if dropping:
                osc = prefix.startswith(("\x1b]", "\x9d"))
                ended = ((char in "\x07\x9c" or escaped and char == "\\") if osc
                         else "@" <= char <= "~" or char in "\x18\x1a")
                escaped = char == "\x1b"
                if ended:
                    parser = super()._parser_fsm()
                    plain = next(parser)
                    count, prefix, dropping = 0, "", False
                continue
            was_plain = plain
            plain = parser.send(char)
            if plain:
                count, prefix = 0, ""
            else:
                if was_plain:
                    count, prefix = 0, ""
                count += 1
                if len(prefix) < 2:
                    prefix += char
                if count >= MAX_CONTROL_CHARS:
                    parser.close()
                    dropping = True
                    escaped = char == "\x1b"


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
                 rows: int, cols: int, persist: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.id = secrets.token_urlsafe(18)
        self.name = DEFAULT_NAME
        self.control_acquisitions = 0
        self.control_releases = 0
        self.output_done = asyncio.Event()
        self.project_id, self.workspace, self.creator, self.session_id = project_id, workspace, creator, session_id
        self.created = time.time()
        self.ended: float | None = None
        self.status = "running"
        self.exit_code: int | None = None
        self.signal: int | None = None
        self.rows, self.cols = rows, cols
        self.screen = Screen(cols, rows)
        self.parser = BoundedByteStream(self.screen)
        self.frames: deque[tuple[dict[str, Any], int]] = deque()
        self.retained = 0
        self.discarded = 0
        self._safe: set[int] = set()
        self._discard_to_boundary = False
        self.seq = 0
        self.attachments: dict[str, Attachment] = {}
        self.controller: str | None = None
        self.persist = persist
        self.fd, slave = os.openpty()
        try:
            self._winsize(rows, cols)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env.update(cargo_env.extra_env(workspace))
            shell = env.get("SHELL") or "/bin/sh"
            child = Path(__file__).with_name("_terminal_child.py")
            argv = [sys.executable, "-I", str(child), shell]
            self.process = subprocess.Popen(
                argv, cwd=workspace, env=env,
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True,
            )
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)
        self.pid = self.process.pid
        os.set_blocking(self.fd, False)
        self._fd_closed = False
        self._close_lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.fd, self._read)
        self.waiter = self.loop.create_task(self._wait())
        self._persist()

    def _persist(self) -> None:
        if self.persist:
            self.persist(self.metadata())

    def metadata(self) -> dict[str, Any]:
        controller = self.attachments.get(self.controller or "")
        return {"id": self.id, "name": self.name,
                "project_id": self.project_id, "workspace": self.workspace,
                "session_id": self.session_id, "creator": self.creator, "status": self.status,
                "created": self.created, "ended": self.ended, "rows": self.rows, "cols": self.cols,
                "exit_code": self.exit_code, "signal": self.signal,
                "controller": controller.client_id if controller else None,
                "control_acquisitions": self.control_acquisitions, "control_releases": self.control_releases}

    def _emit(self, kind: str, **payload: Any) -> None:
        self.seq += 1
        frame = {"version": 1, "seq": self.seq, "type": kind, **payload}
        size = len(base64.b64decode(payload["data"])) if kind == "output" else 0
        safe = self.parser._taking_plain_text is True and not self.parser.utf8_decoder.getstate()[0]
        if self._discard_to_boundary:
            self.discarded += size
            self._discard_to_boundary = not safe
        else:
            self.frames.append((frame, size))
            self.retained += size
            if safe:
                self._safe.add(self.seq)
        removed = 0
        while self.frames and (self.retained > RING_BYTES or len(self.frames) > 4096):
            while self.frames:
                old, n = self.frames.popleft()
                self.retained -= n
                removed += n
                boundary = old["seq"] in self._safe
                self._safe.discard(old["seq"])
                if boundary:
                    break
            else:
                self._discard_to_boundary = True
        self.discarded += removed
        overflowed = []
        for aid, attachment in list(self.attachments.items()):
            if attachment.queue is not None:
                try:
                    attachment.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    overflowed.append(aid)
        for aid in overflowed:
            self.detach(aid)

    def _read(self) -> bool:
        try:
            data = os.read(self.fd, 16384)
        except BlockingIOError:
            return False
        except OSError:
            data = b""
        if not data:
            self.loop.remove_reader(self.fd)
            self.output_done.set()
            return False
        self.parser.feed(data)
        before = self.discarded
        self._emit("output", data=base64.b64encode(data).decode("ascii"))
        if self.discarded != before:
            self._emit("discard", bytes=self.discarded, floor=self.floor())
        return True

    def _drain(self) -> None:
        for _ in range(256):
            if not self._read():
                break

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
        status = await asyncio.to_thread(self.process.wait)
        await self.output_done.wait()
        self.status = "exited" if self.status == "running" else self.status
        self.ended = time.time()
        if status >= 0:
            self.exit_code = status
        else:
            self.signal = -status
        self._emit("exit", status=self.status, exit_code=self.exit_code, signal=self.signal)
        self._persist()

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
        attachment = self.attachments.pop(aid, None)
        if attachment and attachment.queue is not None:
            if attachment.queue.full():
                while not attachment.queue.empty():
                    attachment.queue.get_nowait()
            attachment.queue.put_nowait(None)
        if self.controller == aid:
            self.controller = None
            self.control_releases += 1
            self._persist()
            self._emit("control", controller=None)

    def control(self, aid: str, action: str) -> None:
        attachment = self.attachment(aid)
        if not attachment.connected:
            raise Conflict("attachment stream is not connected")
        if action == "acquire":
            if self.controller not in (None, aid):
                raise Conflict("terminal already has a controller")
            if self.controller != aid:
                self.control_acquisitions += 1
            self.controller = aid
        elif action == "release":
            if self.controller != aid:
                raise Conflict("not the controller")
            self.controller = None
            self.control_releases += 1
        else:
            raise ValueError("invalid control action")
        self._persist()
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
        self._persist()

    async def close(self) -> None:
        async with self._close_lock:
            await self._close()

    async def _close(self) -> None:
        if self.status == "running":
            self.status = "closed"
            try:
                foreground = os.tcgetpgrp(self.fd)
            except OSError:
                foreground = self.pid
            if foreground > 0 and foreground != self.pid:
                try:
                    os.killpg(foreground, signal.SIGHUP)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.killpg(foreground, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self.waiter), 1)
            except TimeoutError:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self._drain()
                self.loop.remove_reader(self.fd)
                os.close(self.fd)
                self._fd_closed = True
                self.output_done.set()
                await self.waiter
            if foreground > 0 and foreground != self.pid:
                try:
                    if os.getsid(foreground) == self.pid:
                        os.killpg(foreground, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if not self._fd_closed:
            self._drain()
            self._fd_closed = True
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
            self.output_done.set()
        for aid in list(self.attachments):
            self.detach(aid)


def dimensions(rows: int, cols: int) -> None:
    if type(rows) is not int or type(cols) is not int or not (1 <= rows <= 200 and 1 <= cols <= 500):
        raise ValueError("rows must be 1..200 and cols 1..500")


class TerminalService:
    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.terminals: dict[str, Terminal] = {}
        self.retired: dict[str, dict[str, Any]] = {}
        self.db = db
        if db is not None:
            db.execute("create table if not exists terminal_resource (id text primary key, metadata text not null)")
            for terminal_id, raw in db.execute("select id, metadata from terminal_resource").fetchall():
                metadata = json.loads(raw)
                if metadata["status"] == "running":
                    metadata.update(status="ended", reason="core_restart", ended=time.time())
                if metadata.pop("background_job_id", None) is not None:
                    # Linked to an agent bash job by an older build; the dock is user-only now.
                    metadata["status"] = "closed"
                    metadata["ended"] = metadata.get("ended") or time.time()
                for key in ("turn_id", "tool_call_id"):
                    metadata.pop(key, None)
                metadata["controller"] = None
                db.execute("update terminal_resource set metadata=? where id=?", (json.dumps(metadata), terminal_id))
            db.commit()

    def save(self, metadata: dict[str, Any]) -> None:
        if self.db is not None:
            self.db.execute("insert or replace into terminal_resource values (?, ?)",
                            (metadata["id"], json.dumps(metadata)))
            self.db.commit()

    def list(self, project_id: str) -> list[dict[str, Any]]:
        if self.db is None:
            return ([t.metadata() for t in self.terminals.values() if t.project_id == project_id]
                    + [m for m in self.retired.values() if m["project_id"] == project_id])
        rows = [row for (raw,) in self.db.execute("select metadata from terminal_resource")
                if (row := json.loads(raw))["project_id"] == project_id]
        return [self.metadata(row["id"]) for row in rows]

    def metadata(self, terminal_id: str) -> dict[str, Any]:
        if terminal_id in self.terminals:
            return self.terminals[terminal_id].metadata()
        if terminal_id in self.retired:
            return self.retired[terminal_id]
        if self.db is not None:
            row = self.db.execute("select metadata from terminal_resource where id=?", (terminal_id,)).fetchone()
            if row:
                metadata = json.loads(row[0])
                metadata.setdefault("name", DEFAULT_NAME)
                return metadata
        raise NotFound("unknown terminal")

    def rename(self, terminal_id: str, name: Any) -> dict[str, Any]:
        if not isinstance(name, str):
            raise TypeError("invalid terminal name")
        name = name.strip()
        if not name or len(name) > MAX_NAME or any(unicodedata.category(c).startswith("C") for c in name):
            raise ValueError("terminal name must be 1..80 characters without control characters")
        metadata = self.metadata(terminal_id)
        if terminal_id in self.terminals:
            term = self.terminals[terminal_id]
            term.name = name
            term._persist()
            term._emit("rename", name=name)
            return term.metadata()
        metadata["name"] = name
        if terminal_id in self.retired:
            self.retired[terminal_id] = metadata
        self.save(metadata)
        return metadata

    def create(self, *, project_id: str, workspace: str, creator: str, session_id: str | None,
               rows: int, cols: int) -> Terminal:
        dimensions(rows, cols)
        if sum(t.status == "running" for t in self.terminals.values()) >= MAX_TERMINALS:
            raise Conflict("terminal limit reached")
        if len(self.terminals) >= MAX_RETAINED_TERMINALS:
            candidates = [t for t in self.terminals.values()
                          if t.waiter.done() and not t.attachments and t.output_done.is_set()]
            if not candidates:
                raise Conflict("terminal retention limit reached")
            old = min(candidates, key=lambda t: t.ended or t.created)
            if not old._fd_closed:
                old.loop.remove_reader(old.fd)
                os.close(old.fd)
                old._fd_closed = True
            metadata = old.metadata()
            self.save(metadata)
            if self.db is None:
                self.retired[old.id] = metadata
                if len(self.retired) > MAX_RETAINED_TERMINALS:
                    self.retired.pop(next(iter(self.retired)))
            del self.terminals[old.id]
        if not Path(workspace).is_dir():
            raise ValueError("workspace does not exist")
        term = Terminal(project_id=project_id, workspace=workspace, creator=creator,
                        session_id=session_id, rows=rows, cols=cols, persist=self.save)
        self.terminals[term.id] = term
        return term

    async def dismiss(self, terminal_id: str) -> dict[str, Any]:
        """Close a resource for good: terminate it if running, and mark an ended one `closed`, which hides it."""
        if terminal_id in self.terminals:
            term = self.terminals[terminal_id]
            await term.close()
            if term.status in ("exited", "ended"):
                term.status = "closed"
                term._persist()
            return term.metadata()
        metadata = dict(self.metadata(terminal_id))
        if metadata["status"] != "closed":
            metadata["status"] = "closed"
            if terminal_id in self.retired:
                self.retired[terminal_id] = metadata
            self.save(metadata)
        return metadata

    def get(self, terminal_id: str) -> Terminal:
        try:
            return self.terminals[terminal_id]
        except KeyError as exc:
            raise NotFound("unknown terminal") from exc

    async def shutdown(self) -> None:
        await asyncio.gather(*(t.close() for t in self.terminals.values()))
