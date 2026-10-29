"""Read-only export of one session, or one session and its descendants.

Two formats: a readable Markdown transcript and a structured JSON document. The
export is a client-facing operation over the daemon's HTTP API; it never writes
to the database, never calls a provider, never reconstructs a missing snapshot,
never runs a tool and never reads a workspace file or a credential.

**One boundary.** `ExportView` opens its own SQLite connection in read-only mode
and starts one read transaction. Every session row, message and event is read
inside that transaction, so the export is one coherent database view even while
turns keep writing. The boundary is reported as a watermark (the highest stored
event row id and bus `seq`, per-session counts and the newest session `updated`)
and as an explicit running/durable-partial state. SQLite's WAL mode lets writers
continue while the read transaction is open.

**Large exports.** Events are read in pages (`EVENT_PAGE`) with `id > after`
until a short page, never with the HTTP event endpoint's paging cap, and both
formats stream their output. A large log is therefore complete and memory stays
bounded.

**Snapshots.** Only the stored `session_snapshot` is exported. A session from
before snapshots existed has no historical prompt; the export labels that and
does not reconstruct one. An `agent-switch` snapshot is labelled as the
configuration after the switch, never as the original prompt.

**Transport metadata.** Structured event data is passed through
`redact_headers` for transport headers on wire request/response events only.
Message text and tool output are preserved as stored; no text-pattern redaction
is applied.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from .llmhub.responses import redact_headers
from .snapshot import RECONSTRUCTED_NOTE

FORMAT_NAME = "nexus.session-export"
FORMAT_VERSION = 1
FORMATS = ("markdown", "json")
SCOPES = ("session", "tree")
EVENT_PAGE = 1000

MEDIA_TYPES = {"markdown": "text/markdown; charset=utf-8", "json": "application/json; charset=utf-8"}
EXTENSIONS = {"markdown": "md", "json": "json"}

NOTICE_PREFIXES = ("[Nexus notice:", "[Nexus recovery:", "[Notification:", "→ Background Shell Done:")


class ExportOptions(ValueError):
    """An unknown format or scope."""


def check_options(format_: str | None, scope: str | None) -> tuple[str, str]:
    if format_ not in FORMATS:
        raise ExportOptions(f"format must be 'markdown' or 'json' (got {format_!r})")
    scope = scope or "session"
    if scope not in SCOPES:
        raise ExportOptions(f"scope must be 'session' or 'tree' (got {scope!r})")
    return format_, scope


def filename(root_id: str, scope: str, format_: str, generated: float) -> str:
    stamp = datetime.fromtimestamp(generated, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"nexus-{root_id}-{scope}-{stamp}.{EXTENSIONS[format_]}"


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _inline(value: Any) -> str:
    """One-line display text for metadata (newlines and tabs collapsed)."""
    return " ".join(str(value).split())


@dataclass(frozen=True, slots=True)
class Boundary:
    generated: float
    generation: str
    event_id: int
    seq: int
    session_updated: float
    counts: dict[str, dict[str, int]]
    floor: int = 0
    history_truncated: bool = False


class ExportView:
    """A coherent, read-only view of one session or session tree."""

    def __init__(self, path: str | Path, root_id: str, scope: str,
                 running: set[str] | None = None) -> None:
        if scope not in SCOPES:
            raise ExportOptions(f"scope must be 'session' or 'tree' (got {scope!r})")
        self.root_id = root_id
        self.scope = scope
        self.running = set(running or ())
        # A separate read-only connection keeps the daemon's writer connection free.
        uri = f"file:{quote(str(Path(path)), safe='/')}?mode=ro"
        self.db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._closed = False
        try:
            self.db.execute("begin")
            # The first read establishes the snapshot, so everything below sees one database view.
            if self.db.execute("select 1 from session where id = ?", (root_id,)).fetchone() is None:
                raise KeyError(f"no session {root_id}")
            self.sessions = self._session_rows()
            self.boundary = self._boundary()
        except BaseException:
            self.db.close()
            self._closed = True
            raise
        self.running_sessions = sorted({row["id"] for row in self.sessions} & self.running)

    # -- opening and closing -------------------------------------------------

    def _session_rows(self) -> list[dict[str, Any]]:
        if self.scope == "session":
            row = self.db.execute("select * from session where id = ?", (self.root_id,)).fetchone()
            return [dict(row) | {"depth": 0}]
        # Read the subtree, then walk it parent-first (stable: created, id). A
        # depth-first order keeps every child directly under its own parent in
        # the Markdown outline, including nested user-created trees.
        rows = self.db.execute(
            "with recursive tree(id) as ("
            " select id from session where id = ?"
            " union all select s.id from session s join tree on s.parent_id = tree.id)"
            " select session.* from session join tree on session.id = tree.id",
            (self.root_id,),
        ).fetchall()
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            by_parent.setdefault(row["parent_id"], []).append(dict(row))
        root = next((row for row in by_parent.get(None, []) if row["id"] == self.root_id), None)
        if root is None:  # the selected session is itself a child; find it by id
            root = next(row for group in by_parent.values() for row in group if row["id"] == self.root_id)
        root["depth"] = 0
        ordered: list[dict[str, Any]] = [root]

        def walk(parent: str, depth: int) -> None:
            for item in sorted(by_parent.get(parent, []), key=lambda r: (r["created"], r["id"])):
                item["depth"] = depth
                ordered.append(item)
                walk(item["id"], depth + 1)

        walk(self.root_id, 1)
        return ordered

    def _boundary(self) -> Boundary:
        event = self.db.execute("select coalesce(max(id), 0) as id, coalesce(max(seq), 0) as seq from event").fetchone()
        floor = 0
        high = event["id"]
        try:
            log = self.db.execute("select generation, floor, head_high from log where id = 1").fetchone()
            if log is not None:
                floor = log["floor"]
                high = max(high, log["head_high"])
        except sqlite3.OperationalError:  # a database written before the log table; export stays read-only
            log = None
        counts: dict[str, dict[str, int]] = {}
        for row in self.sessions:
            counts[row["id"]] = {
                "messages": self.db.execute(
                    "select count(*) from message where session_id = ?", (row["id"],)
                ).fetchone()[0],
                "events": self.db.execute(
                    "select count(*) from event where session_id = ?", (row["id"],)
                ).fetchone()[0],
            }
        return Boundary(
            generated=time.time(),
            generation=log["generation"] if log else "",
            event_id=high,
            seq=event["seq"],
            session_updated=max((row["updated"] for row in self.sessions), default=0.0),
            counts=counts,
            floor=floor,
            history_truncated=floor > 0,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.db.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- records (all inside the boundary transaction) ------------------------

    def session_record(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            disabled = sorted(set(json.loads(row["disabled_tools"] or "[]")))
        except (TypeError, ValueError):
            disabled = []
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "directory": row["directory"],
            "title": row["title"],
            "model": row["model"] or None,  # null = the agent's or the daemon default applies
            "permission": row["permission"],
            "effort": row["effort"],
            "agent": row["agent"],
            "parent_id": row["parent_id"],
            "parent_call_id": row["parent_call_id"],
            "launched_by": row["launched_by"],
            "disabled_tools": disabled,
            "archived": bool(row["archived"]),
            "created": row["created"],
            "updated": row["updated"],
        }

    def children_of(self, session_id: str) -> list[str]:
        rows = self.db.execute(
            "select id from session where parent_id = ? order by created, id", (session_id,)
        ).fetchall()
        return [row["id"] for row in rows]

    def snapshot(self, session_id: str) -> tuple[dict[str, Any] | None, float | None]:
        row = self.db.execute(
            "select data, created from session_snapshot where session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None, None
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            return None, row["created"]
        # A rebuilt snapshot keeps the table row's original `created`, so use the
        # snapshot's own timestamp when it has one (agent switch, reconstruction).
        created = data.get("created") if isinstance(data, dict) else None
        if not isinstance(created, (int, float)):
            created = row["created"]
        return data, created

    def messages(self, session_id: str) -> Iterator[dict[str, Any]]:
        cursor = self.db.execute(
            "select * from message where session_id = ? order by seq, id", (session_id,)
        )
        for row in cursor:
            yield {
                "id": row["id"],
                "session_id": row["session_id"],
                "seq": row["seq"],
                "created": row["created"],
                "role": row["role"],
                "parts": json.loads(row["parts"]),
            }

    def events(self, session_id: str) -> Iterator[dict[str, Any]]:
        """Every stored event, paged by row id; never truncated at the API cap."""
        after = 0
        while True:
            rows = self.db.execute(
                "select * from event where session_id = ? and id > ? order by id limit ?",
                (session_id, after, EVENT_PAGE),
            )
            count = 0
            for row in rows:
                count += 1
                after = row["id"]
                yield {
                    "id": row["id"],
                    "seq": row["seq"],
                    "time": row["time"],
                    "type": row["type"],
                    "session_id": row["session_id"],
                    "data": redact_transport(row["type"], json.loads(row["data"])),
                }
            if count < EVENT_PAGE:
                break

    def counts(self, session_id: str) -> dict[str, int]:
        return dict(self.boundary.counts.get(session_id, {"messages": 0, "events": 0}))

    def state(self) -> dict[str, Any]:
        running = list(self.running_sessions)
        if running:
            note = (
                "At the export boundary one or more selected sessions were running a turn. "
                "This export is their durable stored state up to that boundary: later output is not "
                "included, and the turn was not interrupted or waited for."
            )
        else:
            note = (
                "No selected session was running a turn at the export boundary. The export is the "
                "stored state captured there; it is not a claim that the work itself is finished."
            )
        return {
            "running": bool(running),
            "running_sessions": running,
            "durable_partial": bool(running),
            "note": note,
        }

    def meta(self) -> dict[str, Any]:
        return {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "generated": self.boundary.generated,
            "generated_at": _iso(self.boundary.generated),
            "scope": self.scope,
            "root": self.root_id,
            "session_count": len(self.sessions),
            "state": self.state(),
            "boundary": {
                "log_generation": self.boundary.generation,
                "event_id": self.boundary.event_id,
                "seq": self.boundary.seq,
                "floor": self.boundary.floor,
                "history_truncated": self.boundary.history_truncated,
                "session_updated": self.boundary.session_updated,
                "counts": self.boundary.counts,
                "note": (
                    "All records were read inside one SQLite read transaction. Writes after this "
                    "boundary are not in this export. `seq` is the publishing daemon's counter; "
                    "`event_id` is durable, and a resume cursor is `<log_generation>:<event_id>`. "
                    "When `history_truncated` is true, diagnostic events at or below `floor` expired "
                    "before this export; messages and snapshots remain complete."
                ),
            },
        }


def redact_transport(type_: str, value: Any) -> Any:
    """Redact actual transport headers, preserving request bodies and tool data."""
    if type_ in ("wire_request", "wire_response") and isinstance(value, dict):
        headers = value.get("headers")
        if isinstance(headers, dict):
            return {**value, "headers": redact_headers(headers)}
    return value


# -- Markdown ----------------------------------------------------------------


def _fence(text: str, lang: str = "") -> str:
    """A fence longer than any backtick run in `text`, so the content cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _snapshot_notice(origin: str | None, created: float | None, present: bool) -> str:
    if not present:
        return (
            "No stored snapshot exists for this session. Its original prompt is unknown; this export "
            "does not reconstruct one."
        )
    when = f" at {_iso(created)}" if created else ""
    if origin == "created":
        return (
            f"Recorded when the session was created{when}. Its frozen agent prompt is the one this "
            "session started with; later explicit changes (tools, model, directory or agent) are "
            "recorded forward in the stored messages, and an agent switch replaces this snapshot."
        )
    if origin == "agent-switch":
        return (
            f"Rebuilt when the session's agent was switched{when}. This is the configuration after "
            "the switch, not the session's original historical prompt. Earlier requests used the "
            "earlier prompt, which is not retained in this snapshot. Historical wire_request events in the JSON "
            "export may retain those requests."
        )
    if origin == "reconstructed":
        return RECONSTRUCTED_NOTE
    return (
        f"Snapshot origin {origin!r}{when}. The export reports the stored snapshot as it is; it is "
        "not presented as the original historical prompt."
    )


def _part_label(part: dict[str, Any]) -> str:
    kind = part.get("type")
    return {"text": "Text", "reasoning": "Reasoning", "tool_call": "Tool call",
            "tool_result": "Tool result"}.get(kind, f"Unknown part ({kind})")


def _wire_line(part: dict[str, Any]) -> str:
    origin = part.get("origin")
    raw = part.get("raw")
    pieces = []
    if origin:
        pieces.append(f"origin `{_inline(origin)}`")
    pieces.append("raw wire item: present" if raw is not None else "raw wire item: none")
    return " · ".join(pieces)


def _markdown_part(part: dict[str, Any]) -> Iterator[str]:
    kind = part.get("type")
    yield f"##### {_part_label(part)}\n\n"
    if kind == "reasoning":
        summary = part.get("summary") or ""
        raw = part.get("raw")
        encrypted = raw.get("encrypted_content") if isinstance(raw, dict) else None
        if summary:
            yield "Readable reasoning summary:\n\n"
            yield _fence(summary, "text") + "\n\n"
        elif not encrypted:
            yield "No readable reasoning text was stored.\n\n"
        if encrypted:
            yield (
                f"Encrypted reasoning: present ({len(str(encrypted)):,} characters of encrypted "
                "state) and not readable. It is replayed unchanged to the same provider.\n\n"
            )
        yield _wire_line(part) + "\n\n"
        return
    if kind == "tool_call":
        yield f"Name: `{_inline(part.get('name'))}` · call id: `{_inline(part.get('id'))}`\n\n"
        arguments = part.get("arguments") or ""
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            yield "Arguments (formatted JSON; the stored JSON text is preserved in the JSON export):\n\n"
            yield _fence(json.dumps(parsed, indent=2, ensure_ascii=False), "json") + "\n\n"
        else:
            yield "Arguments (not valid JSON; reproduced verbatim):\n\n"
            yield _fence(str(arguments), "text") + "\n\n"
        yield _wire_line(part) + "\n\n"
        return
    if kind == "tool_result":
        flag = " (error)" if part.get("is_error") else ""
        yield f"Call id: `{_inline(part.get('call_id'))}`{flag}\n\n"
        yield _fence(str(part.get("output") or ""), "text") + "\n\n"
        return
    if kind == "image":
        yield f"Image: {_inline(part.get('name'))}\n\n"
        yield f"![Image]({part.get('data_url', '')})\n\n"
        return
    if kind == "text":
        yield _fence(str(part.get("text") or ""), "text") + "\n\n"
        yield _wire_line(part) + "\n\n"
        return
    yield _fence(json.dumps(part, indent=2, ensure_ascii=False), "json") + "\n\n"


def _notice_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    return any(
        str(part.get("text") or "").startswith(NOTICE_PREFIXES)
        for part in message.get("parts", [])
        if part.get("type") == "text"
    )


def _markdown_message(message: dict[str, Any]) -> Iterator[str]:
    stamp = _iso(message["created"]) if message.get("created") else "unknown time"
    yield (
        f"#### Message {message.get('seq')} · {message.get('role')} · `{message.get('id')}`\n\n"
        f"_{stamp} · stored in session order_\n\n"
    )
    if _notice_message(message):
        yield "Kind: context notice stored as a user message for provider context; it may not yet have been sent.\n\n"
    if not message.get("parts"):
        yield "No parts were stored for this message.\n\n"
    for part in message.get("parts") or []:
        yield from _markdown_part(part)


def _markdown_session(view: ExportView, row: dict[str, Any], in_export: set[str]) -> Iterator[str]:
    sid = row["id"]
    session = view.session_record(row)
    counts = view.counts(sid)
    children = view.children_of(sid)
    included_children = [child for child in children if child in in_export]
    snapshot, snapshot_created = view.snapshot(sid)
    origin = snapshot.get("origin") if isinstance(snapshot, dict) else None
    notice = _snapshot_notice(origin, snapshot_created, snapshot is not None)
    running = sid in view.running_sessions

    yield "\n---\n\n"
    yield f"## Session `{sid}`\n\n"
    yield f"- Title: {_inline(session['title']) or '(untitled)'}\n"
    yield f"- Agent: `{_inline(session['agent'])}`\n"
    model = session["model"]
    yield f"- Model: `{_inline(model)}` (set on this session)\n" if model else \
        "- Model: not set on this session (the agent's or daemon's default applies)\n"
    yield f"- Effort: {_inline(session['effort'])}\n" if session["effort"] else \
        "- Effort: not set on this session (the agent's or provider's default applies)\n"
    yield f"- Permission: `{_inline(session['permission'])}`\n"
    yield f"- Directory: `{_inline(session['directory'])}`\n"
    parent = session["parent_id"]
    if parent is None:
        yield "- Parent: none (root session)\n"
    elif parent in in_export:
        yield f"- Parent: `{parent}` (included in this export)\n"
    else:
        yield (f"- Parent: `{parent}` (not included in this session-only export; export from the "
               "root with the tree scope to include it)\n")
    if session["parent_call_id"]:
        yield f"- Launched by: {_inline(session['launched_by'])} via call `{_inline(session['parent_call_id'])}`\n"
    else:
        yield f"- Launched by: {_inline(session['launched_by'])}\n"
    if children:
        outside = [child for child in children if child not in included_children]
        text = ", ".join(f"`{child}`" for child in included_children) or "none"
        yield f"- Children in this export: {text}\n"
        if outside:
            yield (f"- Other children not in this export: {', '.join(f'`{child}`' for child in outside)} "
                   "(their conversations are separate contexts; use the tree scope)\n")
    else:
        yield "- Children: none\n"
    yield f"- Archived: {'yes' if session['archived'] else 'no'}\n"
    yield f"- Created: {_iso(session['created'])} · Updated: {_iso(session['updated'])}\n"
    if snapshot is not None:
        agent = snapshot.get("agent") or {}
        yield (f"- Snapshot: version {snapshot.get('version')} · origin `{_inline(origin)}` · stored "
               f"{_iso(snapshot_created) if snapshot_created else 'unknown'}\n")
        yield f"- Snapshot agent: `{_inline(agent.get('name'))}` · source `{_inline(agent.get('source'))}`"
        if agent.get("fingerprint"):
            yield f" · fingerprint `{_inline(agent['fingerprint'])}`"
        yield "\n"
        instruction = snapshot.get("instruction_file") or {}
        yield (f"- Instruction file: `{_inline(instruction.get('path'))}` · status "
               f"`{_inline(instruction.get('status'))}`\n")
    else:
        yield "- Snapshot: none stored\n"
    yield f"- Messages: {counts['messages']} · Events: {counts['events']}\n"
    if running:
        yield ("- Status at the boundary: **running** — this is the durable partial state; the turn "
               "was not interrupted or waited for and later output is not included.\n")
    else:
        yield "- Status at the boundary: not running\n"
    yield f"\n**Snapshot note:** {notice}\n\n"
    if isinstance(snapshot, dict):
        yield "### Stored snapshot instructions\n\n"
        yield "This is the snapshot described above, not a reconstruction of earlier request prompts.\n\n"
        if isinstance(snapshot.get("system_prompt"), str):
            yield _fence(snapshot["system_prompt"], "text") + "\n\n"
        else:
            yield "No system prompt was stored in this snapshot.\n\n"
        yield "### Stored snapshot tool definitions\n\n"
        yield _fence(json.dumps(snapshot.get("tool_definitions", {}), indent=2, ensure_ascii=False), "json") + "\n\n"

    if counts["messages"] == 0:
        yield "No messages were stored for this session.\n"
        return
    yield "### Stored context, in order\n\n"
    for message in view.messages(sid):
        yield from _markdown_message(message)


def markdown_export(view: ExportView) -> Iterator[str]:
    """A readable transcript; each session keeps its own context section."""
    meta = view.meta()
    selected = {row["id"] for row in view.sessions}
    running = ", ".join(f"`{sid}`" for sid in view.running_sessions)
    yield "# Nexus session export\n\n"
    yield f"- Format version: {FORMAT_VERSION}\n"
    yield f"- Exported: {meta['generated_at']}\n"
    yield f"- Scope: `{view.scope}` · root `{view.root_id}` · sessions {meta['session_count']}\n"
    yield (f"- Boundary: event id {view.boundary.event_id} · bus seq {view.boundary.seq} · "
           f"one SQLite read transaction\n")
    if running:
        yield f"- State: durable partial state; running at the boundary: {running}\n"
    else:
        yield "- State: durable stored state at the boundary; no selected session was running\n"
    if view.boundary.history_truncated:
        yield (f"- Event retention: earlier diagnostic events expired at floor {view.boundary.floor}; "
               "the JSON event log starts after that boundary. Messages and snapshots are complete.\n")
    yield (
        "\nThe export is read-only. A child session's conversation is its own context and appears in "
        "its own section, never inside its parent's messages. Provider stream deltas live in the JSON "
        "export's event log; this transcript shows stored messages. No additional clipping is applied: "
        "existing clip notices are reproduced as stored.\n\n"
    )
    yield "## Session tree\n\n"
    for row in view.sessions:
        sid = row["id"]
        depth = int(row.get("depth") or 0)
        role = "root of the export" if depth == 0 and row["parent_id"] is None else \
            ("selected session" if depth == 0 else "descendant")
        yield f"{'  ' * depth}- `{sid}` — {_inline(row['title']) or '(untitled)'} ({role})"
        if row["parent_id"] and depth > 0:
            yield f" — parent `{row['parent_id']}`"
        if row["parent_call_id"]:
            yield f" · launched by {_inline(row['launched_by'])} via `{row['parent_call_id']}`"
        yield "\n"
    for row in view.sessions:
        yield from _markdown_session(view, row, selected)


# -- JSON --------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_session(view: ExportView, row: dict[str, Any], in_export: set[str]) -> Iterator[str]:
    sid = row["id"]
    children = view.children_of(sid)
    snapshot, snapshot_created = view.snapshot(sid)
    origin = snapshot.get("origin") if isinstance(snapshot, dict) else None
    header = {
        "session": view.session_record(row),
        "tree": {
            "depth": int(row.get("depth") or 0),
            "in_export": True,
            "parent_in_export": row["parent_id"] in in_export if row["parent_id"] else False,
            "children": children,
            "children_in_export": [child for child in children if child in in_export],
        },
        "snapshot": snapshot,
        "snapshot_origin": origin,
        "snapshot_created": snapshot_created,
        "snapshot_notice": _snapshot_notice(origin, snapshot_created, snapshot is not None),
        "state": {
            "running": sid in view.running_sessions,
            "durable_partial": sid in view.running_sessions,
        },
        "counts": view.counts(sid),
    }
    text = _dumps(header)
    yield text[:-1]  # reopen the object to stream messages and events
    yield ',"messages":['
    first = True
    for message in view.messages(sid):
        if not first:
            yield ","
        first = False
        yield _dumps(message)
    yield '],"events":['
    first = True
    for event in view.events(sid):
        if not first:
            yield ","
        first = False
        yield _dumps(event)
    yield "]}"


def json_export(view: ExportView) -> Iterator[str]:
    meta = _dumps(view.meta())
    yield meta[:-1] + ',"sessions":['
    selected = {row["id"] for row in view.sessions}
    first = True
    for row in view.sessions:
        if not first:
            yield ","
        first = False
        yield from _json_session(view, row, selected)
    yield "]}"


def stream(view: ExportView, format_: str) -> Iterator[str]:
    """Yield one export and close its read-only view when the response ends."""
    producer = markdown_export if format_ == "markdown" else json_export
    try:
        yield from producer(view)
    finally:
        view.close()
