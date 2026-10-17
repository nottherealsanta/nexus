"""Delegation bridge: voice requests become ordinary agent turns (plan section 4.1, M2).

Contract:

- A ``delegation.created`` from the voice model is **untrusted input**, exactly
  like a typed prompt. It is enqueued as a user message through the host
  (idle session: ``queue``; a turn is running: ``steer``), labelled
  ``origin = {"kind": "live", "delegation_id": ...}`` on the durable
  ``input.queued`` event. The permission engine, hooks and limits apply
  unchanged; the bridge never grants, resolves or auto-approves anything.
- The bridge watches the session through a **non-viewer tap**
  (``LiveHost.observe``), so it never changes presence or the unattended policy.
- Progress: ``tool.requested`` bursts become one short ``commentary`` append at
  most every 5 s, only when ``[live].progress`` is on.
- The user message is the delegation text **plus** a labelled transcript of both speakers
  since the previous delegation and a note that it was speech (``compose_request``), like
  Codex's ``<realtime_delegation>`` wrapper (codex-rs core/src/context/realtime_delegation.rs).
  It is plain text in the message itself, so the timeline shows exactly what the agent saw;
  Nexus has no hidden-context channel for user messages and must not grow one. Each part is
  bounded (request 1 MiB, background transcript 32 KiB) and clipping is announced. An empty delegation falls back to the transcript.
- ``turn.completed`` sends the last visible assistant text as plain speakable text (no label;
  the manager sends it as ``session.context.append``/``speakable``, as Codex's TUI does).
  ``turn.failed`` /
  ``turn.cancelled`` send a notice. ``permission.requested`` and
  ``question.requested`` send a notice that the answer happens **on screen**.
- Every append goes through ``append`` (the manager), which chunks it to 500 UTF-8
  bytes and records it as a durable ``live.appended`` event.

Not exercised against the real service: **not verified**.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..util import redact_secrets

log = logging.getLogger("nexus.live")

PROGRESS_INTERVAL_S = 5.0
MAX_FINAL_CHARS = 12_000
MAX_TOOLS_IN_PROGRESS = 5
MAX_REQUEST_MESSAGE_CHARS = 200_000  # includes headers, transcript, and omission notice
MAX_TRACKED_TURNS = 32
MAX_HEARD_CHARS = 2_000
# Resource bounds only: a sideband frame is already <= 1 MiB, so a real spoken request fits whole.
MAX_INPUT_BYTES = 1_048_576  # delegation text kept (start); clipping is announced in the text
MAX_TRANSCRIPT_BYTES = 32_768  # background transcript kept (end); clipping is announced in the text
SPEECH_NOTE = (
    "Note: this request came from a voice call. The text is speech recognition output and may "
    "contain errors or be unpunctuated; work out what the user meant and say so if unsure. "
    "Your final answer is read back to the user by a voice assistant, so lead with a short "
    "plain-language summary."
)
ROLE_LABELS = {"user": "You", "assistant": "Voice"}

Append = Callable[[str, str, "str | None"], Awaitable[None]]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _clip_bytes(text: str, limit: int, *, keep: str) -> tuple[str, int]:
    """Clip to ``limit`` UTF-8 bytes keeping the ``start`` or the ``end``; returns (text, characters dropped)."""
    if len(text.encode("utf-8")) <= limit:
        return text, 0
    raw = text.encode("utf-8")
    kept = (raw[:limit] if keep == "start" else raw[-limit:]).decode("utf-8", "ignore")
    return kept, len(text) - len(kept)


def format_transcript(entries: list[tuple[str, str]]) -> str:
    """``You: ...`` / ``Voice: ...`` lines, one per spoken turn."""
    lines = []
    for role, text in entries:
        text = " ".join(str(text).split())
        if text:
            lines.append(f"{ROLE_LABELS.get(role, role.title())}: {text}")
    return "\n".join(lines)


def compose_request(text: str, transcript: list[tuple[str, str]]) -> tuple[str, dict[str, Any]]:
    """The user message for one delegation, plus the facts recorded in ``live.delegated``.

    Layout (plain text, readable to the agent and in the timeline)::

        Voice request
        <delegation text>

        Spoken conversation since the previous request
        You: ...
        Voice: ...

        Note: ...

    A trailing ``You`` line equal to the request is dropped (it would repeat it). With no
    delegation text the transcript is the request. Returns ``("", ...)`` when both are empty.
    """
    entries = [(role, " ".join(str(t).split())) for role, t in transcript if str(t).strip()]
    request = text.strip()
    if request and entries and entries[-1][0] == "user" and _norm(entries[-1][1]) == _norm(request):
        entries.pop()
    body = format_transcript(entries)
    from_transcript = not request
    input_dropped = delta_dropped = 0
    if from_transcript:
        request, body = body, ""
        # here the transcript *is* the request, so it gets the request bound, not the context bound
        request, delta_dropped = _clip_bytes(request, MAX_INPUT_BYTES, keep="end")
        if delta_dropped:
            request = f"[… {delta_dropped} earlier characters omitted]\n{request}"
    else:
        request, input_dropped = _clip_bytes(request, MAX_INPUT_BYTES, keep="start")
        if input_dropped:
            request += f"\n[… {input_dropped} more characters omitted]"
        body, delta_dropped = _clip_bytes(body, MAX_TRANSCRIPT_BYTES, keep="end")
        if delta_dropped:
            body = f"[… {delta_dropped} earlier characters omitted]\n{body}"
    if not request:
        return "", {}
    head = "Voice request (taken from the spoken conversation)" if from_transcript else "Voice request"
    parts = [f"{head}\n{request}"]
    if body:
        parts.append(f"Spoken conversation since the previous request\n{body}")
    parts.append(SPEECH_NOTE)
    facts = {
        "transcript": body or (request if from_transcript else ""),
        "transcript_omitted": delta_dropped, "input_omitted": input_dropped,
        "from_transcript": from_transcript,
    }
    return "\n\n".join(parts), facts


def _is_subagent(data: Mapping[str, Any]) -> bool:
    agent = data.get("agent")
    return isinstance(agent, Mapping) and bool(agent.get("id"))


class LiveBridge:
    """Connect one call to one session."""

    def __init__(
        self,
        *,
        host: Any,
        session_id: str,
        append: Append,
        emit: Callable[[str, dict[str, Any]], None],
        set_phase: Callable[[str], None],
        progress: bool,
        clock: Callable[[], float],
        progress_interval: float = PROGRESS_INTERVAL_S,
    ) -> None:
        self._host = host
        self._session = session_id
        self._append = append
        self._emit = emit
        self._set_phase = set_phase
        self._progress = progress
        self._clock = clock
        self._interval = progress_interval
        self._queued: dict[str, str] = {}  # queued_id -> delegation_id
        self._settled: dict[str, None] = {}  # queued ids already consumed/dropped (ordered set)
        self._turns: dict[str, list[str]] = {}  # turn id -> delegation ids it carries
        self._tools: dict[str, list[str]] = {}  # turn id -> tool names since the last progress
        self._texts: dict[str, str] = {}  # turn id -> last assistant text
        self._last_progress = float("-inf")
        self._tap: Any = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._tap = self._host.observe(self._session)
        self._task = asyncio.create_task(self._run(), name="nexus-live-bridge")

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if self._tap is not None:
            with contextlib.suppress(Exception):
                self._tap.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    # -- delegation --------------------------------------------------------

    async def on_delegation(
        self, delegation_id: str, text: str, heard: str = "", transcript: list[tuple[str, str]] | None = None
    ) -> None:
        """Hand one spoken request to the session as a queued or steering user message.

        ``heard`` is the user's latest transcript when the manager has one; it is recorded
        beside ``text`` so a client can show whether the voice model's request was its own
        wording or the heard utterance passed through (``verbatim``).
        """
        async with self._lock:
            if self._closed:
                return
            text = text.strip()
            message, facts = compose_request(text, transcript or [])
            if not message:
                await self._append("The request was empty, so nothing was sent to the agent.", "notice", delegation_id)
                return
            if len(message) > MAX_REQUEST_MESSAGE_CHARS:
                original_length = len(message)
                prefix_length = MAX_REQUEST_MESSAGE_CHARS
                while True:
                    omitted = original_length - prefix_length
                    notice = (
                        f"\n\n[… {omitted} characters of the spoken request omitted to fit the "
                        f"{MAX_REQUEST_MESSAGE_CHARS}-character message limit. This request is incomplete; "
                        "ask the user to repeat the missing part.]"
                    )
                    next_prefix_length = MAX_REQUEST_MESSAGE_CHARS - len(notice)
                    if next_prefix_length == prefix_length:
                        break
                    prefix_length = next_prefix_length
                message = message[:prefix_length] + notice
                self._emit("live.error", {
                    "message": f"A spoken request exceeded the {MAX_REQUEST_MESSAGE_CHARS}-character message limit; {omitted} characters were omitted."
                })
            mode = "steer" if self._host.turn_running(self._session) else "queue"
            try:
                queued_id = await self._host.enqueue(
                    self._session, message, mode, {"kind": "live", "delegation_id": delegation_id},
                    title=text or facts["transcript"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - tell the user instead of leaving the voice waiting
                log.warning("live bridge could not enqueue a delegation", exc_info=True)
                self._emit("live.error", {
                    "message": f"Could not hand a spoken request to the agent: {_clip(redact_secrets(str(exc)), 200)}"
                })
                await self._append(
                    "The request could not be sent to the agent, so nothing was started. "
                    "Tell the user it failed and ask them to try again.", "notice", delegation_id,
                )
                return
            # The tap may already have seen this input queued *and* consumed (a quick tap):
            # re-adding the mapping then would leave a stale entry that nothing ever pops.
            if queued_id not in self._settled:
                self._queued.setdefault(queued_id, delegation_id)
            heard = " ".join(heard.split())[:MAX_HEARD_CHARS]
            self._emit("live.delegated", {
                "delegation_id": delegation_id, "text": text or facts["transcript"], "mode": mode, "queued_id": queued_id,
                "source": "delegation", "heard": heard,
                "verbatim": bool(heard) and _norm(heard) == _norm(text), **facts,
            })
            self._set_phase("working")

    # -- session events ----------------------------------------------------

    async def _run(self) -> None:
        try:
            async for event in self._tap:
                try:
                    await self.handle_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad event never stops the bridge
                    log.warning("live bridge could not handle %s", getattr(event, "type", "?"), exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("live bridge tap ended", exc_info=True)

    async def handle_event(self, event: Any) -> None:
        kind = getattr(event, "type", "")
        data = event.data if isinstance(getattr(event, "data", None), Mapping) else {}
        if _is_subagent(data):
            return
        turn = getattr(event, "turn", None) or ""
        if kind == "input.queued":
            origin = data.get("origin")
            if isinstance(origin, Mapping) and origin.get("kind") == "live" and origin.get("delegation_id"):
                self._queued[str(data.get("queued_id", ""))] = str(origin["delegation_id"])
                if len(self._queued) > 256:
                    self._queued.pop(next(iter(self._queued)))
        elif kind == "input.consumed":
            delegation = self._settle(str(data.get("queued_id", "")))
            consumed_turn = str(data.get("turn") or turn)
            if delegation and consumed_turn:
                self._turns.setdefault(consumed_turn, []).append(delegation)
                while len(self._turns) > MAX_TRACKED_TURNS:
                    self._turns.pop(next(iter(self._turns)))
        elif kind == "input.dropped":
            delegation = self._settle(str(data.get("queued_id", "")))
            if delegation:
                await self._append(
                    f"The request was not run: {_clip(str(data.get('reason') or 'it was dropped'), 200)}.",
                    "notice", delegation,
                )
        elif kind == "text" and turn:
            text = str(data.get("text") or "")
            if text.strip():
                self._texts[turn] = text
                while len(self._texts) > MAX_TRACKED_TURNS:
                    self._texts.pop(next(iter(self._texts)))
        elif kind == "tool.requested" and turn:
            await self._on_tool(turn, str(data.get("tool") or "a tool"))
        elif kind == "permission.requested":
            await self._on_permission(turn, data)
        elif kind == "question.requested":
            await self._on_question(turn, data)
        elif kind in ("turn.completed", "turn.failed", "turn.cancelled") and turn:
            await self._on_terminal(kind, turn, data)

    def _settle(self, queued_id: str) -> str | None:
        """Pop a queued mapping and remember the id as finished (bounded)."""
        delegation = self._queued.pop(queued_id, None)
        if delegation:
            self._settled[queued_id] = None
            while len(self._settled) > 256:
                self._settled.pop(next(iter(self._settled)))
        return delegation

    async def _on_tool(self, turn: str, tool: str) -> None:
        names = self._tools.setdefault(turn, [])
        if tool not in names:
            names.append(tool)
        delegations = self._turns.get(turn)
        now = self._clock()
        if not self._progress or not delegations or now - self._last_progress < self._interval:
            return
        self._last_progress = now
        shown = names[:MAX_TOOLS_IN_PROGRESS]
        more = f" and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
        names.clear()
        await self._append(f"Agent progress: working with {', '.join(shown)}{more}.", "commentary", delegations[-1])

    async def _on_permission(self, turn: str, data: Mapping[str, Any]) -> None:
        delegations = self._turns.get(turn)
        if not delegations:
            return
        tool = _clip(str(data.get("tool") or "a tool"), 60)
        preview = _clip(redact_secrets(str(data.get("preview") or data.get("key") or "")), 120)
        what = f"{tool}: {preview}" if preview else tool
        await self._append(
            f"The agent is waiting for approval to use {what}. The user must approve or deny this on screen; "
            "it cannot be approved by voice.",
            "notice", delegations[-1],
        )

    async def _on_question(self, turn: str, data: Mapping[str, Any]) -> None:
        delegations = self._turns.get(turn)
        if not delegations:
            return
        prompt = _clip(str(data.get("prompt") or ""), 200)
        await self._append(
            f"The agent has a question for the user{': ' + prompt if prompt else ''}. "
            "The user must answer it on screen.",
            "notice", delegations[-1],
        )

    async def _on_terminal(self, kind: str, turn: str, data: Mapping[str, Any]) -> None:
        delegations = self._turns.pop(turn, [])
        text = self._texts.pop(turn, "")
        self._tools.pop(turn, None)
        if not delegations:
            return
        *earlier, last = delegations
        for delegation in earlier:
            await self._append(
                "This request was merged into a later one; its result comes with that request.", "notice", delegation
            )
        if kind == "turn.completed":
            body = text.strip() or "The agent finished without a written reply."
            if len(body) > MAX_FINAL_CHARS:
                body = body[:MAX_FINAL_CHARS] + f"\n[{len(body) - MAX_FINAL_CHARS} more characters omitted]"
            await self._append(body, "final", last)
        elif kind == "turn.failed":
            await self._append(
                f"The agent's work on this request failed: {_clip(str(data.get('error') or 'unknown error'), 300)}.",
                "notice", last,
            )
        else:
            await self._append("The agent's work on this request was cancelled.", "notice", last)
        self._set_phase("listening")


__all__ = ["LiveBridge", "MAX_FINAL_CHARS", "PROGRESS_INTERVAL_S"]
