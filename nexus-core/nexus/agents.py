"""Agents: Markdown files whose frontmatter configures a session and whose body
is the system prompt (after OpenCode's agent files).

```markdown
---
description: Reviews code without changing it   # required: what it is for
mode: subagent                                   # primary | subagent | all (default all)
color: "#7aa2f7"                                 # optional: a colour name or #rrggbb for clients
model: copilot/claude-sonnet-5                   # preferred model, if the session sets none
effort: high                                     # preferred reasoning effort
tools: [read, bash]                              # allowed task tools; omit for all task tools
control: [session]                               # control tools; none unless listed
steps: 20                                        # max loop steps per turn (default 50)
permission: {"bash(git push*)": deny, web_fetch: ask}   # narrows the user's rules; ask or deny only
---
You review code. Point out bugs and risks; do not edit files.
```

`mode` says where an agent may run: `primary` only as a root session (started
by a user), `subagent` only as a sub agent, `all` in both. The daemon refuses
a session whose agent's mode does not fit.

The file name is the agent's name (`review.md` → `review`). Agents load from
three places, and a later one replaces an earlier one with the same name:

1. built-in: `nexus/builtin_agents/*.md` (shipped with core)
2. global: `<home>/agents/*.md` (`~/.nexus10/agents`)
3. project: `<project>/.agents/agents/*.md`

A file that fails to parse is reported in `problems`; it is never silently
skipped.

The system prompt is built by `render_prompt()`: the body plus an environment
block. It does not list tools: each tool is described once, by its definition
in the request. A session with no tools gets one line saying so. A leftover
`{{tools}}` placeholder in a body is removed. Agent bodies should not name
tools, so turning a tool off removes every description of it.
The whole prompt is visible in a session's context (`GET …/context`).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .paths import home

BUILTIN_DIR = Path(__file__).parent / "builtin_agents"
DEFAULT_AGENT = "build"
MAX_FILE_BYTES = 256 * 1024
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_FIELDS = {"description", "mode", "color", "model", "effort", "tools", "control", "steps", "permission"}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}|[a-z][a-z-]{0,31}\Z")
MODES = ("primary", "subagent", "all")
CONTROL_TOOLS = ("session",)  # the control tools an agent file may grant
# Every control tool, whether granted by an agent or always available: none of these
# is ever bound inside a `code` cell.
MESSAGE_TOOL = "message_session"  # exposed to a session by its first `session` call, never granted by an agent file
ESCALATE_TOOL = "escalate"  # every sub agent has it; a root session never does; never granted by an agent file
NEXUS_CONTROL_TOOLS = ("session", MESSAGE_TOOL, ESCALATE_TOOL, "expose_workspace_tools", "change_session_directory", "worktree")


@dataclass(frozen=True, slots=True)
class AgentDef:
    name: str
    description: str
    prompt: str  # the Markdown body
    source: str  # "builtin" | "global" | "project"
    path: str
    mode: str = "all"  # MODES
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] | None = None  # None = every task tool
    control: tuple[str, ...] = ()  # control tools (CONTROL_TOOLS); never implied
    steps: int | None = None
    permission: tuple[tuple[str, str], ...] = ()  # (rule, "ask" | "deny") in file order
    color: str | None = None  # a theme colour name or #rrggbb, for clients


@dataclass
class AgentSet:
    agents: dict[str, AgentDef] = field(default_factory=dict)
    problems: list[dict[str, str]] = field(default_factory=list)  # {"path", "error"}

    def get(self, name: str, *, child: bool | None = None) -> AgentDef:
        """The agent `name`; with `child` set, it must be allowed as a child (True) or a root (False) session."""
        if name not in self.agents:
            raise ValueError(f"unknown agent {name!r}; available: {', '.join(sorted(self.agents))}")
        agent = self.agents[name]
        if child is not None and not fits(agent, child=child):
            where = "a sub agent" if child else "a root session"
            allowed = ", ".join(a.name for a in self.agents.values() if fits(a, child=child))
            raise ValueError(f"agent {name!r} (mode {agent.mode}) cannot run as {where}; allowed: {allowed}")
        return agent

    def allowed(self, *, child: bool) -> list[AgentDef]:
        return [a for a in self.agents.values() if fits(a, child=child)]


def fits(agent: AgentDef, *, child: bool) -> bool:
    return agent.mode == "all" or agent.mode == ("subagent" if child else "primary")


def parse(path: Path, source: str) -> AgentDef:
    name = path.stem
    if not _NAME.fullmatch(name):
        raise ValueError("file name must be lowercase letters, digits, '-' or '_'")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    text = raw.decode("utf-8")
    match = re.match(r"---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        raise ValueError("missing frontmatter (--- ... ---)")
    meta: Any = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")  # noqa: TRY004 - a content error, reported like the others
    unknown = set(meta) - _FIELDS
    if unknown:
        raise ValueError(f"unknown frontmatter keys: {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(_FIELDS))}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    tools = meta.get("tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)):
        raise ValueError("tools must be a list of tool names")
    control = meta.get("control") or []
    if not isinstance(control, list) or not all(isinstance(t, str) for t in control):
        raise ValueError("control must be a list of control tool names")
    unknown_control = [t for t in control if t not in CONTROL_TOOLS]
    if unknown_control:
        raise ValueError(f"unknown control tools: {', '.join(unknown_control)}; available: {', '.join(CONTROL_TOOLS)}")
    mode = meta.get("mode", "all")
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    steps = meta.get("steps")
    if steps is not None and (not isinstance(steps, int) or not 1 <= steps <= 500):
        raise ValueError("steps must be an integer from 1 to 500")
    color = meta.get("color")
    if color is not None and not (isinstance(color, str) and _COLOR.fullmatch(color)):
        raise ValueError("color must be a colour name or #rrggbb")
    for key in ("model", "effort"):
        if meta.get(key) is not None and not isinstance(meta[key], str):
            raise ValueError(f"{key} must be a string")
    rules: list[Any] = []
    if meta.get("permission") is not None:
        from .permissions import parse_rules
        rules, problems = parse_rules(meta["permission"], agent=True)
        if problems:
            raise ValueError("permission: " + "; ".join(problems))
    prompt = match.group(2).strip()
    if not prompt:
        raise ValueError("the body (system prompt) is empty")
    return AgentDef(
        name=name, description=description.strip(), prompt=prompt, source=source, path=str(path), mode=mode,
        model=meta.get("model"), effort=meta.get("effort"),
        tools=tuple(tools) if tools is not None else None, control=tuple(control), steps=steps,
        permission=tuple((rule.text, rule.action) for rule in rules), color=color,
    )


def directories(project_path: str | None) -> list[tuple[str, Path]]:
    dirs = [("builtin", BUILTIN_DIR), ("global", home() / "agents")]
    if project_path:
        dirs.append(("project", Path(project_path) / ".agents" / "agents"))
    return dirs


def load(project_path: str | None = None) -> AgentSet:
    result = AgentSet()
    for source, directory in directories(project_path):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                agent = parse(path, source)
            except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
                result.problems.append({"path": str(path), "error": str(exc).splitlines()[0]})
                continue
            result.agents[agent.name] = agent
    return result


def fingerprint(project_paths: list[str]) -> str:
    """Changes when an agent file is added, removed or edited in the global folder or any project's.
    Built-in agents ship with the code and are left out."""
    stamps: list[tuple[str, int, int]] = []
    for source, directory in directories(None):
        if source == "global":
            stamps += _stamps(directory)
    for path in sorted(set(project_paths)):
        stamps += _stamps(Path(path) / ".agents" / "agents")
    return hashlib.sha256(repr(stamps).encode()).hexdigest()[:16]


def _stamps(directory: Path) -> list[tuple[str, int, int]]:
    out = []
    try:
        for path in sorted(directory.glob("*.md")):
            stat = path.stat()
            out.append((str(path), stat.st_mtime_ns, stat.st_size))
    except OSError:
        pass
    return out


TOOLS_PLACEHOLDER = "{{tools}}"


NO_TOOLS = "No tools are available in this session. Answer from the conversation alone."


def render_prompt(agent: AgentDef, tools: list[str], workspace: Path) -> str:
    body = agent.prompt
    if TOOLS_PLACEHOLDER in body:
        body = re.sub(r"\n{3,}", "\n\n", body.replace(TOOLS_PLACEHOLDER, "")).strip()
    if not tools:
        body = f"{body}\n\n{NO_TOOLS}"
    return (
        f"{body}\n\n# Environment\n"
        f"Workspace: {workspace}\n"
        f"Today: {datetime.now().astimezone().date().isoformat()}"
    )
