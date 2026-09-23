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
from . import commands, details
from .approve import Approver
from .client import Client, ClientError, TransportClosed
from .keys import PromptToolkitReader, Reader, make_reader, stdout_patch
from .render import TERMINAL_EVENTS, TerminalRenderer, sanitize
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
        color: bool | None = None,
        cursor: int = 0,
    ) -> None:
        self.client = client
        self.session = session
        self.reader = reader
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.approver = approver
        self.renderer = renderer or TerminalRenderer(
            self.stdout, stderr=self.stderr, show_thinking=show_thinking, color=color
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
        return details.status_line(name, self.view(name))

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
        lines = [await self.reader(f"{sanitize(self.session, 60)}> ")]
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
        except TransportClosed as exc:
            self._error(f"Connection lost: {exc}\n")
            self._error("The turn may still be running; use /reconnect to resume.\n")
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
            self._error(f"unknown command {sanitize(name, 40)}; try /help\n")
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
        self._write(f"started {sanitize(new_id, 60)}\n")

    async def _cmd_sessions(self, args: tuple[str, ...]) -> None:
        summaries = await self.client.list_sessions()
        if args:
            matched = self._match_session(summaries, args[0])
            if matched is None:
                self._error(f"no session matching {args[0]!r}\n")
                return
            self._switch(matched.id, _summary_last_seq(matched))
        for line in self._session_lines(summaries):
            self._write(line + "\n")

    def _match_session(self, summaries: list[Any], prefix: str) -> Any | None:
        ids = [(getattr(s, "id", ""), s) for s in summaries]
        return next((s for i, s in ids if i == prefix),
                    next((s for i, s in ids if i.startswith(prefix)), None))

    def _session_lines(self, summaries: list[Any]) -> list[str]:
        lines: list[str] = []
        for summary in summaries:
            sid = getattr(summary, "id", "")
            last_seq = getattr(summary, "last_seq", 0)
            if sid != self.session and last_seq > self._cursors.get(sid, 0):
                self._unread.add(sid)
            marker = ">" if sid == self.session else ("*" if sid in self._unread else " ")
            state = sanitize(getattr(summary, "state", "idle"), 24)
            viewers = getattr(summary, "viewers", 0)
            title = sanitize(getattr(summary, "title", ""), 80)
            lines.append(
                f"{marker} {sanitize(sid, 60)} [{state}] {last_seq} seq \u00b7 {viewers}v {title}".rstrip()
            )
        return lines

    async def _cmd_model(self, args: tuple[str, ...]) -> None:
        # No args lists what is selectable; ``/model <ref>`` sets this session's
        # durable override (a tier name, provider/model, or bare id) and it takes
        # effect from the next turn. ``/model list [search]`` keeps the old
        # filtered listing.
        if args and args[0] == "list":
            await self._list_models(args[1] if len(args) > 1 else None)
            return
        if args:
            await self._select_model(args[0])
            return
        await self._list_models(None)
        current = await self._current_selection()
        if current:
            self._write(f"current: {current}\n")

    async def _list_models(self, search: str | None) -> None:
        models = await self.client.list_models(search=search)
        if not models:
            self._write("no models\n")
            return
        for model in models:
            provider = sanitize(model.get("provider") or "?", 40)
            identifier = sanitize(model.get("id") or model.get("name") or "?", 80)
            tier = model.get("tier")
            suffix = f" [{sanitize(tier, 24)}]" if tier else ""
            self._write(f"  {provider}/{identifier}{suffix}\n")
        if search is None:
            self._write("choose with /model <tier|provider/model|id>\n")

    async def _select_model(self, ref: str) -> None:
        result = await self.client.select_model(self.session, ref)
        provider = sanitize(getattr(result, "provider", "") or "?", 40)
        model = sanitize(getattr(result, "model", "") or "?", 80)
        tier = sanitize(getattr(result, "tier", "") or "", 24)
        # Fold the accepted selection through the reducer the stream will
        # replay, so the status bar shows the durable ``model.selected`` value,
        # not a UI-side guess. No ``seq``: it never moves the cursor.
        data = {"reference": getattr(result, "reference", ref) or ref, "provider": provider,
                "model": model, "tier": tier, "tier_source": getattr(result, "tier_source", "") or "",
                "clamped": bool(getattr(result, "clamped", False))}
        self._ingest(Event(type="model.selected", data=data, session=self.session))
        line = f"model -> {provider}/{model}"
        if tier:
            line += f" [{tier}]"
        line += " (applies next turn)\n"
        self._write(line)
        fallback = list(getattr(result, "fallback", ()) or ())
        if fallback:
            self._write(f"fallback: {', '.join(sanitize(f, 80) for f in fallback)}\n")
        self._write(self.status_line() + "\n")

    async def _current_selection(self) -> str | None:
        """The session's current model from the replayed view, or ``None``."""
        try:
            view, _seq = await self.client.state(self.session, 0)
        except ClientError:
            return None
        model = view.get("model") if isinstance(view, dict) else None
        if not isinstance(model, dict):
            return None
        provider = sanitize(model.get("provider") or "?", 40)
        identifier = sanitize(model.get("model") or "?", 80)
        tier = model.get("tier")
        return f"{provider}/{identifier}" + (f" [{sanitize(tier, 24)}]" if tier else "")

    async def _cmd_tools(self, args: tuple[str, ...]) -> None:
        seen: list[str] = []
        for tool in self.view().tools:
            if tool.name and tool.name not in seen:
                seen.append(tool.name)
        if not seen:
            self._write("no tools used in this transcript\n")
            return
        for name in seen:
            self._write(f"  {sanitize(name, 80)}\n")

    async def _cmd_details(self, args: tuple[str, ...]) -> None:
        for line in details.detail_lines(self.session, self.view()):
            self._write(line + "\n")

    async def _cmd_reconnect(self, args: tuple[str, ...]) -> None:
        # ``follow=False`` drains the durable tail and returns, so this never
        # parks the prompt waiting for a turn that is not running.
        from_seq = self.cursor()
        replayed = 0
        try:
            async with aclosing(
                self.client.stream(self.session, from_seq, follow=False)
            ) as events:
                async for event in events:
                    self._ingest(event)
                    replayed += 1
        except ClientError as exc:
            self._error(f"reconnect failed: {exc}\n")
            return
        self._write(f"reconnected from seq {from_seq} \u00b7 {replayed} new event(s)\n")
        self._write(self.status_line() + "\n")

    async def _cmd_cancel(self, args: tuple[str, ...]) -> None:
        cancelled, dropped = await self.client.cancel(self.session)
        self._write(f"cancelled={cancelled} dropped={dropped}\n")

    async def _cmd_fork(self, args: tuple[str, ...]) -> None:
        at_seq = int(args[0]) if args and args[0].isdigit() else None
        summary = await self.client.fork(self.session, at_seq)
        child = getattr(summary, "id", None)
        if child:
            self._switch(child, _summary_last_seq(summary))
            self._write(f"forked to {sanitize(child, 60)}\n")

    async def _cmd_export(self, args: tuple[str, ...]) -> None:
        fmt = args[0] if args else "markdown"
        content = await self.client.export(self.session, format=fmt)
        self._write(content if content.endswith("\n") else content + "\n")

    # -- helpers -----------------------------------------------------------

    def _switch(self, session: str, last_seq: int | None = None) -> None:
        self.session = session
        prior = self._cursors.get(session, 0)
        end = prior if last_seq is None else max(prior, max(0, int(last_seq)))
        self._cursors[session] = end
        self._unread.discard(session)

    def _banner(self) -> None:
        self._write(
            f"Nexus \u00b7 session {sanitize(self.session, 60)} \u00b7 /help for commands \u00b7 "
            "/details for status \u00b7 /exit to quit\n"
        )
        self._write(self.status_line() + "\n")

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
    color: bool | None = None,
) -> int:
    """Open the interactive prompt against ``client`` and return an exit code.

    With the optional ``cli`` extra the editor gets history and a live status
    toolbar, and streaming writes route through ``patch_stdout``.
    """
    holder: list[ChatSession] = []
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    def toolbar() -> str:
        return holder[0].status_line() if holder else ""

    if reader is None:
        reader = make_reader(
            use_prompt_toolkit=use_prompt_toolkit,
            history_path=history_path,
            theme=theme,
            stdout=None if out is sys.stdout else out,
            bottom_toolbar=toolbar,
        )

    async def boot(out: TextIO, err: TextIO) -> ChatSession:
        if handshake:
            await client.handshake()
        summary = await client.open_session(session)
        app = ChatSession(
            client,
            session,
            reader=reader,
            stdout=out,
            stderr=err,
            approver=approver if approver is not None else Approver(reader, stderr=err),
            color=color,
            cursor=_summary_last_seq(summary),
        )
        holder.append(app)
        return app

    # ``patch_stdout`` rewrites ``sys.stdout``/``sys.stderr``; take that path only
    # for the real terminal streams, so captured buffers and explicit readers
    # stay byte-for-byte deterministic.
    if isinstance(reader, PromptToolkitReader) and out is sys.stdout and err is sys.stderr:
        with stdout_patch():
            return await (await boot(sys.stdout, sys.stderr)).run()
    return await (await boot(out, err)).run()


__all__ = ["ChatSession", "run_chat"]
