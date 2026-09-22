# Nexus

A small, UI-independent Python agent harness. Runtime dependencies are `httpx`
and `msgspec`. Python 3.11+ on macOS or Linux, with an installed and
authenticated Codex CLI.

The harness loop is **load → build context → stream provider events → save**.
Codex performs the model/tool loop, including workspace inspection, file editing,
and command execution. Nexus provides the application boundary around it.

## Run it

From this checkout, install the runtime dependencies with `pip install -e .`,
then run:

```sh
codex login
python3 -m nexus doctor
python3 -m nexus run "Explain this repository"
python3 -m nexus chat
python3 -m nexus run "Continue the work" --session my-project --json
```

For a `nexus` executable, install in your Python environment with `pip install -e .`.
To use another workspace:

```sh
nexus --workspace /path/to/project init
nexus --workspace /path/to/project chat --session main
```

`init` creates configuration, instructions, and memory files, preserving existing
files. `run -` reads its prompt from stdin. Sessions default to `default`; reuse a
name to continue or choose a new name for a fresh conversation. Ctrl-C cancels the
active turn and exits. EOF or `/exit` leaves the interactive prompt.

## Native commands (opt-in)

`native-run` and `native-chat` drive Nexus's own loop and tool stack
(`Runtime` + `Session`) rather than the Codex CLI. They are additive: `run` and
`chat` above are unchanged and still use Codex.

```sh
python3 -m nexus native-run "Inspect the failing tests and fix them"
python3 -m nexus native-run "Summarize the repository" --json
python3 -m nexus native-chat --session work
```

Tools execute **on the host** under the permission engine (declarative
allow/ask/deny rules plus path scoping). There is **no OS sandbox or container
isolation**: an approved `Bash` call runs with your user's privileges, so review
prompts and keep `deny` rules for anything destructive.

Interactive runs (`native-run` without `--json`, and `native-chat`) prompt before
an `ask` action with four choices: `y` allow once, `a` allow always (a
session-scoped grant, replayed on later turns), `n` deny once, `never` deny
always. A denial is returned to the model as an error tool result and the turn
continues; only a failed turn or harness error makes the command exit nonzero.
EOF, unrecognized input, or an exhausted prompt always resolves as deny once — it
can never auto-allow.

`--json` is headless: it streams every persisted event envelope as JSONL on
stdout, sends diagnostics to stderr, and never prompts. No approver is attached,
so the configured `permissions.on_unattended` policy applies (`deny` by default,
or `allow` / `fail_turn`). Minimal native `nexus.toml`:

```toml
config_version = 2

[model]
default = "anthropic/claude-sonnet-4-5"

[providers.anthropic]
api_key = "${env:ANTHROPIC_API_KEY}"

[permissions]
mode = "ask"
allow = ["Read(**)", "Glob(**)", "Grep(**)", "LS(**)"]
deny = ["Bash(rm -rf*)", "Read(**/.env)"]
write_roots = ["./"]
on_unattended = "deny"
```

The native path reads only `${env:...}`/`${keychain:...}` credential references;
it never writes secret values to configuration, events, or context, and it does
not claim shell sandboxing. `run` / `chat` remain the default legacy commands.

## Python and UI integration

```python
import asyncio
from nexus import Agent

agent = Agent("/path/to/project")
answer = asyncio.run(agent.run("Inspect the failing tests", session="work"))
```

In an async application, use `await agent.run(...)`. For a GUI, TUI, or service,
consume `agent.stream(...)`. The harness never reads terminal input or renders UI.

```python
from contextlib import aclosing

async with aclosing(agent.stream("Fix the issue", session="work")) as events:
    async for event in events:
        handle_event(event.to_dict())
```

Always use `aclosing` when a consumer may stop early. Cancel the consuming asyncio
task to stop a run. Providers are closed and Codex's process group is terminated.
Keep event handlers quick; queue events to your UI thread if needed. The CLI's
`--json` exposes the same event dictionaries as JSONL.

| Event | Data | Meaning |
| --- | --- | --- |
| `started` | `session`, `context_chars`, `omitted_exchanges` | Context prepared |
| `message` | `text` | Completed agent message; not a token delta |
| `provider` | Original Codex event | Tool progress, usage, lifecycle, future event types |
| `completed` | `session`, `text` | Successful turn persisted and session lock released |

Treat messages as provisional until `completed`. The final text joins the agent
messages in emission order. Provider `turn.completed` precedes Nexus's own commit;
it is not the harness success signal. Python errors propagate as exceptions;
`run --json` emits an `error` event and exits nonzero. Ctrl-C exits 130.
Raw provider events are provider-specific; UIs should tolerate unknown types.

## Configuration and self-configuration

One flat `nexus.toml`, validated and reread at the start of each turn:

```toml
executable = "codex"
# model = "your-model"  # Omit to use the Codex default.
sandbox = "workspace-write" # Or "read-only".
timeout_seconds = 900
context_chars = 64000
instructions_file = "SOUL.md"
memory_file = "MEMORY.md"
```

Unknown keys and invalid values fail before launching Codex. Context file paths
must resolve inside the workspace. Missing note files are treated as empty.
Changes to configuration, instructions, and memory apply to the next turn, even
in an existing `Agent` instance. Each active turn uses one immutable snapshot.
The executable is a name or path, not a shell command or arbitrary argument list.

The agent can edit these ordinary files when asked, using Codex's workspace tools.
When running against this checkout it can also inspect and modify its own source,
run the offline tests, and update the documentation. Restart the Python host to
load source changes. Example:

```sh
python3 -m nexus run "Set the context budget to 48000 characters and verify the configuration."
```

Nexus sets the sandbox explicitly and disables interactive approval requests.
Commands requiring approval will fail; the UI does not need an approval dialog.
Codex retains its own installed configuration and authentication. Nexus does not
install or configure skills, MCP servers, or plugins. Existing Codex configuration
may already enable integrations. Workspace configuration and source are trusted
code/configuration, not a security boundary; use `read-only` for inspection.

## Simple context, durable sessions

Each turn starts an ephemeral Codex session with one JSON prompt containing:

1. Instructions and editable memory, always included.
2. The most recent contiguous suffix of complete user/assistant exchanges.
3. The current message, always included.

`context_chars` limits the actual serialized prompt in Unicode characters, not
model tokens. Old exchanges are omitted whole; notes and the new request are
never silently truncated. If these alone exceed the budget, the turn fails with
an actionable error. The `started` event reports omissions. Codex's own system
instructions, repository context, and within-turn tool results are outside this
budget and remain Codex's responsibility.

All successful exchanges remain in `.nexus/sessions/<name>.json`; omission from a
prompt does not delete history. There is no automatic summarization, retrieval,
or hidden model call. Keep durable facts in `MEMORY.md` when appropriate. Tool
traces are streamed but not replayed or stored by Nexus. Memory and workspace files
are shared across sessions; conversational histories are separate.

Atomic file replacement prevents partial snapshots. A nonblocking OS lock
prevents overlapping turns in the same session, including from separate
processes. Other sessions may run concurrently, but workspace edits are not
isolated: use separate workspaces for concurrent editing. Locks release on
process death. The filesystem should be local and support `flock` and atomic
rename. History is loaded and rewritten in full, appropriate for small local
sessions; a database-backed store can come later if measurements warrant it.

Failures and cancellation do not commit partial exchanges. File edits and other
tool effects may already have happened; Nexus does not roll them back or retry
implicitly. A hard kill of the Python host may leave a running Codex child; normal
cancellation and generator closure perform cleanup. Session files contain plain
text conversation data. `.nexus/` is excluded from version control in this checkout.

## Extension contract

The public extension point is `Provider.stream(prompt, *, workspace, config)`:
return an async iterator of `Event` values, emit `message` for assistant text,
raise on failure, and finish normally only when the turn succeeded. Close owned
resources in `finally`. Inject it with `Agent(path, provider=your_provider)`.
See the offline fake provider in `tests/test_harness.py` for a working example.

Future skills can contribute explicit context before `build_context`; future
MCP support can live in provider configuration and the provider adapter. Neither
requires UI logic or tool dispatch in the core loop. Add typed configuration
fields with validation when a concrete feature needs them. No speculative plugin
loader, registry, framework hooks, or skill/MCP implementation is included.

Files have one purpose: `agent.py` orchestrates, `context.py` selects context,
`config/` validates settings, `store.py` persists sessions, `provider.py` adapts
Codex, and `cli.py` renders the terminal interface. `Event` and `Provider` are the
small public contracts to preserve while extending the system.

## Development

Run the full offline suite with pytest:

```sh
pip install -e '.[dev]'
pytest
```

`python3 -m unittest discover -s tests -v` is retained only as a legacy
compatibility check; it runs the original `unittest` tests and does not exercise
the pytest-style Phase 0 suite.

Tests use temporary workspaces and an executable fake Codex to exercise actual
pipes, stderr backpressure, lifecycle events, failure, timeout, cancellation,
session exclusion, persistence, context limits, and configuration reload. They
need no authentication or network. No GUI, full-screen TUI, server, or Windows
process/locking adapter is included in this version.

The Codex adapter follows the official
[non-interactive mode documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
and was checked against local Codex CLI 0.153.4. It uses `exec --json --ephemeral`
and passes the prompt through stdin.
