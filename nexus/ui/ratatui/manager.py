"""Native Manager entry: the pinned sidebar row, opening the session, and its Live route.

The manager is a dedicated per-workspace session (docs/manager.md). The host owns
every rule (disabled by default, pinned model, no role/model/fork/shell, separate
Live consent); this module only reads ``ManagerState`` for the pinned entry, calls
``ManagerOpen`` to open it, and routes Live through the ordinary ``Live`` bridge
with the manager's cross-session consent. Terminal and desktop share it.
"""
from __future__ import annotations

import time

from ...ui_support import manager as ui
from ...ui_support.text import escape_controls

USAGE = "Use /manager [open|live|status]"
STALE_SECONDS = 30.0  # ManagerState is read at start, on open and about this often while polling
TITLE = "Manager"


class Manager:
    def __init__(self, shell):
        self.shell = shell
        self.state = None  # the last ManagerStateResult
        self.error = ""
        self.read_at: float | None = None

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
        await self.shell.switch(session)
        for tab in self.shell.tabs:
            if tab["id"] == session:
                tab["title"] = TITLE
        await self.refresh()
        if not self.state.signed_in:
            self.shell.flash(f"Manager opened, but its model is not available · {escape_controls(self.state.sign_in)}",
                             "warning")
        return True

    async def live(self) -> None:
        """Open the manager, then start the ordinary Live call there (the manager consent applies)."""
        if not await self.open():
            return
        await self.shell.live.start()

    # -- refusals ------------------------------------------------------------

    def refusal(self, name: str) -> str:
        """The readable reason a manager chat does not offer ``name`` ('' when it is allowed)."""
        return ui.REFUSED_COMMANDS.get(name, "") if self.is_active() else ""
