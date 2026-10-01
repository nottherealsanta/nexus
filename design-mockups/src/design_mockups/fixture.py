"""One fake Nexus session that every design and every element variant renders.

Everything is static and deterministic so screenshots are comparable: the only
differences between two mock-ups are design choices, never data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Session:
    title: str
    when: str
    group: str
    agent: str = "Build"
    status: str = "idle"  # idle | running | approval | error
    turns: int = 0
    cost: str = ""
    archived: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    group: str
    signature: str
    summary: str
    tokens: int
    policy: str = "allow"  # allow | ask | deny


@dataclass(frozen=True)
class Skill:
    name: str
    scope: str
    summary: str
    enabled: bool = True


@dataclass(frozen=True)
class McpServer:
    name: str
    tools: int
    status: str  # ready | starting | failed | off
    transport: str = "stdio"


@dataclass(frozen=True)
class DiffLine:
    kind: str  # " " context, "+" added, "-" removed, "@" hunk header
    old: int | None
    new: int | None
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    tool: str
    target: str
    params: tuple[tuple[str, str], ...]
    status: str  # ok | error | running | denied
    summary: str
    output: tuple[str, ...] = ()
    duration: str = ""
    tokens: int = 0
    diff: tuple[DiffLine, ...] = ()
    added: int = 0
    removed: int = 0
    exit_code: int | None = None


@dataclass(frozen=True)
class Thought:
    seconds: int
    text: str


@dataclass(frozen=True)
class SubAgent:
    id: str
    agent: str
    task: str
    status: str  # done | running | failed
    model: str
    duration: str
    tokens: int
    calls: tuple[ToolCall, ...] = ()
    result: str = ""


@dataclass(frozen=True)
class ErrorItem:
    kind: str  # tool | provider
    title: str
    detail: str
    retry_in: int | None = None
    attempt: str = ""


@dataclass(frozen=True)
class UserMessage:
    text: str
    attachments: tuple[str, ...] = ()
    tokens: int = 0


@dataclass(frozen=True)
class Footer:
    agent: str
    model: str
    duration: str
    tokens_in: int
    tokens_out: int
    cost: str
    cache_hit: int = 0


@dataclass(frozen=True)
class Turn:
    number: int
    user: UserMessage
    items: tuple[object, ...]  # Thought | ToolCall | SubAgent | ErrorItem | str (assistant markdown)
    footer: Footer | None = None
    live: bool = False


@dataclass(frozen=True)
class Permission:
    tool: str
    command: str
    cwd: str
    reason: str
    agent: str
    risk: str
    choices: tuple[tuple[str, str], ...] = (("y", "Allow once"), ("a", "Always allow `pytest`"), ("n", "Deny"))


@dataclass(frozen=True)
class Question:
    agent: str
    prompt: str
    options: tuple[tuple[str, str], ...]
    selected: int = 0


@dataclass(frozen=True)
class ModelRow:
    provider: str
    name: str
    context: str
    price: str
    tags: tuple[str, ...] = ()
    favorite: bool = False
    current: bool = False


@dataclass(frozen=True)
class PaletteRow:
    group: str
    label: str
    keys: str = ""
    hint: str = ""


@dataclass(frozen=True)
class ContextSlice:
    label: str
    tokens: int
    role: str  # palette color role


@dataclass(frozen=True)
class Recording:
    elapsed: str = "0:07"
    max: str = "2:00"
    fraction: float = 0.06
    levels: tuple[int, ...] = (1, 2, 4, 6, 7, 5, 3, 2, 4, 7, 8, 6, 4, 3, 5, 6, 4, 2, 1, 2, 3, 5, 7, 6, 4, 2, 1, 1)
    partial: str = "run the refresh tests again but only the"
    device: str = "MacBook Pro Microphone"
    model: str = "parakeet · local · 178 MB"


@dataclass(frozen=True)
class Fixture:
    workspace: str
    branch: str
    session: Session
    model: str
    provider: str
    effort: str
    sessions: tuple[Session, ...]
    system_prompt: str
    system_tokens: int
    agents_md_tokens: int
    tools: tuple[Tool, ...]
    skills: tuple[Skill, ...]
    mcp: tuple[McpServer, ...]
    context: tuple[ContextSlice, ...]
    budget: int
    turns: tuple[Turn, ...]
    live_turn: Turn
    permission: Permission
    question: Question
    models: tuple[ModelRow, ...]
    palette: tuple[PaletteRow, ...]
    modified: tuple[tuple[str, str, int, int], ...]
    jobs: tuple[tuple[str, str, str], ...]
    worktrees: tuple[tuple[str, str, str], ...]
    logs: tuple[tuple[str, str, str, str], ...]
    recording: Recording = field(default_factory=Recording)

    @property
    def used(self) -> int:
        return sum(s.tokens for s in self.context)

    def calls(self) -> list[ToolCall]:
        out: list[ToolCall] = []
        for turn in (*self.turns, self.live_turn):
            for item in turn.items:
                if isinstance(item, ToolCall):
                    out.append(item)
                elif isinstance(item, SubAgent):
                    out.extend(item.calls)
        return out

    def call(self, call_id: str) -> ToolCall:
        return next(c for c in self.calls() if c.id == call_id)

    def subagents(self) -> list[SubAgent]:
        return [i for t in self.turns for i in t.items if isinstance(i, SubAgent)]

    def errors(self) -> list[ErrorItem]:
        return [i for t in (*self.turns, self.live_turn) for i in t.items if isinstance(i, ErrorItem)]


SYSTEM_PROMPT = """\
You are Build, the default coding agent in Nexus. You work inside the user's
workspace and complete software engineering tasks end to end.

# How you work
- Read the relevant code before you change it. Prefer small, verified edits.
- Run the project's tests after a change and report failures faithfully.
- Never claim something works unless you ran it.
- Ask the user only when a decision is genuinely theirs to make.

# Tools
Use the dedicated file tools (Read, Edit, Glob, Grep) rather than shell
equivalents. Shell commands run in the workspace with a 120 s default timeout.
Long-running commands may be started as background jobs and polled.

# Output
Be concise. Use Markdown. Reference code as `path:line`.

<environment>
workspace: /Users/santa/repos/auth-service
platform: darwin 27.0.0 (arm64)
shell: zsh
git: branch fix/token-refresh, 3 modified files
date: 2026-10-01
</environment>

<project_instructions source="AGENTS.md">
Use `uv run pytest -q` for tests. Async code uses anyio, never raw asyncio.
Token refresh lives in src/auth/refresh.py; keep it free of HTTP imports.
</project_instructions>

<skills>
release-notes: draft release notes from merged PRs
db-migrate: write and check an Alembic migration
pr-review: review the current diff for correctness bugs
</skills>
"""

_TOOLS = (
    Tool("Read", "files", "Read(path, offset?, limit?)", "Read a file with line numbers", 412),
    Tool("Edit", "files", "Edit(path, old, new, all?)", "Replace an exact string in a file", 655, "ask"),
    Tool("Write", "files", "Write(path, content)", "Create or overwrite a file", 380, "ask"),
    Tool("Glob", "files", "Glob(pattern, path?)", "Find files by glob pattern", 290),
    Tool("Grep", "search", "Grep(pattern, path?, glob?, context?)", "Search file contents with ripgrep", 740),
    Tool("Bash", "shell", "Bash(command, timeout?, background?)", "Run a shell command in the workspace", 1210, "ask"),
    Tool("JobOutput", "shell", "JobOutput(job, wait?)", "Read a background job's output", 260),
    Tool("JobStop", "shell", "JobStop(job)", "Stop a background job", 140),
    Tool("WebFetch", "web", "WebFetch(url, prompt)", "Fetch a page and extract an answer", 520, "ask"),
    Tool("WebSearch", "web", "WebSearch(query, limit?)", "Search the web", 410),
    Tool("Task", "agents", "Task(agent, prompt, model?)", "Run a subagent on a focused task", 980),
    Tool("AskUser", "agents", "AskUser(question, options)", "Ask the user a multiple-choice question", 330),
    Tool("TodoWrite", "planning", "TodoWrite(items)", "Track a short task list", 450),
    Tool("Skill", "planning", "Skill(name, args?)", "Load a skill's instructions", 210),
)

_EDIT_DIFF = (
    DiffLine("@", None, None, "@@ -41,9 +41,14 @@ async def refresh(token: Token) -> Token:"),
    DiffLine(" ", 41, 41, "    if not token.refresh_token:"),
    DiffLine(" ", 42, 42, "        raise RefreshError(\"no refresh token\")"),
    DiffLine("-", 43, None, "    if token.expires_at < now():"),
    DiffLine("-", 44, None, "        return await _exchange(token)"),
    DiffLine("+", None, 43, "    skew = timedelta(seconds=settings.refresh_skew)"),
    DiffLine("+", None, 44, "    if token.expires_at - skew <= now():"),
    DiffLine("+", None, 45, "        async with _refresh_lock(token.subject):"),
    DiffLine("+", None, 46, "            current = await store.get(token.subject)"),
    DiffLine("+", None, 47, "            if current.expires_at - skew > now():"),
    DiffLine("+", None, 48, "                return current"),
    DiffLine("+", None, 49, "            return await _exchange(current)"),
    DiffLine(" ", 45, 50, "    return token"),
    DiffLine("@", None, None, "@@ -88,3 +93,7 @@ def _exchange(token: Token) -> Token:"),
    DiffLine(" ", 88, 93, "    resp = await client.post(TOKEN_URL, data=payload)"),
    DiffLine("+", None, 94, "    if resp.status_code == 429:"),
    DiffLine("+", None, 95, "        raise RefreshThrottled(retry_after(resp))"),
    DiffLine(" ", 89, 96, "    resp.raise_for_status()"),
)

_PYTEST_FAIL = (
    "============================= test session starts ==============================",
    "collected 48 items",
    "",
    "tests/test_refresh.py ......F..                                          [ 18%]",
    "tests/test_store.py ...........                                          [ 41%]",
    "tests/test_tokens.py ............................                        [100%]",
    "",
    "=================================== FAILURES ===================================",
    "_________________________ test_concurrent_refresh_once _________________________",
    "",
    "    async def test_concurrent_refresh_once(store, fake_idp):",
    "        token = await store.seed(expires_in=1)",
    "        await anyio.gather(*(refresh(token) for _ in range(5)))",
    ">       assert fake_idp.exchanges == 1",
    "E       assert 5 == 1",
    "E        +  where 5 = <FakeIdP exchanges=5>.exchanges",
    "",
    "tests/test_refresh.py:77: AssertionError",
    "=========================== short test summary info ============================",
    "FAILED tests/test_refresh.py::test_concurrent_refresh_once - assert 5 == 1",
    "========================= 1 failed, 47 passed in 2.31s =========================",
)

_GREP_OUT = tuple(
    f"src/auth/{f}:{n}: {t}" for f, n, t in (
        ("refresh.py", 12, "from .store import TokenStore"),
        ("refresh.py", 41, "async def refresh(token: Token) -> Token:"),
        ("refresh.py", 88, "def _exchange(token: Token) -> Token:"),
        ("middleware.py", 23, "        token = await refresh(token)"),
        ("middleware.py", 57, "    # refresh happens before every proxied call"),
        ("client.py", 140, "            self._token = await refresh(self._token)"),
        ("jobs.py", 9, "from .refresh import refresh"),
        ("jobs.py", 31, "    await refresh(token)  # nightly pre-warm"),
    )
) + tuple(f"tests/test_refresh.py:{n}: refresh(" for n in range(12, 98, 3))

_SUB_CALLS = (
    ToolCall("s1", "Grep", "\"_refresh_lock\"", (("pattern", "_refresh_lock"), ("path", "src/")), "ok", "0 matches", (), "0.1s", 40),
    ToolCall("s2", "Read", "src/auth/store.py", (("path", "src/auth/store.py"),), "ok", "164 lines", (), "0.1s", 2210),
    ToolCall("s3", "Grep", "\"anyio.Lock\"", (("pattern", "anyio.Lock"), ("glob", "**/*.py")), "ok", "3 matches in 2 files", (), "0.2s", 120),
)

_TURN1 = Turn(
    1,
    UserMessage(
        "Token refresh sometimes runs five times in parallel when a token expires under load.\n"
        "Find why and make it refresh once. Screenshot of the IdP rate-limit graph attached.",
        ("idp-rate-limits.png · 412 KB",),
        tokens=1840,
    ),
    (
        Thought(4, "The symptom is a thundering herd on expiry. I should look at refresh() and every caller, "
                   "then check whether there is any lock around the exchange. Middleware and the client both call it."),
        ToolCall("c1", "Read", "src/auth/refresh.py", (("path", "src/auth/refresh.py"),), "ok", "212 lines",
                 ("  1  \"\"\"Access-token refresh.\"\"\"", "  2  ", "  3  from __future__ import annotations", "  …"),
                 "0.1s", 2840),
        ToolCall("c2", "Grep", "\"refresh(\" in src/ tests/", (("pattern", "refresh("), ("path", "src/ tests/"), ("context", "0")),
                 "ok", "37 matches in 5 files", _GREP_OUT, "0.2s", 1310),
        ToolCall("c3", "Read", "src/auth/middleware.py", (("path", "src/auth/middleware.py"), ("offset", "1"), ("limit", "80")),
                 "ok", "80 of 133 lines", (), "0.1s", 1020),
        "## Why it refreshes five times\n\n"
        "`refresh()` checks `expires_at` and calls `_exchange()` with **no lock**, so every request that "
        "sees the expired token starts its own exchange:\n\n"
        "1. `middleware.py:23` refreshes before each proxied call\n"
        "2. `client.py:140` refreshes on its own copy of the token\n"
        "3. the nightly job in `jobs.py:31` can overlap both\n\n"
        "The fix is a per-subject lock plus a re-check after acquiring it:\n\n"
        "```python\nasync with _refresh_lock(token.subject):\n    current = await store.get(token.subject)\n"
        "    if current.expires_at - skew > now():\n        return current\n    return await _exchange(current)\n```\n\n"
        "I'll also add a small clock skew so tokens refresh just *before* they expire.",
    ),
    Footer("Build", "claude-opus-5-5", "18.4s", 14210, 912, "$0.084", 72),
)

_TURN2 = Turn(
    2,
    UserMessage("Do it, and add a test that fires five concurrent refreshes.", (), 22),
    (
        ToolCall("c4", "Edit", "src/auth/refresh.py", (("path", "src/auth/refresh.py"), ("old", "if token.expires_at < now(): …"), ("new", "skew = timedelta(…) …")),
                 "ok", "2 hunks · +9 −2", (), "0.0s", 610, _EDIT_DIFF, 9, 2),
        ToolCall("c5", "Write", "tests/test_refresh.py", (("path", "tests/test_refresh.py"), ("content", "61 lines")), "ok", "created · 61 lines", (), "0.0s", 980, (), 61, 0),
        ToolCall("c6", "Bash", "uv run pytest -q", (("command", "uv run pytest -q"), ("timeout", "120s"), ("cwd", "~/repos/auth-service")),
                 "error", "exit 1 · 1 failed, 47 passed", _PYTEST_FAIL, "2.9s", 1460, exit_code=1),
        ErrorItem("tool", "Write denied: path outside workspace", "Write(/etc/hosts) is outside /Users/santa/repos/auth-service. "
                  "Policy: tools.paths.allow = [\"$WORKSPACE\"]."),
        Thought(6, "The test still sees five exchanges. _refresh_lock creates a new Lock per call, so nothing is shared; "
                   "it has to come from a module-level WeakValueDictionary keyed by subject."),
        ToolCall("c7", "Edit", "src/auth/refresh.py", (("path", "src/auth/refresh.py"), ("old", "def _refresh_lock(…)"), ("new", "_LOCKS: WeakValueDictionary …")),
                 "ok", "1 hunk · +6 −2", (), "0.0s", 330, (), 6, 2),
        ToolCall("c8", "Bash", "uv run pytest -q", (("command", "uv run pytest -q"), ("timeout", "120s")),
                 "ok", "exit 0 · 48 passed", ("48 passed in 2.12s",), "2.6s", 90, exit_code=0),
        "Fixed. One exchange per subject now, and `test_concurrent_refresh_once` covers it.\n\n"
        "| File | Change |\n| --- | --- |\n| `src/auth/refresh.py` | per-subject lock, re-check, `refresh_skew` |\n"
        "| `tests/test_refresh.py` | new, 5 tests |\n\nAll **48 tests pass**.",
    ),
    Footer("Build", "claude-opus-5-5", "41.7s", 22980, 1630, "$0.151", 81),
)

_TURN3 = Turn(
    3,
    UserMessage("Check whether anything else in the repo has the same race, in parallel.", (), 18),
    (
        SubAgent("a1", "explore", "Find other unlocked check-then-act patterns in src/", "done", "claude-haiku-4-5", "9.8s", 6120,
                 _SUB_CALLS, "One more: `jobs.py:31` warms tokens without the store lock. Low risk (runs nightly)."),
        SubAgent("a2", "explore", "Audit tests for missing concurrency coverage", "running", "claude-haiku-4-5", "12.1s", 4410,
                 _SUB_CALLS[:2]),
        SubAgent("a3", "review", "Review the refresh diff for correctness", "failed", "claude-sonnet-5-5", "3.0s", 880, (),
                 "Provider error: 529 overloaded"),
    ),
    None,
)

_LIVE = Turn(
    4,
    UserMessage("Fix the jobs.py one too and summarise everything for the PR.", (), 19),
    (
        ErrorItem("provider", "Rate limited by anthropic (429)", "Too many requests on claude-opus-5-5. Retrying with backoff.", 8, "attempt 2 of 5"),
        ToolCall("c9", "Edit", "src/auth/jobs.py", (("path", "src/auth/jobs.py"), ("old", "await refresh(token)"), ("new", "await refresh_locked(token)")),
                 "ok", "1 hunk · +1 −1", (), "0.0s", 120, (), 1, 1),
        ToolCall("c10", "Bash", "uv run pytest -q tests/test_jobs.py", (("command", "uv run pytest -q tests/test_jobs.py"),), "running", "running · 4s", ("....",), "4s", 0),
        "## PR summary\n\nToken refresh is now single-flight per subject. Concurrent callers wait on a shared lock and",
    ),
    None,
    live=True,
)

FIXTURE = Fixture(
    workspace="~/repos/auth-service",
    branch="fix/token-refresh",
    session=Session("Fix concurrent token refresh", "now", "Today", "Build", "running", 4, "$0.31"),
    model="claude-opus-5-5",
    provider="anthropic",
    effort="high",
    sessions=(
        Session("Fix concurrent token refresh", "now", "Today", "Build", "running", 4, "$0.31"),
        Session("Add OpenTelemetry spans to proxy", "14:02", "Today", "Build", "approval", 7, "$0.42"),
        Session("Why is CI slow on macOS?", "11:40", "Today", "Ask", "idle", 3, "$0.05"),
        Session("Migrate settings to pydantic v2", "09:15", "Today", "Build", "error", 12, "$1.12"),
        Session("Draft 0.4 release notes", "18:20", "Yesterday", "Docs", "idle", 2, "$0.03"),
        Session("Rate limiter design review", "16:05", "Yesterday", "Plan", "idle", 5, "$0.22"),
        Session("Flaky test_store_ttl", "10:48", "Yesterday", "Build", "idle", 9, "$0.37"),
        Session("Explain the session reducer", "Mon", "Last week", "Ask", "idle", 4, "$0.06"),
        Session("Dockerfile multi-stage build", "Mon", "Last week", "Build", "idle", 6, "$0.19"),
        Session("Bump httpx to 0.28", "Sun", "Last week", "Build", "idle", 3, "$0.04"),
        Session("Old spike: websockets", "Sep 12", "Archived", "Build", "idle", 21, "$2.40", True),
        Session("Scratch", "Sep 03", "Archived", "Ask", "idle", 1, "$0.01", True),
    ),
    system_prompt=SYSTEM_PROMPT,
    system_tokens=3240,
    agents_md_tokens=410,
    tools=_TOOLS,
    skills=(
        Skill("release-notes", "project", "Draft release notes from merged PRs"),
        Skill("db-migrate", "project", "Write and check an Alembic migration"),
        Skill("pr-review", "global", "Review the current diff for correctness bugs"),
        Skill("dataviz", "global", "Chart and dashboard guidance", enabled=False),
    ),
    mcp=(
        McpServer("github", 26, "ready", "http"),
        McpServer("postgres", 4, "ready"),
        McpServer("sentry", 0, "failed", "http"),
    ),
    context=(
        ContextSlice("System prompt", 3240, "blue"),
        ContextSlice("AGENTS.md", 410, "cyan"),
        ContextSlice("Tools", 7900, "purple"),
        ContextSlice("Skills", 380, "yellow"),
        ContextSlice("MCP tools", 9800, "green"),
        ContextSlice("History", 96200, "accent"),
        ContextSlice("Attachments", 4300, "red"),
    ),
    budget=200_000,
    turns=(_TURN1, _TURN2, _TURN3),
    live_turn=_LIVE,
    permission=Permission(
        "Bash", "uv run pytest -q tests/test_jobs.py --runslow", "~/repos/auth-service",
        "Run the slow job tests after editing jobs.py", "Build", "Runs project code · no network",
    ),
    question=Question(
        "Build",
        "jobs.py pre-warms tokens nightly. How should it behave under the new lock?",
        (
            ("Use the lock", "Same single-flight path as requests (safest)"),
            ("Skip if locked", "Don't wait; a request is already refreshing"),
            ("Remove pre-warm", "Delete the nightly job; refresh on demand only"),
        ),
        0,
    ),
    models=(
        ModelRow("anthropic", "claude-opus-5-5", "1M", "$5 / $25", ("reasoning", "vision"), True, True),
        ModelRow("anthropic", "claude-sonnet-5-5", "1M", "$3 / $15", ("reasoning", "vision"), True),
        ModelRow("anthropic", "claude-haiku-4-5", "200k", "$1 / $5", ("fast",)),
        ModelRow("openai", "gpt-5.2", "400k", "$1.25 / $10", ("reasoning",)),
        ModelRow("openai", "gpt-5.2-mini", "400k", "$0.25 / $2", ("fast",)),
        ModelRow("google", "gemini-3-pro", "1M", "$2 / $12", ("reasoning", "vision")),
        ModelRow("ollama", "qwen3-coder:30b", "256k", "local", ("local",)),
    ),
    palette=(
        PaletteRow("Session", "New session", "ctrl+n"),
        PaletteRow("Session", "Switch session…", "ctrl+s"),
        PaletteRow("Session", "Rename session", "", "/rename"),
        PaletteRow("Session", "Export as JSONL", "", "/export"),
        PaletteRow("Model", "Choose model…", "ctrl+m", "/model"),
        PaletteRow("Model", "Reasoning effort…", "", "/effort"),
        PaletteRow("Agent", "Switch agent…", "tab", "/agent"),
        PaletteRow("View", "Toggle sessions sidebar", "ctrl+b"),
        PaletteRow("View", "Toggle details sidebar", "ctrl+d"),
        PaletteRow("View", "Context details", "ctrl+i"),
        PaletteRow("Voice", "Start dictation", "ctrl+x v"),
        PaletteRow("App", "Settings", "ctrl+,"),
    ),
    modified=(("M", "src/auth/refresh.py", 15, 4), ("A", "tests/test_refresh.py", 61, 0), ("M", "src/auth/jobs.py", 1, 1)),
    jobs=(("j1", "uv run pytest -q tests/test_jobs.py", "running · 4s"), ("j0", "uv run ruff check src", "exit 0 · 0.4s")),
    worktrees=(("main", "~/repos/auth-service", "current"), ("explore-a2", "~/.nexus/worktrees/a2", "subagent")),
    logs=(
        ("12:41:02", "INFO", "daemon", "session 01J9… resumed (4 turns, 182 records)"),
        ("12:41:02", "INFO", "mcp", "github ready · 26 tools · 310 ms"),
        ("12:41:03", "WARN", "mcp", "sentry failed to start: 401 Unauthorized"),
        ("12:41:09", "INFO", "turn", "turn 4 started · agent=Build model=claude-opus-5-5"),
        ("12:41:10", "WARN", "model", "429 rate limited · retry in 8s (2/5)"),
        ("12:41:18", "INFO", "tool", "Edit src/auth/jobs.py ok 4 ms"),
        ("12:41:18", "INFO", "tool", "Bash started job j1"),
        ("12:41:19", "DEBUG", "context", "prompt 122,230 tok · cache hit 81%"),
        ("12:41:21", "ERROR", "agent", "review a3 failed: 529 overloaded"),
    ),
)
