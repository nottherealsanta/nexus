"""Native Manager entry: the pinned sidebar row, opening the session, and its Live route.

The manager is a dedicated per-workspace session (docs/manager.md). The host owns
every rule (disabled by default, pinned model, no role/model/fork/shell, separate
Live consent); this module only reads ``ManagerState`` for the pinned entry, calls
``ManagerOpen`` to open it, and routes Live through the ordinary ``Live`` bridge
with the manager's cross-session consent: every Live call is attached to the manager, whichever
session is on screen. Terminal and desktop share it.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict

from ...ui_support import manager as ui
from ...ui_support.prompts import pending_questions
from ...ui_support.text import escape_controls

USAGE = "Use /manager [open|live|status]"
STALE_SECONDS = 30.0  # ManagerState is read at start, on open and about this often while polling
SHOW_TTL_SECONDS = 30.0  # a manager.show is actionable this long after it was recorded
SHOWN_MEMORY = 64  # manager.show events already acted on (bounded)


class Manager:
    def __init__(self, shell):
        self.shell = shell
        self._shown: OrderedDict[tuple[str, int, str], None] = OrderedDict()
        self._show_task: asyncio.Task | None = None
        self.state = None  # the last ManagerStateResult
        self.error = ""
        self.read_at: float | None = None
        self.previous: tuple[str, str] | None = None  # (workspace, session) in front before the manager opened

    # -- state ---------------------------------------------------------------

    @property
    def session(self) -> str:
        return str(getattr(self.state, "session", "") or "")

    def is_active(self, session: str | None = None) -> bool:
        """Whether ``session`` (default: the open one) is the manager session."""
        session = self.shell.controller.session if session is None else session
        return bool(session) and session in {ui.MANAGER_SESSION_ID, self.session}

    def due(self) -> bool:
        return self.read_at is None or time.monotonic() - self.read_at >= STALE_SECONDS

    async def refresh(self) -> None:
        """Read ``ManagerState``. A failure is kept as text (an older daemon has no such command)."""
        try:
            self.state = await self.shell.client.manager_state()
            self.error = ""
        except Exception as exc:  # noqa: BLE001 - shown in the pinned entry, never fatal
            self.error = f"Manager unavailable: {escape_controls(str(exc))[:160]}"
        self.read_at = time.monotonic()

    def entry(self) -> dict:
        """The ``manager`` snapshot field: the pinned entry plus whether the open session is the manager."""
        row = ui.entry(self.state, current=self.shell.controller.session, error=self.error)
        row["active"] = row["active"] or self.is_active()
        return row

    # -- commands ------------------------------------------------------------

    async def command(self, args: tuple[str, ...]) -> None:
        action = args[0].casefold() if args else "open"
        if len(args) > 1 or action not in {"open", "live", "status"}:
            raise ValueError(USAGE)
        if action == "status":
            await self.status()
        elif action == "live":
            await self.live()
        else:
            await self.open()

    async def status(self) -> None:
        await self.refresh()
        if self.state is None:
            self.shell.flash(self.error or "Manager unavailable", "error")
            return
        self.shell.show("Manager status", ui.status_lines(self.state))

    async def open(self) -> bool:
        """Open (creating on first use) the manager session and switch to it; ``False`` if it cannot."""
        await self.refresh()
        if self.state is None:
            self.shell.flash(self.error, "error")
            return False
        if not self.state.enabled:
            self.shell.workflows.close_dashboard()
            self.shell.show("Manager is off", "\n".join([ui.DISABLED_SUMMARY, "", ui.ENABLE_HINT]))
            return False
        result = await self.shell.client.manager_open()
        session = result.session.id
        earlier = self.previous
        if not self.is_active():  # opening again from the manager keeps the way back
            self.previous = (self.shell.workspace, self.shell.controller.session)
        try:
            await self.shell.switch(session)  # the manager is a view, not a tab: `switch` adds none for it
        except BaseException:
            self.previous = earlier
            raise
        await self.refresh()
        if not self.state.signed_in:
            self.shell.flash(f"Manager opened, but its model is not available · {escape_controls(self.state.sign_in)}",
                             "warning")
        return True

    async def toggle(self) -> None:
        """The top-bar button and ``Ctrl+X A``: open the manager view, or close it and restore the prior session."""
        if self.is_active():
            await self.close()
        else:
            await self.open()

    async def close(self) -> None:
        """Leave the manager for the session that was in front when it opened. Nothing is archived or deleted."""
        shell = self.shell
        target, self.previous = self.previous, None
        tabs = [(tab["workspace"], tab["id"]) for tab in shell.tabs if tab["id"] != self.session
                and tab["id"] != ui.MANAGER_SESSION_ID]
        if target is not None and target[1] == self.session:
            target = None
        if target is None or (target not in tabs and tabs):
            target = tabs[0] if tabs else None  # the earlier tab was closed meanwhile
        if target is None:
            await shell.command("/new", ())
        else:
            await shell.switch_project(*target)

    async def live(self) -> None:
        """Start the Live call; it is always on the manager, and the session on screen stays put."""
        self.shell.workflows.close_dashboard()  # the call panel is drawn on conversation pages
        await self.shell.live.start()

    async def live_ready(self) -> bool:
        """Read ``ManagerState``; ``False`` (with the reason on screen) when the manager cannot front a call."""
        await self.refresh()
        if self.state is None:
            self.shell.flash(self.error, "error")
            return False
        if not self.state.enabled:
            self.shell.workflows.close_dashboard()
            self.shell.show("Manager is off", "\n".join([ui.DISABLED_SUMMARY, "", ui.ENABLE_HINT]))
            return False
        return True

    async def live_session(self) -> str:
        """Create the manager session on first use and return its id, without switching to it."""
        try:
            result = await self.shell.client.manager_open()
        except Exception as exc:  # noqa: BLE001 - shown with the redacted reason, the call does not start
            self.shell.flash(f"Manager unavailable: {escape_controls(str(exc))[:160]}", "error")
            return ""
        await self.refresh()
        if self.state is not None and not self.state.signed_in:
            self.shell.flash(f"The Manager's model is not available · {escape_controls(self.state.sign_in)}",
                             "warning")
        return str(result.session.id)

    # -- show requests (manager.show) ---------------------------------------

    def on_event(self, event, *, session: str = "", fresh: bool = True, floor: float = 0.0) -> bool:
        """Act on a fresh ``manager.show`` the host recorded for this client; ``True`` when a switch was started.

        Called with every event of the manager log this client reduces, before it is ingested.
        The request is advisory and unauthenticated by this client, so it acts only when all of
        these hold: the event is not a replay (``fresh``: past the reduced cursor; ``floor``: not
        older than the Live follow that reads it), it is within 30 s of its own timestamp and
        before ``expires_at``, it was not acted on already (bounded memory), and it targets this
        client: this client id is in ``clients`` and the manager is still in front, or the request
        came from Live and this client's Live call follows that manager. A modal, form, dashboard,
        recording or pending prompt is never replaced; a toast says so instead.
        """
        if getattr(event, "type", "") != "manager.show":
            return False
        session = session or str(getattr(event, "session", "") or "")
        if not session or not self.is_active(session) or not fresh:
            return False
        data = event.data if isinstance(getattr(event, "data", None), dict) else {}
        target, clients, expires = data.get("target"), data.get("clients"), data.get("expires_at")
        stamp = getattr(event, "ts", None)
        if (not isinstance(target, str) or not 0 < len(target) <= 128
                or not isinstance(clients, list) or isinstance(expires, bool)
                or not isinstance(expires, (int, float)) or isinstance(stamp, bool)
                or not isinstance(stamp, (int, float))):
            return False
        now = time.time()
        if stamp < floor or now - stamp > SHOW_TTL_SECONDS or now >= expires or expires - stamp > SHOW_TTL_SECONDS + 5:
            return False
        key = (session, int(getattr(event, "seq", 0) or 0), str(getattr(event, "id", "") or ""))
        if key in self._shown:
            return False
        shell = self.shell
        mine = shell.focus.client_id in clients and shell.controller.session == session
        live = bool(data.get("live")) and self._live_follows(session)
        if not (mine or live):
            return False
        self._shown[key] = None
        while len(self._shown) > SHOWN_MEMORY:
            self._shown.popitem(last=False)
        if self._show_task is not None and not self._show_task.done():
            return False  # one switch at a time; the per-turn cap bounds the rest
        blocked = self._blocked()
        if blocked:
            shell.flash(f"The Manager asked to show {target}, but {blocked}", "warning")
            return False
        if shell.controller.session == target:
            return False
        self._show_task = asyncio.get_running_loop().create_task(self._show(session, target, live))
        return True

    def _live_follows(self, session: str) -> bool:
        live = getattr(self.shell, "live", None)
        follow = getattr(live, "follow", None)
        return bool(live is not None and live.active and live.session == session
                    and follow is not None and follow.session == session
                    and getattr(live, "workspace", self.shell.workspace) == self.shell.workspace)

    def _blocked(self) -> str:
        """Why a switch would clobber something the user is doing ('' when it would not)."""
        shell = self.shell
        flows = shell.workflows
        dashboard = getattr(flows, "dashboard", None)
        view = shell.controller.view
        if flows.form is not None or shell.panel_title or shell.items or flows.stack:
            return "a panel is open"
        if dashboard is not None and dashboard.visible:
            return "the Sessions dashboard is open"
        if shell.voice.phase != "idle":
            return "voice input is active"
        if view.pending_permissions or pending_questions(view) or shell.live.follow.waiting():
            return "a prompt is waiting"
        return ""

    async def _show(self, session: str, target: str, live: bool) -> None:
        """Switch to ``target`` in its own task: a switch cancels the stream task that delivered the event."""
        shell = self.shell
        try:
            if not live and not (self.is_active(session) and shell.controller.session == session):
                return  # the user left the manager meanwhile
            if self._blocked():
                return
            if live and not self._live_follows(session):
                return  # another project is in front now: the target id belongs to the call's workspace
            await shell.switch_project(shell.workspace, target)
            if not shell.controller.running:
                shell.controller.resume(shell.on_update)
            if shell.on_update is not None:
                await shell.on_update()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown as a toast, never fatal
            shell.flash(f"Could not show {escape_controls(target)[:128]}: {escape_controls(str(exc))[:160]}", "error")

    async def aclose(self) -> None:
        task, self._show_task = self._show_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- refusals ------------------------------------------------------------

    def refusal(self, name: str) -> str:
        """The readable reason a manager chat does not offer ``name`` ('' when it is allowed)."""
        return ui.REFUSED_COMMANDS.get(name, "") if self.is_active() else ""
