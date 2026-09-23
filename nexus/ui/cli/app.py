"""The interactive chat surface (PLAN section 14.11).

A line-mode client, not a full-screen application: the reader owns one input
line, streaming output scrolls naturally, and scrollback and copy-paste keep
working. The session switcher, transcript, status line, multiline input, slash
commands, approval first-responder, and reconnect-from-``seq`` are all here, and
all render the same pure ``view/`` model the HTTP surface will.

The app is injected with a :class:`~nexus.ui.cli.client.Client` and a reader, so
it is exercised in tests with a fake transport and a scripted reader and needs
neither a daemon nor prompt_toolkit.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import uuid
from contextlib import aclosing
from typing import Any, TextIO

from ...events import Event
from ...view import ConversationView, apply, initial_state
from . import commands
from .approve import Approver
from .client import Client, ClientError
from .keys import Reader, make_reader
from .render import TERMINAL_EVENTS, TerminalRenderer
from .stream import answer_permission, turn_events


class ChatSession:
    """One interactive session: a transcript, a cursor, and a command loop."""

    def __init__(
        self,
        client: Client,
        session: str,
        *,
        reader: Reader,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        approver: Approver | None = None,
        renderer: TerminalRenderer | None = None,
        show_thinking: bool = False,
        cursor: int = 0,
    ) -> None:
        self.client = client
        self.session = session
        self.reader = reader
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.approver = approver
        self.renderer = renderer or TerminalRenderer(
            self.stdout, stderr=self.stderr, show_thinking=show_thinking
        )
        self._views: dict[str, ConversationView] = {}
        # Seed the cursor with the session's current end so the first turn of a
        # resumed/existing session subscribes strictly after it and never
        # replays an earlier turn's terminal event.
        self._cursors: dict[str, int] = {session: max(0, int(cursor))}
        self._unread: set[str] = set()
        self._active = False
        self._closed = False
        self._permission_index = 0

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def closed(self) -> bool:
        return self._closed

    def view(self, session: str | None = None) -> ConversationView:
        name = session or self.session
        if name not in self._views:
            self._views[name] = initial_state(name)
        return self._views[name]

    def cursor(self, session: str | None = None) -> int:
        return self._cursors.get(session or self.session, 0)

    def status_line(self, session: str | None = None) -> str:
        name = session or self.session
        view = self.view(name)
        model = (view.model or {}).get("model") or "-"
        usage = view.usage
        phase = view.phase
        viewers = view.presence.viewers
        tokens = f"{usage.input_tokens}\u2191 {usage.output_tokens}\u2193"
        return (
            f"[{name}] {phase} \u00b7 {model} \u00b7 {tokens} tok "
            f"\u00b7 {viewers} viewer(s)"
        )

    # -- loop --------------------------------------------------------------

    async def run(self) -> int:
        self._banner()
        while not self._closed:
            try:
                line = await self._read_input()
            except KeyboardInterrupt:
                self._write("\n")
                continue
            except EOFError:
                break
            if not line.strip():
                continue
            parsed = commands.parse(line)
            if parsed is not None:
                try:
                    await self._handle(parsed)
                except ClientError as exc:
                    self._error(f"Error: {exc}\n")
                continue
            await self._turn(line)
        return 0

    async def _read_input(self) -> str:
        lines = [await self.reader(f"{self.session}> ")]
        while commands.is_continuation(lines[-1]):
            lines[-1] = commands.strip_continuation(lines[-1])
            try:
                lines.append(await self.reader("...> "))
            except EOFError:
                break
        return "\n".join(lines)

    async def _turn(self, content: str) -> None:
        self._active = True
        from_seq = self.cursor()
        try:
            async with aclosing(
                turn_events(self.client, self.session, content, from_seq)
            ) as events:
                async for event in events:
                    self._ingest(event)
                    if (
                        event.type == "permission.requested"
                        and self.approver is not None
                    ):
                        self._permission_index += 1
                        await answer_permission(
                            self.client,
                            self.session,
                            event.data,
                            self.approver,
                            self.stderr,
                        )
                    if event.type in TERMINAL_EVENTS:
                        break
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.client.cancel(self.session, reason="interrupted")
            raise
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                await self.client.cancel(self.session, reason="interrupted")
            self._error("\nCancelled.\n")
        except ClientError as exc:
            self._error(f"Error: {exc}\n")
        finally:
            self._active = False
            self._write(self.status_line() + "\n")

    def _ingest(self, event: Event) -> None:
        self.renderer.render(event)
        view = apply(self.view(self.session), event)
        self._views[self.session] = view
        if event.seq and event.seq > 0:
            self._cursors[self.session] = max(self.cursor(), event.seq)
        self._unread.discard(self.session)

    # -- commands ----------------------------------------------------------

    async def _handle(self, parsed: commands.ParsedCommand) -> None:
        name = parsed.name
        if name not in commands.BY_NAME:
            self._error(f"unknown command {name}; try /help\n")
            return
        handler = getattr(self, f"_cmd_{name[1:]}", None)
        if handler is None:
            self._error(f"{name} is not available\n")
            return
        await handler(parsed.args)

    async def _cmd_help(self, args: tuple[str, ...]) -> None:
        self._write(commands.help_text() + "\n")

    async def _cmd_exit(self, args: tuple[str, ...]) -> None:
        self._closed = True

    async def _cmd_new(self, args: tuple[str, ...]) -> None:
        new_id = args[0] if args else f"session-{uuid.uuid4().hex[:8]}"
        summary = await self.client.open_session(new_id)
        self._switch(new_id, _summary_last_seq(summary))
        self._write(f"started {new_id}\n")

    async def _cmd_sessions(self, args: tuple[str, ...]) -> None:
        summaries = await self.client.list_sessions()
        if args:
            match = self._match_session(summaries, args[0])
            if match is None:
                self._error(f"no session matching {args[0]!r}\n")
                return
            matched = next(
                (item for item in summaries if getattr(item, "id", "") == match),
                None,
            )
            self._switch(match, _summary_last_seq(matched))
        summaries = await self.client.list_sessions()
        for line in self._session_lines(summaries):
            self._write(line + "\n")

    def _match_session(self, summaries: list[Any], prefix: str) -> str | None:
        for summary in summaries:
            if getattr(summary, "id", "") == prefix:
                return prefix
        for summary in summaries:
            if getattr(summary, "id", "").startswith(prefix):
                return summary.id
        return None

    def _session_lines(self, summaries: list[Any]) -> list[str]:
        lines: list[str] = []
        for summary in summaries:
            sid = getattr(summary, "id", "")
            last_seq = getattr(summary, "last_seq", 0)
            if sid != self.session and last_seq > self._cursors.get(sid, 0):
                self._unread.add(sid)
            marker = ">" if sid == self.session else ("*" if sid in self._unread else " ")
            state = getattr(summary, "state", "idle")
            viewers = getattr(summary, "viewers", 0)
            title = getattr(summary, "title", "")
            lines.append(
                f"{marker} {sid} [{state}] {last_seq} seq \u00b7 {viewers}v {title}".rstrip()
            )
        return lines

    async def _cmd_model(self, args: tuple[str, ...]) -> None:
        # The model is resolved daemon-side from the layered config per turn;
        # there is no per-session override on the facade, so this command only
        # reports what is selectable and says where to change it. It never
        # repaints the status line with a model the daemon is not using.
        models = await self.client.list_models(search=args[0] if args else None)
        if not models:
            self._write("no models\n")
            return
        for model in models:
            provider = model.get("provider") or "?"
            identifier = model.get("id") or model.get("name") or "?"
            self._write(f"  {provider}/{identifier}\n")
        if args:
            self._write(
                "note: the model is set by config ([models] default); "
                "edit nexus.toml to change it\n"
            )

    async def _cmd_tools(self, args: tuple[str, ...]) -> None:
        seen: list[str] = []
        for tool in self.view().tools:
            if tool.name and tool.name not in seen:
                seen.append(tool.name)
        if not seen:
            self._write("no tools used in this transcript\n")
            return
        for name in seen:
            self._write(f"  {name}\n")

    async def _cmd_cancel(self, args: tuple[str, ...]) -> None:
        cancelled, dropped = await self.client.cancel(self.session)
        self._write(f"cancelled={cancelled} dropped={dropped}\n")

    async def _cmd_fork(self, args: tuple[str, ...]) -> None:
        at_seq = int(args[0]) if args and args[0].isdigit() else None
        summary = await self.client.fork(self.session, at_seq)
        child = getattr(summary, "id", None)
        if child:
            self._switch(child, _summary_last_seq(summary))
            self._write(f"forked to {child}\n")

    async def _cmd_export(self, args: tuple[str, ...]) -> None:
        fmt = args[0] if args else "markdown"
        content = await self.client.export(self.session, format=fmt)
        self._write(content if content.endswith("\n") else content + "\n")

    # -- helpers -----------------------------------------------------------

    def _switch(self, session: str, last_seq: int | None = None) -> None:
        self.session = session
        if last_seq is None:
            self._cursors.setdefault(session, 0)
        else:
            self._cursors[session] = max(
                self._cursors.get(session, 0), max(0, int(last_seq))
            )
        self._unread.discard(session)

    def _banner(self) -> None:
        self._write(
            f"Nexus \u00b7 session {self.session} \u00b7 /help for commands \u00b7 "
            "/exit to quit\n"
        )

    def _write(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _error(self, text: str) -> None:
        self.stderr.write(text)
        self.stderr.flush()


def _summary_last_seq(summary: Any) -> int:
    """A session summary's current end, or ``0`` when absent/unknown."""
    value = getattr(summary, "last_seq", 0)
    return int(value) if isinstance(value, int) and value > 0 else 0


async def run_chat(
    client: Client,
    *,
    session: str,
    reader: Reader | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    approver: Approver | None = None,
    theme: dict[str, str] | None = None,
    history_path: Any | None = None,
    use_prompt_toolkit: bool = True,
    handshake: bool = True,
) -> int:
    """Open the interactive prompt against ``client`` and return an exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if reader is None:
        reader = make_reader(
            use_prompt_toolkit=use_prompt_toolkit,
            history_path=history_path,
            theme=theme,
            stdout=out,
        )
    if approver is None:
        approver = Approver(reader, stderr=err)
    if handshake:
        await client.handshake()
    summary = await client.open_session(session)
    app = ChatSession(
        client,
        session,
        reader=reader,
        stdout=out,
        stderr=err,
        approver=approver,
        cursor=_summary_last_seq(summary),
    )
    return await app.run()


__all__ = ["ChatSession", "run_chat"]
