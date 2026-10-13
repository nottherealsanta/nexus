"""Native GPT Live call bridge (GPT_LIVE_PLAN section 5, M4; docs/live.md).

Owns the *client* half of one call: consent, the ``nexus-live`` helper process
(``ui_support/live_helper.py``), the call bar state and the on-screen errors. The
daemon owns the call itself (``LiveStart/Mute/Stop``); the timeline card is a
projection of the durable ``live.*`` events (``ui_support/live_card.py``). Typing
during a call is ordinary steering: nothing here takes the editor.

Consent is stored by the daemon (``LiveConsent``), once per machine, never only in
memory. Real microphone and OpenAI behavior are not verified (docs/live.md).
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from ...ui_support import live_helper
from ...ui_support.live_card import card_rows, card_status, card_title
from ...ui_support.live_card import encode as card_encode
from ...ui_support.live_helper import LiveCall, describe_failure
from ...ui_support.voice_capture import play_cue

CONSENT_LINES = (  # short lines: list dialogs clip instead of wrapping
    "GPT Live is a spoken conversation with OpenAI's",
    "voice model, working through this session.",
    "",
    "Sent to OpenAI through your ChatGPT account:",
    "  - audio from your microphone during the call",
    "  - summaries of the agent's work and results,",
    "    which can include file contents and output",
    "",
    "The session's model, tools and permissions do the",
    "work. The voice can ask the agent to act but can",
    "never approve anything: permissions stay on screen.",
    "",
    "Experimental, undocumented service. Asked once per",
    "machine; withdraw any time with /live revoke.",
)
USAGE = "Use /live [stop|mute|new|status|revoke]"
_STATE_LABEL = {"connecting": "connecting", "listening": "listening", "speaking": "speaking",
                "working": "working", "muted": "muted", "ended": "ended"}


class Live:
    def __init__(self, shell):
        self.shell = shell
        self.call: LiveCall | None = None
        self.state = "idle"  # idle|starting|active|stopping
        self.session = ""
        self.started_at = 0.0
        self.muted = False
        self.watch: asyncio.Task | None = None
        self.opened: set[str] = set()  # call ids whose card is expanded (instructions, commentary)
        self.watch_interval = 0.5
        self.status_interval = 5.0

    # -- state ---------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.state in {"starting", "active"}

    def bar(self) -> dict | None:
        """The call bar for the snapshot, or ``None`` when no call is open."""
        if self.state == "idle":
            return None
        helper = self.call.helper if self.call else None
        view = self._view_call()
        phase = "connecting" if self.state == "starting" else "listening"
        if view is not None and view.status == "active" and view.phase:
            phase = view.phase
        elif helper is not None and self.state == "active":
            phase = helper.phase
        if self.muted and self.state == "active":
            phase = "muted"
        if self.state == "stopping":
            phase = "ended"
        current = getattr(self.shell.controller, "session", "")
        return {
            "active": True, "state": self.state, "phase": _STATE_LABEL.get(phase, phase),
            "model": (view.model if view is not None else "") or "", "muted": self.muted,
            "started": self.started_at, "in": 0.0 if self.muted else round(helper.level_in if helper else 0.0, 3),
            "out": round(helper.level_out if helper else 0.0, 3),
            "elsewhere": self.session if self.session and self.session != current else "",
        }

    def _view_call(self):
        """This call's folded view, when the shown session is the call's session."""
        if self.call is None or self.call.result is None:
            return None
        for call in reversed(getattr(self.shell.controller.view, "live_calls", [])):
            if call.call_id == self.call.result.call_id:
                return call
        return None

    # -- commands ------------------------------------------------------------

    async def command(self, args: tuple[str, ...]) -> None:
        action = args[0].casefold() if args else ""
        if len(args) > 1 or action not in {"", "stop", "mute", "new", "status", "revoke"}:
            raise ValueError(USAGE)
        if action == "":
            if self.active:
                await self.stop()
            else:
                await self.start()
        elif action == "stop":
            await self.stop()
        elif action == "mute":
            await self.mute()
        elif action == "new":
            if self.active:
                raise ValueError("A call is open. End it with /live stop, then use /live new.")
            await self.start(new_conversation=True)
        elif action == "status":
            await self.status()
        else:
            await self.shell.client.live_consent(False)
            self.shell.notice = "GPT Live consent withdrawn · the notice will be shown again"

    async def operate(self, operation: dict) -> None:
        kind = operation.get("kind")
        if kind == "live_consent":
            await self.shell.client.live_consent(True)
            self._close_panel()
            await self.start(new_conversation=bool(operation.get("new")), consented=True)
        elif kind == "live_toggle":
            call_id = str(operation.get("id", ""))
            self.opened.symmetric_difference_update({call_id})
            if len(self.opened) > 64:
                self.opened.clear()

    async def start(self, *, new_conversation: bool = False, consented: bool = False) -> None:
        if self.state != "idle":
            self.shell.notice = f"A Live call is {self.state} · /live stop ends it"
            return
        session = self.shell.controller.session
        status = await self.shell.client.live_status()
        if not status.available:
            self._unavailable(status)
            return
        try:
            live_helper.binary_path()
        except live_helper.LiveHelperError as exc:
            self.fail("GPT Live is not available", exc)
            return
        if not (status.consent or consented):
            # The notice is the first row's body (list dialogs show item lines, not panel lines);
            # Enter on it closes, so agreeing is always a deliberate second step.
            self.shell.workflows.menu(
                "Use GPT Live voice?",
                [("Before you start: what is sent", {"kind": "back", "_presentation": {"lines": list(CONSENT_LINES)}}),
                 ("Agree and start the call", {"kind": "live_consent", "new": new_conversation}),
                 ("Cancel", {"kind": "back"})],
                CONSENT_LINES, layout="context")
            return
        await self._connect(session, new_conversation)

    async def _connect(self, session: str, new_conversation: bool) -> None:
        self.state, self.session, self.muted, self.started_at = "starting", session, False, 0.0
        self.call = LiveCall(self.shell.client, session)
        await self._refresh()
        try:
            await self.call.start(new_conversation=new_conversation)
        except asyncio.CancelledError:
            await self._release()
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is shown with its fix
            await self._release()
            self.fail("GPT Live could not start", exc)
            return
        self.state, self.started_at = "active", self.call.started_at
        play_cue("start")
        self.shell.notice = "Live call connected · speak naturally, /live stop to hang up"
        self.watch = asyncio.create_task(self._watch(self.call))

    async def stop(self) -> None:
        call = self.call
        if call is None or self.state == "idle":
            self.shell.notice = "No active Live call"
            return
        if self.state == "stopping":
            return
        self.state = "stopping"
        try:
            await call.stop("user")
        except Exception as exc:  # noqa: BLE001 - the helper is released regardless
            self.shell.flash(describe_failure(exc)[0], "error")
        await self._finished("Live call ended")

    async def mute(self) -> None:
        if self.call is None or self.state != "active":
            self.shell.notice = "No active Live call"
            return
        state = await self.call.mute(None)
        self.muted = bool(state.muted)
        self.shell.notice = "Microphone muted" if self.muted else "Microphone live"

    async def status(self) -> None:
        status = await self.shell.client.live_status()
        helper = "installed" if live_helper.available() else "missing (build or install nexus-live)"
        rows = {
            "Available": "yes" if status.available else f"no ({status.reason})",
            "State": status.state, "Phase": status.phase or "none",
            "Session": status.active_session or "none", "Voice": status.voice or "default",
            "Muted": "yes" if status.muted else "no",
            "Elapsed": f"{int(status.elapsed_s)}s of {status.max_minutes} min",
            "Consent": "given" if status.consent else "not given yet",
            "Audio helper": helper, "Last error": status.last_error or "none",
        }
        self.shell.show("Live status", rows)

    # -- ending --------------------------------------------------------------

    async def shutdown(self) -> None:
        """Release the microphone when the shell closes; the daemon hears ``live_stop``."""
        if self.watch and self.watch is not asyncio.current_task():
            self.watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watch
        self.watch = None
        call, self.call = self.call, None
        self.state = "idle"
        if call is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(call.stop("client_closed"), 5)

    async def _release(self) -> None:
        call, self.call = self.call, None
        self.state, self.started_at, self.muted = "idle", 0.0, False
        if call is not None:
            with contextlib.suppress(Exception):
                await call.helper.stop()

    async def _finished(self, notice: str, error: tuple[str, str] | None = None) -> None:
        watch, self.watch = self.watch, None
        if watch and watch is not asyncio.current_task():
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        await self._release()
        play_cue("stop")
        if error:
            self.fail("GPT Live call ended", None, message=error[0], fix=error[1])
        else:
            self.shell.notice = notice
        await self._refresh()

    async def _watch(self, call: LiveCall) -> None:
        """Notice the end of a call that this client did not request (helper exit, deadline, other client)."""
        last_status = time.monotonic()
        try:
            while self.call is call and self.state == "active":
                await asyncio.sleep(self.watch_interval)
                helper = call.helper
                if not helper.running:
                    reason = helper.closed_reason or "helper_exited"
                    with contextlib.suppress(Exception):
                        await call.client.live_stop(call.session, "helper_closed")
                    detail = f"The audio helper stopped ({reason})" + (f": {helper.error}" if helper.error else "")
                    await self._finished(
                        "", (detail, live_helper.classify_failure(reason, helper.error)))
                    return
                view = self._view_call()
                if view is not None and view.status == "ended":
                    await self._finished(self._ended_text(view))
                    return
                if time.monotonic() - last_status >= self.status_interval:
                    last_status = time.monotonic()
                    status = await call.client.live_status()
                    if status.state == "idle" or status.active_session != call.session:
                        await self._finished("Live call ended")
                        return
                await self._refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken watch must not leave the mic open
            await self._finished("", (describe_failure(exc)[0], ""))

    @staticmethod
    def _ended_text(view) -> str:
        reason = view.end_reason or "ended"
        return "Live call ended" if reason in {"user", "stop"} else f"Live call ended ({reason})"

    # -- timeline ------------------------------------------------------------

    def card_blocks(self, view) -> list[tuple[float, dict]]:
        """``(start time, block)`` for each call in ``view``: the labelled Live card.

        Cards are collapsed by default (voice instructions, commentary and long text);
        clicking one sends ``live_toggle``. The block ``status`` field carries the call
        phase; the native client draws kind ``live`` (``rust/tui/src/transcript.rs``).
        """
        turns: dict[str, int] = {}
        for turn in view.turns:
            for message in turn.messages:
                origin = message.origin or {}
                if message.role == "user" and origin.get("kind") == "live" and origin.get("delegation_id"):
                    turns[str(origin["delegation_id"])] = turn.index + 1
        cards = []
        for call in view.live_calls:
            expanded = call.call_id in self.opened
            rows = card_rows(call, expanded=expanded, turns=turns)
            cards.append((call.started_ts or 0.0, {
                "id": f"live:{call.call_id}", "kind": "live", "title": card_title(call),
                "status": card_status(call), "collapsed": not expanded, "gap": 1,
                "detail": card_encode(rows), "operation": {"kind": "live_toggle", "id": call.call_id}}))
        return cards

    # -- display -------------------------------------------------------------

    def _close_panel(self) -> None:
        self.shell.panel_title = ""
        self.shell.items = []
        self.shell.panel_revision += 1

    async def _refresh(self) -> None:
        update = getattr(self.shell, "on_update", None)
        if update is not None:
            with contextlib.suppress(Exception):
                await update(immediate=False)

    def _unavailable(self, status) -> None:
        reasons = {
            "busy": f"A Live call is already running in session {status.active_session or 'another session'}.",
            "no_credential": "GPT Live needs a ChatGPT sign-in.",
            "disabled": "GPT Live is turned off.",
            "unsupported": "GPT Live is not available in this install.",
        }
        message = reasons.get(status.reason, f"GPT Live is unavailable ({status.reason or 'unknown reason'}).")
        fix = live_helper.fix_for(status.reason)
        self.fail("GPT Live is not available", None, message=message, fix=fix)

    def fail(self, title: str, exc: BaseException | None, *, message: str = "", fix: str = "") -> None:
        """Show the host's redacted message with the fix, as a panel and a toast."""
        if exc is not None:
            message, fix = describe_failure(exc)
        lines = [message or title]
        if fix:
            lines += ["", f"Fix: {fix}"]
        self.shell.show(title, "\n".join(lines))
        self.shell.flash(f"{title} · {message}"[:200], "error")
