"""Native GPT Live call bridge (GPT_LIVE_PLAN section 5, M4; docs/live.md).

Owns the *client* half of one call: consent, the ``nexus-live`` helper process
(``ui_support/live_helper.py``), the call bar state and the on-screen errors. The
daemon owns the call itself (``LiveStart/Mute/Stop``); the timeline card is a
projection of the durable ``live.*`` events (``ui_support/live_card.py``). Typing
during a call is ordinary steering: nothing here takes the editor.

Every call is attached to the manager session (plan 02 section 10), never to the session on
screen: ``manager_follow.ManagerFollow`` subscribes to it so the pane and the manager's
approvals stay reachable while another session is in front.

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
from ...ui_support.live_helper import CONSENT_LINES, LiveCall, describe_failure
from ...ui_support.manager import CONSENT_LINES as MANAGER_CONSENT_LINES
from ...ui_support.voice_capture import play_cue
from .manager_follow import ManagerFollow

USAGE = "Use /live [stop|pause|resume|mute|new|status|revoke [manager]]"
MANAGER_LABEL = "Manager"
MANAGER_PREFIX = "Manager · "
_STATE_LABEL = {"connecting": "connecting", "listening": "listening", "speaking": "speaking",
                "working": "working", "muted": "muted", "paused": "paused", "ended": "ended"}


class Live:
    def __init__(self, shell):
        self.shell = shell
        self.call: LiveCall | None = None
        self.state = "idle"  # idle|starting|active|stopping
        self.session = ""
        self.workspace = ""  # the project whose manager this call is on (captured when the call starts)
        self.started_at = 0.0
        self.begin_ts = 0.0  # wall clock when the current start began (matches a call before its id is known)
        self.muted = False
        self.paused = False
        self.pause_left = 0.0
        self.hibernated = False  # the last call ended because a pause ran out (Ctrl+X P restarts it)
        self.watch: asyncio.Task | None = None
        self.opened: set[str] = set()  # call ids whose card is expanded (instructions, commentary)
        self.watch_interval = 0.5
        self.status_interval = 5.0
        # The native terminal draws the open call in a pane above the composer instead of
        # a timeline card; the desktop client keeps the card (prototype.run sets this).
        self.pane = True
        # The panel subscribes to the manager session itself, so its rows and approvals reach the
        # user wherever they are (a hidden manager view would deadlock or auto-deny an approval).
        self.follow = ManagerFollow(shell)

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
        if self.paused and self.state == "active":
            phase = "paused"
        if self.state == "stopping":
            phase = "ended"
        current = getattr(self.shell.controller, "session", "")
        pane = (self._card(view, self._turn_numbers(self._view()))
                if self.pane and view is not None else {})
        return {
            "active": True, "state": self.state, "phase": _STATE_LABEL.get(phase, phase),
            "model": (view.model if view is not None else "") or "", "muted": self.muted,
            "paused": self.paused,
            "pause_left": round(max(0.0, self.pause_left - time.time()), 1) if self.paused else 0.0,
            "started": self.started_at, "in": 0.0 if self.muted or self.paused else round(helper.level_in if helper else 0.0, 3),
            "out": round(helper.level_out if helper else 0.0, 3),
            # The call is on the manager: named when another session is in front.
            "elsewhere": MANAGER_LABEL if self.session and self.session != current else "",
            "manager": True,
            "attention": self._attention(current),
            # The open call's card rows, drawn in the Live pane above the composer.
            "detail": pane.get("detail", ""), "collapsed": pane.get("collapsed", True),
            "operation": pane.get("operation"),
        }

    def _view(self):
        """The manager's folded view: the shown session's when it is the manager, else the followed one."""
        controller = self.shell.controller
        if self.session and getattr(controller, "session", "") == self.session:
            return controller.view
        return self.follow.view if self.follow.session == self.session else None

    def _attention(self, current: str) -> str:
        """``approval waiting`` while a manager prompt is pending and another session is in front."""
        if not self.session or current == self.session or self.follow.session != self.session:
            return ""
        return self.follow.waiting()

    def manager_prompt(self, current: str, build) -> dict | None:
        """The manager's pending approval or question, for any other session in front.

        ``build`` is the projection's prompt builder. The prompt names its session so the answer
        goes to the manager, and its first line says whose it is (it is not the shown session's).
        """
        if self.state == "idle" or not self.session or current == self.session:
            return None
        view = self._view()
        prompt = build(view) if view is not None else None
        if prompt is None:
            return None
        prompt["session"] = self.session
        lines = list(prompt.get("lines") or [""])
        prompt["lines"] = [MANAGER_PREFIX + lines[0], *lines[1:]]
        if prompt.get("title"):
            prompt["title"] = MANAGER_PREFIX + prompt["title"]
        return prompt

    def _view_call(self):
        """This call's folded view, in the manager's view (shown or followed)."""
        if self.call is None:
            return None
        view = self._view()
        calls = getattr(view, "live_calls", []) if view is not None else []
        if self.call.result is not None:
            return next((c for c in reversed(calls) if c.call_id == self.call.result.call_id), None)
        # The daemon records ``live.started`` before this client has the start result, so for
        # a moment the call id is unknown. This client holds one call (the daemon allows one),
        # so the newest call of the manager that has not ended and began after we did
        # is ours; matching it keeps the pane up and the card out of the timeline.
        if self.state not in {"starting", "active", "stopping"}:
            return None
        for call in reversed(calls):
            if call.status != "ended" and (call.started_ts or 0.0) >= self.begin_ts - 5.0:
                return call
        return None

    # -- commands ------------------------------------------------------------

    async def command(self, args: tuple[str, ...]) -> None:
        action = args[0].casefold() if args else ""
        scope = "manager" if action == "revoke" and len(args) == 2 and args[1].casefold() == "manager" else ""
        if not scope and (len(args) > 1 or action not in {"", "stop", "pause", "resume", "mute", "new", "status", "revoke"}):
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
        elif action in ("pause", "resume"):
            await self.pause(None if action == "pause" else False)
        elif action == "new":
            if self.active:
                raise ValueError("A call is open. End it with /live stop, then use /live new.")
            await self.start(new_conversation=True)
        elif action == "status":
            await self.status()
        elif scope:
            await self.shell.client.live_consent(False, scope=scope)
            self.shell.notice = "Manager cross-session consent withdrawn · the notice will be shown again for the Manager"
        else:
            await self.shell.client.live_consent(False)
            self.shell.notice = "GPT Live consent withdrawn · the notice will be shown again"

    async def operate(self, operation: dict) -> None:
        kind = operation.get("kind")
        if kind == "live_consent":
            scope = "manager" if operation.get("scope") == "manager" else ""
            await self.shell.client.live_consent(True, **({"scope": scope} if scope else {}))
            self._close_panel()
            await self.start(new_conversation=bool(operation.get("new")), consented=True)
        elif kind == "live_toggle":
            call_id = str(operation.get("id", ""))
            self.opened.symmetric_difference_update({call_id})
            if len(self.opened) > 64:
                self.opened.clear()

    async def start(self, *, new_conversation: bool = False, consented: bool = False) -> None:
        """Start a call. Live always attaches to the manager, never to the session on screen.

        Once the call connects, the foreground moves to the manager screen (plan 10): the call,
        its pane and the manager's approvals share one screen with the composer, which stays
        usable during the call. A modal, form, recording or pending prompt is never replaced;
        a toast offers to open the manager instead. Ending the call never switches back.
        """
        if self.state != "idle":
            self.shell.notice = f"A Live call is {self.state} · /live stop ends it"
            return
        if not await self.shell.manager.live_ready():
            return
        status = await self.shell.client.live_status()
        if not status.available:
            self._unavailable(status)
            return
        try:
            live_helper.binary_path()
        except live_helper.LiveHelperError as exc:
            self.fail("GPT Live is not available", exc)
            return
        agreed = status.consent
        if agreed and not consented:
            # The call is on the manager, which also sends what it reads of other sessions: its own notice.
            agreed = bool((await self.shell.client.live_consent(None, scope="manager")).accepted)
        if not (agreed or consented):
            self._consent_dialog(new_conversation, True)
            return
        session = await self.shell.manager.live_session()
        if not session:
            return
        await self._connect(session, new_conversation)

    def _consent_dialog(self, new_conversation: bool, manager: bool = False) -> None:
        # The notice is the first row's body (list dialogs show item lines, not panel lines);
        # Enter on it closes, so agreeing is always a deliberate second step.
        lines = [*CONSENT_LINES, "", *MANAGER_CONSENT_LINES] if manager else list(CONSENT_LINES)
        agree = {"kind": "live_consent", "new": new_conversation, **({"scope": "manager"} if manager else {})}
        self.shell.workflows.menu(
            "Use GPT Live with the Manager?" if manager else "Use GPT Live voice?",
            [("Before you start: what is sent", {"kind": "back", "_presentation": {"lines": lines}}),
             ("Agree and start the call", agree),
             ("Cancel", {"kind": "back"})],
            lines, layout="context")

    async def _connect(self, session: str, new_conversation: bool) -> None:
        self.state, self.session, self.muted, self.started_at = "starting", session, False, 0.0
        self.workspace = self.shell.workspace
        self.paused, self.pause_left, self.hibernated = False, 0.0, False
        self.begin_ts = time.time()
        self.call = LiveCall(self.shell.client, session)
        self.follow.start(session)
        await self._refresh()
        try:
            await self.call.start(new_conversation=new_conversation)
        except asyncio.CancelledError:
            await self._release()
            self.state = "idle"
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is shown with its fix
            await self._release()
            self.state = "idle"
            if getattr(exc, "kind", "") == "live_consent_required":  # the daemon's refusal is the consent dialog
                self.shell.notice = "GPT Live needs your consent first"
                self._consent_dialog(new_conversation, True)
                return
            self.fail("GPT Live could not start", exc)
            return
        self.state, self.started_at = "active", self.call.started_at
        play_cue("start")
        self.shell.notice = "Live call connected · speak naturally, /live stop to hang up"
        self.watch = asyncio.create_task(self._watch(self.call))
        await self._open_manager()

    async def _open_manager(self) -> None:
        """Move to the manager screen now the call is up (plan 10 §4.3).

        Never steals a modal, form, dashboard, recording or pending prompt: then a toast
        offers to open the manager instead. Consent refusals and unavailable helpers never
        reach here (``start`` returns before ``_connect``), so they never move the user.
        """
        manager = self.shell.manager
        if manager.is_active():
            return
        blocked = manager._blocked()
        if blocked:
            self.shell.toast(f"Live call connected · the Manager screen is withheld: {blocked}", "info",
                             action={"label": "Open Manager", "operation": {"kind": "manager_open"}})
            return
        await manager.open()

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

    async def pause(self, paused: bool | None = None) -> None:
        """Pause or resume (docs/live.md "Pause vs stop"); with no call, a timed-out pause restarts it."""
        if self.call is None or self.state != "active":
            if self.hibernated and self.state == "idle" and paused is not True:
                await self.start()  # the carried transcript is the resume
                return
            self.shell.notice = "No active Live call"
            return
        state = await self.call.pause(paused)
        self.paused = bool(state.paused)
        left = float(getattr(state, "pause_remaining_s", 0.0) or 0.0)
        self.pause_left = time.time() + left  # wall-clock deadline; the bar counts down
        self.shell.notice = (
            f"Live paused · microphone and voice silent · call ends in {int(left)}s · "
            "Ctrl+X P resumes, /live stop ends" if self.paused else "Live resumed · microphone live")

    async def status(self) -> None:
        status = await self.shell.client.live_status()
        helper = "installed" if live_helper.available() else "missing (build or install nexus-live)"
        rows = {
            "Available": "yes" if status.available else f"no ({status.reason})",
            "State": status.state, "Phase": status.phase or "none",
            "Session": status.active_session or "none", "Voice": status.voice or "default",
            "Muted": "yes" if status.muted else "no",
            "Paused": f"yes ({int(status.pause_remaining_s)}s left)" if status.paused else "no",
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
        await self.follow.stop()
        if call is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(call.stop("client_closed"), 5)

    async def _release(self) -> None:
        call, self.call = self.call, None
        self.started_at, self.muted = 0.0, False
        self.paused, self.pause_left = False, 0.0
        await self.follow.stop()
        if call is not None:
            with contextlib.suppress(Exception):
                await call.helper.stop()

    async def _finished(self, notice: str, error: tuple[str, str] | None = None) -> None:
        watch, self.watch = self.watch, None
        if watch and watch is not asyncio.current_task():
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        if not error:
            self.shell.notice = notice
        await self._release()
        play_cue("stop")
        if error:
            self.fail("GPT Live call ended", None, message=error[0], fix=error[1])
        else:
            self.shell.notice = notice
        self.state = "idle"
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
                    self.hibernated = view.end_reason == "pause_timeout"
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
        if reason == "pause_timeout":
            return "Live call ended: the pause ran out · Ctrl+X P starts a new call with the transcript carried over"
        return "Live call ended" if reason in {"user", "stop"} else f"Live call ended ({reason})"

    # -- timeline ------------------------------------------------------------

    def card_blocks(self, view) -> list[tuple[float, dict]]:
        """``(start time, block)`` for each call in ``view``: the labelled Live card.

        Cards are collapsed by default (voice instructions, commentary and long text);
        clicking one sends ``live_toggle``. The block ``status`` field carries the call
        phase; the native client draws kind ``live`` (``rust/tui/src/transcript.rs``).
        The call that is open on this client is left out: the Live pane above the
        composer shows it (``bar()["detail"]``) until it ends, then it joins the timeline.
        """
        turns = self._turn_numbers(view)
        open_call = self._view_call()
        pane_id = open_call.call_id if self.pane and open_call is not None and self.state != "idle" else None
        return [(call.started_ts or 0.0, self._card(call, turns))
                for call in view.live_calls if call.call_id != pane_id]

    @staticmethod
    def _turn_numbers(view) -> dict[str, int]:
        """Delegation id -> number of the turn its user message started."""
        turns: dict[str, int] = {}
        for turn in getattr(view, "turns", ()):
            for message in turn.messages:
                origin = message.origin or {}
                if message.role == "user" and origin.get("kind") == "live" and origin.get("delegation_id"):
                    turns[str(origin["delegation_id"])] = turn.index + 1
        return turns

    def _card(self, call, turns: dict[str, int]) -> dict:
        expanded = call.call_id in self.opened
        rows = card_rows(call, expanded=expanded, turns=turns)
        return {
            "id": f"live:{call.call_id}", "kind": "live", "title": card_title(call),
            "status": card_status(call), "collapsed": not expanded, "gap": 1,
            "detail": card_encode(rows), "operation": {"kind": "live_toggle", "id": call.call_id}}

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
        message = live_helper.unavailable_message(status.reason, status.active_session)
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
