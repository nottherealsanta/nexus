"""Pure formatting and filtering for reducer-backed conversation timelines."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..ui_support.text import escape_controls, sanitize
from ..view import AgentView, MessageView, ToolCallView, TurnView

_DETAIL_LIMIT = 1_600
_ARG_LIMIT = 180
_STALE_GREETINGS = (
    "hi! how can i help?",
    "i’m nexus, your assistant.",
    "i'm nexus, your assistant.",
)
_SETUP_FAILURE_MARKERS = (
    "configerror", "providererror", "authentication_error", "authentication error",
    "unauthorized", "invalid api key", "no api key", "credentials",
)


def _text(value: object, limit: int = _DETAIL_LIMIT) -> str:
    return sanitize(value, limit)


def _literal(value: object, limit: int = _DETAIL_LIMIT) -> str:
    """Bound terminal data without interpreting it as Markdown or Rich markup."""
    return escape_controls(str(value))[:limit]


def _output(tool: ToolCallView) -> str:
    if tool.display:
        return _literal(tool.display)
    if tool.context_note:
        return _literal(tool.context_note)
    if not tool.result:
        return ""
    parts: list[str] = []
    for block in tool.result[:8]:
        value = block.get("text", block.get("content", block)) if isinstance(block, Mapping) else block
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True, default=str)
        if value:
            parts.append(_literal(value, 400))
    return "\n".join(parts)[:_DETAIL_LIMIT]


def _first_line(text: str, limit: int = _ARG_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _setup_failure(text: str | None) -> bool:
    lowered = (text or "").casefold()
    return any(marker in lowered for marker in _SETUP_FAILURE_MARKERS)


def _stale_greeting(message: MessageView) -> bool:
    return message.role == "assistant" and message.text.strip().casefold() in _STALE_GREETINGS


def _message_markdown(message: MessageView) -> str:
    """Render assistant text; thoughts render separately as a Thought line."""
    if message.role == "user":
        return message.text
    return "\n\n".join(block.text for block in message.blocks if block.kind == "text" and block.text)


def thought_title(text: str, limit: int = 96) -> str:
    """One-line summary of provider thinking: its first heading or sentence."""
    for raw in text.splitlines():
        line = re.sub(r"[*_`#]+", "", raw).strip()
        if line:
            return _first_line(_text(re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip("."), limit), limit)
    return "Thinking"


_TOOL_VERBS = {
    "read": "→ Read", "ls": "→ List", "glob": "✱ Glob", "grep": "✱ Grep",
    "edit": "← Edit", "multiedit": "← Edit", "write": "← Write", "apply_patch": "← Patch",
    "webfetch": "% Fetch", "websearch": "◈ Search", "todowrite": "☐ Todo", "skill": "◇ Skill",
    "task": "◉ Task", "subagent": "◉ Task", "question": "? Question",
}
_RAW_OUTPUT_TOOLS = frozenset({"bash", "bashoutput", "glob", "grep", "ls"})

@dataclass(frozen=True)
class ToolGroup:
    """A stable native activity group; canonical formatting stays stable."""
    id: str
    members: tuple[ToolCallView, ...]
    failures: int
    running: bool

    @property
    def latest(self):
        return next((tool for tool in reversed(self.members) if tool_status(tool) == "running"), self.members[-1])


def group_tools(turn: TurnView) -> list[ToolGroup]:
    """Consecutive calls, separated by visible messages and standalone tasks."""
    events = sorted([(message.event_seq, 0, message) for message in turn.messages if message.text or message.thinking]
                    + [(tool.event_seq, 1, tool) for tool in turn.tools], key=lambda row: (row[0], row[1]))
    groups, pending = [], []
    def finish():
        if pending:
            members = tuple(pending)
            groups.append(ToolGroup(f"{turn.id}:g{members[0].call_id}", members,
                sum(tool_status(tool) == "failed" for tool in members),
                any(tool_status(tool) == "running" for tool in members)))
            pending.clear()
    for _, kind, value in events:
        if kind == 0:
            finish()
        elif value.name.casefold() in {"task", "subagent"}:
            finish()
            pending.append(value)
            finish()
        else:
            pending.append(value)
    finish()
    return groups


def tool_heading(tool: ToolCallView) -> str:
    """``$ command`` for shells, ``→ Read path`` style for everything else."""
    name = tool.name.casefold()
    if name == "mcpcall":
        target = tool.target or str(tool.input.get("tool", ""))
        label = target.removeprefix("mcp__").replace("__", " · ").replace("/", " · ")
        return "⚙ " + _text(label, 120) + " · via McpCall"
    args = format_arguments(tool)
    if name in {"bash", "bashoutput", "killshell"}:
        return f"$ {args}" if args else "$"
    verb = _TOOL_VERBS.get(name, f"⚙ {_text(tool.name or 'tool', 40)}")
    return f"{verb} {args}".rstrip()


def todo_preview(tool: ToolCallView) -> list[str]:
    """Five item rows, or four items and a remaining count."""
    if tool.name.casefold() != "todowrite" or tool_status(tool) == "failed":
        return []
    args = tool.input if isinstance(tool.input, Mapping) else {}
    todos = tool.metrics.get("todos", args.get("todos")) if isinstance(tool.metrics, Mapping) else args.get("todos")
    if not isinstance(todos, (list, tuple)):
        return []
    glyphs = {"pending": "☐", "in_progress": "◐", "completed": "✓", "cancelled": "✗"}
    shown = todos[:4] if len(todos) > 5 else todos
    rows = [f"{glyphs.get(str(item.get('status')), '☐')} {_first_line(_text(item.get('content', ''), 180))}"
            for item in shown if isinstance(item, Mapping)]
    if len(todos) > 5:
        rows.append(f"{len(todos) - 4} more")
    return rows


def tool_output(tool: ToolCallView) -> str:
    """The body a tool block shows: raw output for shells and searches."""
    if tool.error:
        return _literal(tool.error)
    if tool.name.casefold() in _RAW_OUTPUT_TOOLS and tool.result:
        parts = [
            _literal(block.get("text", ""), _DETAIL_LIMIT)
            for block in tool.result[:8] if isinstance(block, Mapping) and block.get("text")
        ]
        if parts:
            return "\n".join(parts)[:_DETAIL_LIMIT]
    if tool.progress and tool_status(tool) == "running":
        return "\n".join(_literal(item, 300) for item in tool.progress[-10:])
    if tool.name.casefold() == "write":
        return tool_summary(tool)
    return _output(tool)


def _has_message_content(message: MessageView) -> bool:
    return bool(message.text or (message.role == "assistant" and message.thinking))


def _turn_setup_failure(turn: TurnView) -> bool:
    """Match setup failures only against structured error fields."""
    return _setup_failure(turn.error) or any(_setup_failure(tool.error) for tool in turn.tools)


def format_arguments(tool: ToolCallView) -> str:
    """Return useful, bounded arguments without exposing write payloads."""
    args = tool.input if isinstance(tool.input, dict) else {}
    name = tool.name.casefold()
    path = args.get("path") or args.get("file_path") or ""
    if name == "read":
        extras = [f"{key}={_text(args[key], 32)}" for key in ("offset", "limit") if key in args]
        return _first_line(f"{_text(path, 120)}  ({', '.join(extras)})" if extras else _text(path, 180))
    if name == "write":
        content = args.get("content")
        lines = content.count("\n") + 1 if isinstance(content, str) and content else 0
        return _first_line(f"{_text(path, 120)} ({lines} lines)" if lines else _text(path, 180))
    if name in {"edit", "multiedit"}:
        return _first_line(_text(path, 180))
    if name == "apply_patch":
        return _first_line(_text(", ".join(_patch_paths(args.get("patch"))), 180))
    if name in {"bash", "bashoutput", "killshell"}:
        return _first_line(_text(args.get("command") or args.get("id") or ""))
    if name == "task":
        return _first_line(_text(args.get("description") or args.get("task") or args.get("prompt") or ""))
    pairs = [
        f"{key}={_first_line(_text(value), 48)}"
        for key, value in args.items()
        if key not in {"content", "old_string", "new_string"}
    ]
    return _first_line(", ".join(pairs))


_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete|Move) File: (.+?)(?: -> .+)?$", re.MULTILINE)


def _patch_paths(patch: object) -> list[str]:
    """The files an ``apply_patch`` body touches, never its content."""
    if not isinstance(patch, str):
        return []
    paths = list(dict.fromkeys(_PATCH_FILE.findall(patch[:200_000])))
    return paths[:3] + [f"+{len(paths) - 3} more"] if len(paths) > 3 else paths


def tool_status(tool: ToolCallView) -> str:
    if tool.is_error or tool.status == "failed":
        return "failed"
    return "running" if tool.status in {"requested", "running"} else tool.status


def tool_summary(tool: ToolCallView) -> str:
    output = _output(tool)
    if tool.error:
        return _first_line(_text(tool.error))
    if tool.is_error:
        return "failed"
    if tool.progress:
        return _first_line(_text(tool.progress[-1]))
    if tool.name.casefold() == "read":
        return "read" if not output else _first_line(output)
    if tool.name.casefold() == "write":
        return "written" if tool.status == "completed" else tool.status
    return _first_line(output) if output else tool.status


#: Tools whose ``diff`` artifact is shown inline under their activity row.
DIFF_TOOLS = frozenset({"edit", "multiedit", "apply_patch"})
#: Beyond this line number a hunk is drawn with relative numbers, unpadded.
_MAX_DIFF_PAD = 50_000
_HUNK_START = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


@dataclass(frozen=True, slots=True)
class DiffSection:
    """One file of a durable diff artifact, as before/after text for a viewer.

    Both texts are padded with blank lines up to each hunk's start so a viewer
    that numbers lines from 1 shows real file line numbers; the padding sits
    beyond the hunk's own context, so a context-collapsing viewer hides it.
    """

    path: str
    before: str
    after: str
    added: int
    removed: int


def _diff_path(old: str, new: str) -> str:
    if new != "/dev/null":
        return new.removeprefix("b/")
    return old.removeprefix("a/")


def split_diff_files(diff: Mapping[str, object]) -> list[tuple[str, str]]:
    """``(path, hunk)`` per file of a (possibly multi-file) ``diff`` artifact.

    Files are delimited by their ``---``/``+++`` headers; a hunk without any
    header belongs to the artifact's own ``path``.
    """
    hunk = diff.get("hunk")
    if not isinstance(hunk, str) or not hunk:
        return []
    default = diff.get("path")
    path = _text(default, 400) if isinstance(default, str) else "file"
    files: list[tuple[str, str]] = []
    body: list[str] = []
    lines = hunk.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.startswith("--- ") and following.startswith("+++ "):
            if body:
                files.append((path, "\n".join(body)))
            path, body = _text(_diff_path(line[4:], following[4:]), 400), []
            index += 2
            continue
        body.append(line)
        index += 1
    if body:
        files.append((path, "\n".join(body)))
    return files


DiffRow = tuple[int, str, int, str, str]


def diff_split_rows(hunk: str, limit: int = 400) -> list[DiffRow]:
    """Side-by-side rows ``(old_no, old_text, new_no, new_text, kind)`` for one file's hunks.

    ``kind`` is ``ctx``, ``del``, ``add``, ``change`` (a removal paired with an
    addition), ``sep`` (a gap between hunks) or ``clip`` (``old_text`` says how many
    rows were left out). Line numbers are real file lines; ``0`` means no line.
    """
    rows: list[DiffRow] = []
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
    old = new = 0

    def flush() -> None:
        for i in range(max(len(removed), len(added))):
            left = removed[i] if i < len(removed) else (0, "")
            right = added[i] if i < len(added) else (0, "")
            kind = "change" if left[0] and right[0] else "del" if left[0] else "add"
            rows.append((left[0], left[1], right[0], right[1], kind))
        removed.clear()
        added.clear()

    started = False
    for line in hunk.splitlines():
        header = _HUNK_START.match(line)
        if header:
            flush()
            if started:
                rows.append((0, "", 0, "", "sep"))
            started = True
            old, new = int(header.group(1)), int(header.group(2))
        elif not started or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            added.append((new, line[1:]))
            new += 1
        elif line.startswith("-"):
            removed.append((old, line[1:]))
            old += 1
        else:
            flush()
            rows.append((old, line[1:], new, line[1:], "ctx"))
            old += 1
            new += 1
    flush()
    if len(rows) > limit:
        rows = [*rows[:limit], (0, f"… {len(rows) - limit} more rows", 0, "", "clip")]
    return rows


UnifiedRow = tuple[str, int, str]


def diff_unified_rows(hunk: str, limit: int = 400) -> list[UnifiedRow]:
    """One-column rows ``(kind, line_no, text)`` for one file's hunks.

    ``kind`` is ``ctx``, ``del`` (``line_no`` is the old line), ``add`` (the new
    line), ``sep`` (a gap between hunks) or ``clip`` (``text`` says how many rows
    were left out). Removals keep their place before the additions that replace them.
    """
    rows: list[UnifiedRow] = []
    old = new = 0
    started = False
    for line in hunk.splitlines():
        header = _HUNK_START.match(line)
        if header:
            if started:
                rows.append(("sep", 0, ""))
            started = True
            old, new = int(header.group(1)), int(header.group(2))
        elif not started or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            rows.append(("add", new, line[1:]))
            new += 1
        elif line.startswith("-"):
            rows.append(("del", old, line[1:]))
            old += 1
        else:
            rows.append(("ctx", new, line[1:]))
            old += 1
            new += 1
    if len(rows) > limit:
        rows = [*rows[:limit], ("clip", 0, f"… {len(rows) - limit} more rows")]
    return rows


_REVIEW_ROW_LIMIT = 4000


def structured_diff(diff: Mapping[str, object]) -> dict:
    """Structured per-file unified diff for the desktop Review pane (plan D3).

    Returns ``{"files": [{"path", "added", "removed", "hunks": [{"header",
    "rows": [{"kind", "old_no", "new_no", "text"}]}]}], "truncated": bool}``.
    ``kind`` is ``ctx``, ``add`` or ``del``; ``0`` means no line number. The row
    budget is announced through ``truncated`` rather than silently clipped.
    """
    files: list[dict] = []
    budget = _REVIEW_ROW_LIMIT
    truncated = bool(diff.get("truncated"))
    for path, hunk in split_diff_files(diff):
        hunks: list[dict] = []
        added = removed = 0
        current: dict | None = None
        old = new = 0
        for line in hunk.splitlines():
            header = _HUNK_START.match(line)
            if header:
                if current is not None:
                    hunks.append(current)
                old, new = int(header.group(1)), int(header.group(2))
                current = {"header": line, "rows": []}
                continue
            if current is None or line.startswith("\\"):
                continue
            if budget <= 0:
                truncated = True
                break
            if line.startswith("+"):
                current["rows"].append(
                    {"kind": "add", "old_no": 0, "new_no": new, "text": line[1:]}
                )
                new += 1
                added += 1
            elif line.startswith("-"):
                current["rows"].append(
                    {"kind": "del", "old_no": old, "new_no": 0, "text": line[1:]}
                )
                old += 1
                removed += 1
            else:
                text = line[1:] if line.startswith(" ") else line
                current["rows"].append(
                    {"kind": "ctx", "old_no": old, "new_no": new, "text": text}
                )
                old += 1
                new += 1
            budget -= 1
        if current is not None:
            hunks.append(current)
        if hunks:
            files.append(
                {"path": path, "added": added, "removed": removed, "hunks": hunks}
            )
        if budget <= 0:
            break
    return {"files": files, "truncated": truncated}


def diff_sections(diff: Mapping[str, object]) -> list[DiffSection]:
    """Per-file before/after text for a (possibly multi-file) ``diff`` artifact."""
    sections: list[DiffSection] = []
    for path, hunk in split_diff_files(diff):
        before: list[str] = []
        after: list[str] = []
        added = removed = 0
        seen_hunk = False
        for line in hunk.splitlines():
            header = _HUNK_START.match(line)
            if header:
                seen_hunk = True
                gap = int(header.group(1)) - 1 - len(before)
                if 0 < gap and len(before) + gap <= _MAX_DIFF_PAD:
                    before.extend([""] * gap)
                    after.extend([""] * gap)
                continue
            if not seen_hunk or line.startswith("\\"):
                continue
            if line.startswith("+"):
                after.append(line[1:])
                added += 1
            elif line.startswith("-"):
                before.append(line[1:])
                removed += 1
            else:
                before.append(line[1:])
                after.append(line[1:])
        if before or after:
            sections.append(DiffSection(path, "\n".join(before), "\n".join(after), added, removed))
    return sections


def _latest_activity(agent: AgentView) -> str:
    for turn in reversed(agent.body.turns):
        if turn.tools:
            tool = max(turn.tools, key=lambda item: item.event_seq)
            return _text(f"{tool.name or 'tool'}: {tool_summary(tool)}", 140)
    for turn in reversed(agent.body.turns):
        for message in reversed(turn.messages):
            if message.text:
                return _text(message.text, 140)
    return "waiting for activity"


def _task_short_phrase(value: object) -> str:
    if value is None:
        return ""
    phrase = _text(value, 240)
    phrase = " ".join(phrase.split())
    phrase = re.split(r"(?<=[.!?])\s+", phrase, maxsplit=1)[0]
    return _text(phrase, 100)


def _task_result(tool: ToolCallView) -> str:
    result = tool.display or ""
    if not result:
        result = "\n".join(
            str(block.get("text") or block.get("content") or "")
            for block in tool.result[:4]
            if isinstance(block, Mapping)
        )
    if result:
        result = re.sub(r"^\s*task\s*:\s*", "", result, flags=re.IGNORECASE)
    return _task_short_phrase(result or tool.error or "")


def _task_phrase(tool: ToolCallView, agent: AgentView | None) -> str:
    description = tool.input.get("description") if isinstance(tool.input, Mapping) else None
    prompt = tool.input.get("prompt") if isinstance(tool.input, Mapping) else None
    for candidate in (
        description,
        agent.description if agent is not None else None,
        prompt,
        _task_result(tool),
        agent.error if agent is not None else None,
    ):
        phrase = _task_short_phrase(candidate)
        if phrase:
            return phrase
    return ""


def _task_header(tool: ToolCallView, agent: AgentView | None, spinner_index: int) -> tuple[str, bool]:
    marker = tool_status(tool)
    kind = (
        agent.type
        if agent and agent.type
        else tool.input.get("subagent_type")
        if isinstance(tool.input, Mapping)
        else None
    ) or "General"
    kind = _text(str(kind), 32).title()
    running = marker == "running" and (agent is None or agent.status == "spawned")
    phrase = _task_phrase(tool, agent) or "completed"
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    mark = spinner[spinner_index] if running else "·"
    return f"{mark} {kind} · {phrase}", running


def _task_child_activity(agent: AgentView) -> str:
    tools = sorted(
        (tool for turn in agent.body.turns for tool in turn.tools),
        key=lambda item: item.event_seq,
    )
    if not tools:
        return "Starting…"
    tool = tools[-1]
    heading = tool_heading(tool)
    progress = tool.progress[-1] if tool.progress else ""
    # Width-dependent clipping belongs to the renderer, not the activity model.
    return escape_controls(" · ".join(part for part in (heading, progress) if part)).replace("\n", " ").replace("\t", " ")


def _task_children(tool: ToolCallView, agents: Mapping[str, AgentView]) -> tuple[AgentView, ...]:
    return tuple(agents[agent_id] for agent_id in tool.child_agent_ids if agent_id in agents)


def _task_metrics(tool: ToolCallView, agent: AgentView | None, running: bool) -> str:
    if running:
        summary = _task_child_activity(agent) if agent is not None else "Starting…"
    elif agent is not None:
        summary = _agent_metrics(agent)
    else:
        elapsed = tool.duration_ms
        summary = f"0 tool calls · {elapsed / 1000:.1f}s" if elapsed is not None else "0 tool calls"
    return re.sub(
        r"\b(\d+) tools?\b(?! calls?\b)",
        lambda match: f"{match.group(1)} tool call" + ("s" if match.group(1) != "1" else ""),
        summary,
    )


def _task_child_details(agents: tuple[AgentView, ...]) -> list[str]:
    return [
        f"{_text(agent.type or agent.id, 48)} · {_text(agent.task or agent.description, 96)}"
        f" · {_text(agent.status, 24)} · {_agent_metrics(agent)}\n  {_latest_activity(agent)}"
        for agent in agents
    ]


def _agent_link_label(agent: AgentView) -> str:
    activity = _latest_activity(agent)
    tool = next(
        (tool for turn in reversed(agent.body.turns) for tool in reversed(turn.tools)),
        None,
    )
    if tool is not None:
        activity += f" · {tool_status(tool)}"
    return (
        f"{_text(agent.type or agent.id, 36)} · "
        f"{_text(agent.status, 18)} · {_agent_metrics(agent)} · {activity}"
    )


def _agent_metrics(agent: AgentView) -> str:
    """Summarize reducer-owned child calls and timestamps without a wall clock."""
    tools = [tool for turn in agent.body.turns for tool in turn.tools]
    completed = sum(tool.status in {"completed", "failed"} for tool in tools)
    label = f"{completed} tool{'s' if completed != 1 else ''}"
    if agent.status == "spawned":
        return label
    starts = [stamp for value in (agent.spawned_ts, *(turn.started_ts for turn in agent.body.turns))
              if (stamp := _timestamp(value)) is not None]
    if not starts:
        return label
    start = min(starts)
    ends = [stamp for value in (
        agent.completed_ts, *(turn.updated_ts for turn in agent.body.turns),
        *(stamp for tool in tools for stamp in (tool.requested_ts, tool.started_ts, tool.finished_ts)),
    ) if (stamp := _timestamp(value)) is not None]
    end = max(ends, default=start)
    elapsed = max(0, round((end - start) * 1000))
    duration = f"{elapsed / 1000:.1f}s" if elapsed < 60_000 else f"{elapsed // 60_000}m {(elapsed // 1000) % 60}s"
    return f"{label} · {duration}"


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _turn_duration(turn: TurnView) -> str | None:
    elapsed_ms = turn.elapsed_ms
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
        return None
    return f"{elapsed_ms / 1000:.1f}s" if elapsed_ms < 60_000 else f"{elapsed_ms // 60_000}m {(elapsed_ms // 1000) % 60}s"


def _turn_models(turn: TurnView) -> str:
    """Show actual per-message model metadata, never the current selection."""
    models = dict.fromkeys(
        f"{message.provider}/{message.model}" if message.provider and message.model
        else message.model or message.provider or ""
        for message in turn.messages
        if message.role == "assistant" and (message.provider or message.model)
    )
    return ", ".join(_literal(model, 120) for model in models) or "unknown"


def _turn_agent(turn: TurnView) -> str:
    agent = turn.agent
    name = agent.get("name") if isinstance(agent, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        return "No agent"
    name = name.strip()
    return _literal(name[0].upper() + name[1:], 80)


def _turn_effort(turn: TurnView) -> str:
    effort = turn.reasoning_effort
    return _literal(effort.strip(), 48) if isinstance(effort, str) and effort.strip() else "Default"


def _turn_summary(turn: TurnView) -> str:
    """Render frozen per-turn facts from the reducer projection."""
    parts = [f"Model {_turn_models(turn)}"]
    if duration := _turn_duration(turn):
        parts.append(duration)
    parts.extend((f"Agent {_turn_agent(turn)}", f"Effort {_turn_effort(turn)}"))
    return "  ·  ".join(parts)


def turn_footer_text(turn: TurnView) -> str:
    """Plain right-aligned stats for a completed turn (model, time, tokens, cache, reasoning)."""
    from .context import _compact_tokens
    model = _turn_models(turn).split(", ")[0].rsplit("/", 1)[-1]
    usage = turn.usage
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    # This is additive provider usage for the whole turn, not the latest
    # request size shown by the composer context meter.
    tokens = f"turn ↑{_compact_tokens(prompt)} ↓{_compact_tokens(usage.output_tokens)}" if prompt or usage.output_tokens else ""
    cached = f"{round(usage.cache_read_tokens / prompt * 100)}% cached" if prompt and usage.cache_read_tokens else ""
    # ``r`` is reasoning tokens; the count is additive provider usage for the
    # whole turn and does not distinguish shared from hidden reasoning.
    reasoning = f"{_compact_tokens(usage.reasoning_tokens)} r" if usage.reasoning_tokens else ""
    # A configured limit ended the turn, not the model: say so, or the turn
    # looks like it simply stopped after its last tool result.
    stopped = _LIMIT_STOPS.get(turn.stop_reason or "", "") if turn.phase == "completed" else ""
    return " · ".join(part for part in (stopped, model if model != "unknown" else "", _turn_duration(turn) or "", tokens, cached, reasoning) if part)


_LIMIT_STOPS = {
    "budget": "stopped by turn limit",
    "max_iterations": "stopped by iteration limit",
}


def turn_agent_label(turn: TurnView) -> tuple[str, str]:
    """``(name, color)`` of the agent that answered the turn; empty name when unknown."""
    agent = turn.agent if isinstance(turn.agent, Mapping) else {}
    name = agent.get("name") if isinstance(agent.get("name"), str) else ""
    if not name.strip():
        return "", ""
    color = agent.get("color") if isinstance(agent.get("color"), str) else ""
    name = name.strip()
    return _literal(name[0].upper() + name[1:], 60), color


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def tool_row_text(tool: ToolCallView, spinner_index: int = 0, gutter: str = "") -> str:
    """The compact activity row of a tool call: heading, summary, live output tail."""
    marker = tool_status(tool)
    indicator = f"{_SPINNER_FRAMES[spinner_index % len(_SPINNER_FRAMES)]} " if marker == "running" else ""
    summary = ""
    if marker == "completed" and tool.display and tool.name.casefold() not in {"read", "grep", "todowrite"}:
        summary = _text(tool.display.splitlines()[0], 88)
        if tool.name.casefold() == "write":
            content = tool.input.get("content") if isinstance(tool.input, Mapping) else None
            lines = content.count("\n") + 1 if isinstance(content, str) and content else 0
            summary = f"written · {lines} lines" if lines else "written"
    elif marker == "failed" and tool.error:
        summary = _text(tool.error.splitlines()[0], 88)
    if summary.casefold().startswith(f"{tool.name.casefold()}:"):
        summary = summary[len(tool.name) + 1 :].strip()
    summary = summary
    suffix = f" · {summary}" if summary else (f" · {marker}" if marker not in {"completed", "failed"} else "")
    rows = todo_preview(tool)
    heading = "☐ Todo " + rows[0] if rows else tool_heading(tool)
    text = f"{gutter}{indicator}{heading}{suffix}"
    if rows:
        text += "".join(f"\n{gutter}  {row}" for row in rows[1:])
    live = running_output_tail(tool) if marker == "running" else None
    if live is not None:
        tail, hidden = live
        text += f"\n{gutter}  ⎿  " + (tail[0] if tail else "running…")
        text += "".join(f"\n{gutter}     {line}" for line in tail[1:])
        if hidden:
            text += f"\n{gutter}     … {hidden} earlier line{'s' if hidden != 1 else ''} · enter for full output"
    return text


__all__ = [
    "running_output_tail",
    "DIFF_TOOLS",
    "_DETAIL_LIMIT",
    "DiffSection",
    "_agent_link_label",
    "_agent_metrics",
    "_has_message_content",
    "_latest_activity",
    "_literal",
    "_message_markdown",
    "_output",
    "_setup_failure",
    "_stale_greeting",
    "_task_child_details",
    "_task_children",
    "_task_header",
    "_task_metrics",
    "_task_phrase",
    "_task_result",
    "_task_short_phrase",
    "_text",
    "_turn_setup_failure",
    "_turn_summary",
    "diff_sections",
    "diff_split_rows",
    "format_arguments",
    "structured_diff",
    "split_diff_files",
    "thought_title",
    "tool_heading",
    "tool_output",
    "tool_status",
    "tool_row_text",
    "tool_summary",
    "turn_agent_label",
    "turn_footer_text",
]


def _byte_size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


LIVE_TAIL_LINES = 4
_LIVE_OUTPUT_TOOLS = frozenset({"bash", "bashoutput", "shell"})


def running_output_tail(tool: ToolCallView, lines: int = LIVE_TAIL_LINES) -> tuple[list[str], int] | None:
    """A running shell's latest output lines and how many came before them.

    ``None`` for tools that are not long-running shells; clipping is counted so
    the row can announce it.
    """
    if tool.name.casefold() not in _LIVE_OUTPUT_TOOLS:
        return None
    output = "".join(tool.progress[-200:])
    rows = [_text(line, 160) for line in output.splitlines() if line.strip()]
    return rows[-lines:], max(0, len(rows) - lines)


def submitted_attachment_summary(message: MessageView) -> tuple[str, list[str]]:
    """Separate numbered attachment payloads from the literal prompt (PLAN §14.4).

    Labels read ``image 1 · photo.png`` (the image itself is the content, so its
    byte size is left out) and ``document 1 · report.pdf · 12.4 KB`` (the size
    of the converted text the model receives).
    """
    prompt, labels = [], []
    for block in message.blocks:
        if block.kind != "text":
            continue
        match = re.match(r"^\n\nAttachment: ((?:image|document) [1-9][0-9]*) · ([^\n]+)\n", block.text)
        if match:
            name = match[2]
            if match[1].startswith("image"):
                name = re.sub(r" · \d+ bytes$", "", name)
                name = re.sub(r" · image/[\w.+-]+$", "", name)
                labels.append(f"{match[1]} · {name}")
            else:
                body = block.text[match.end():].lstrip("\n")
                labels.append(f"{match[1]} · {name} · {_byte_size(len(body.encode('utf-8')))}")
        else:
            prompt.append(block.text)
    return "".join(prompt), labels