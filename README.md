# Nexus

A small, provider-agnostic Python agent harness. Nexus owns the agentic loop,
the message and tool contracts, permissions, sessions, context, and the
extension system; model providers are pluggable adapters behind one protocol.

Runtime dependencies are `httpx`, `msgspec`, `mcp`, `keyring`, `textual`, and
`textual-diff-view`. Python 3.11+ on macOS or
Linux. No vendor SDK, no agent framework, no OS sandbox.

New here? Read [ARCHITECTURE.md](ARCHITECTURE.md) for the design and
[EXTENDING.md](EXTENDING.md) to add tools, skills, agents, hooks, MCP servers,
or providers.

## Install

From a clean checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# To run tests/build wheels:
pip install -e '.[dev]'
```

The Textual chat shell is installed with the normal CLI runtime dependencies.
`nexus chat` requires an interactive terminal; use `nexus run` for piped or
non-interactive prompts.

Set at least one provider credential through the environment. Nexus never wants
a literal key in a config file:

```sh
export ANTHROPIC_API_KEY=...
# or OPENAI_API_KEY / GEMINI_API_KEY / GEMINI_GOOGLE_API_KEY ...
```

For a fully local setup, install [Ollama](https://ollama.com), pull a model, and
skip the key entirely — see [Offline and local](#offline-and-local).

## Get started

```sh
nexus --workspace /path/to/project init          # create nexus.toml, SOUL.md, MEMORY.md
nexus --workspace /path/to/project doctor        # validate config, providers, extensions, MCP
nexus --workspace /path/to/project run "Explain this repository"
nexus --workspace /path/to/project chat          # interactive Textual shell
```

`init` creates editable files without overwriting anything that exists; it is
local and needs no daemon. `run` takes a prompt, or `-` to read the prompt from
stdin. `chat` is the full-screen Textual shell; `Ctrl+P` opens chat commands,
`Ctrl+N` starts a session, `Ctrl+O` opens the Sessions dialog, `Ctrl+F` forks,
`Ctrl+B` / `Ctrl+L` toggle the sessions and details sidebars, `Ctrl+S` opens
Settings, and `Shift+Tab` cycles root agents. `Enter` sends the prompt, and `Shift+Enter` (or
`Ctrl+Enter`) inserts a newline; `Alt+Enter` does the same only when the
terminal's key protocol preserves the modifier; `Ctrl+J` inserts one too, which
is the fallback for terminals that cannot report a modified Enter. A non-TTY
invocation fails with guidance to use `nexus run` instead of silently changing
interaction modes.

The chat keeps its append-only session/event history unchanged. In the visible
timeline, completed setup/configuration/provider-auth failures are compacted out
after a later successful turn; a current or otherwise unresolved failure remains
visible. Generic historical event errors and failed turns remain available from
`/details` and the session export/replay. The canned “Hi! How can I help?” and
“I’m Nexus …” assistant greetings are omitted when they precede the first user
prompt. Routine ready/completed status text is not shown. Runtime/model metadata
sits below the composer, and errors/status feedback remain there when actionable.

`tests/browser_serve.py` is a development/test helper, not a shipped surface: it
is not installed with the package, `nexus` never imports it, and it is not a
supported serving path. It exists because plain `textual serve`'s bundled xterm
frontend reports every Enter variant as a bare carriage return, so Shift+Enter
would submit instead of newlining; the helper adds a small serving-layer keyboard
bridge that reports Shift/Ctrl+Enter as Kitty CSI-u, which the app already
understands, and nothing else. Use it only to serve a local checkout with the
contract intact, as `python tests/browser_serve.py --command "nexus chat"` does.

**Terminal support and limitations.** The terminal decides how Enter and
Shift+Enter are encoded. `nexus chat` works wherever the terminal speaks the
Kitty keyboard protocol — kitty, WezTerm, foot, Ghostty, recent iTerm2 — because
Textual negotiates it and resolves `CSI 13;2u` to `Shift+Enter`. The Nexus
key-protocol driver (`nexus/ui/tui/keys.py`) additionally decodes the older xterm
`modifyOtherKeys` form (`CSI 27;2;13~`) for terminals or tmux setups that already
emit it; that mode is not force-enabled, because doing so re-encodes printable
shifted keys in a way Textual's parser does not preserve. `Alt+Enter` is only a
newline where the terminal encodes it distinctly (`CSI 13;3u`, or the
`modifyOtherKeys` form the driver rewrites); terminals that map it to `ESC CR`
lose the modifier, and the app cannot recover it. Terminals that send a bare
carriage return for *every* Enter cannot be distinguished — those bytes mean
Enter — so there `Shift+Enter` submits and `Ctrl+J` (the `LF` byte) inserts the
newline. `tests/test_tui_keys.py` pins the decoder against the real byte
sequences, runs the driver under a pseudo-terminal, and drives the real
`NexusTextualApp`/`ChatEditor` through that driver to assert Shift+Enter drafts a
newline and Enter then sends it.

Every command except `init`, `auth`, and `nexus daemon status|stop|logs` is a **client of
a per-workspace daemon** — including `doctor`, `models`, `sessions`, `ext`,
`tools`, `agents`, and `replay`. If no daemon is listening, the client starts
one, waits (bounded) for readiness, and connects, so `nexus doctor` starts (or
reuses) the daemon rather than checking the workspace in-process. The daemon owns
the `Runtime`; the CLI never has an in-process fallback, so every surface sees
identical semantics.

```sh
nexus run "Inspect the failing tests and fix them" --session work
nexus run "Summarize the repository" --json > events.jsonl   # headless JSONL
cat prompt.txt | nexus run -
```

The daemon exits on its own after an idle window (300 seconds by default) with
no viewers and no running or queued turns. It never exits with a turn in
flight.

```sh
nexus daemon status      # running? pid, socket, live counters
nexus daemon logs        # tail the daemon log
nexus daemon stop        # graceful shutdown
```

## What it does

- **One agentic loop.** A turn streams model output, executes tool calls under
  permissions, feeds results back, and repeats until the model stops or a limit
  is hit. Assistant messages are persisted before tools run, so a crash resumes
  cleanly.
- **Multiple providers behind one IR.** Anthropic, any OpenAI-compatible
  endpoint (Responses or Chat Completions), Gemini, and Ollama/llama.cpp, all
  driven by the same loop. Capabilities come from the model registry.
- **Host execution with permissions, no sandbox.** Tools run with your user's
  privileges under declarative allow/ask/deny rules and path scoping. There is
  no OS sandbox or container isolation — an approved `Bash` call can do anything
  your shell can. Review prompts and keep `deny` rules.
- **Self-extension without a restart.** Tools, providers, in-process hooks,
  skills, agents, MCP servers, and config are watched data. `ReloadExtensions`
  (or editing a file) swaps an immutable manifest at the next loop iteration, in
  the same turn.
- **Sessions that survive their views.** One daemon runs many sessions
  concurrently. A turn with zero subscribers still completes and is fully
  replayable from the append-only log.

## Commands

| Command | Purpose |
| --- | --- |
| `nexus init` | Create `nexus.toml`, `SOUL.md`, `MEMORY.md` without overwriting. |
| `nexus doctor [--explain-reload] [--json]` | Validate config, providers, registry, extensions, MCP, and state what is hot vs. restart-only. |
| `nexus run <prompt\|->` | One turn. `--session NAME`, `--json` for headless JSONL. |
| `nexus chat` | Interactive Textual shell. `--session NAME`. Requires stdin/stdout TTY. |
| `nexus web [--no-browser]` | Open the workspace in a local browser client served by the running daemon (one-use launch URL). |
| `nexus replay <id> [--json]` | Re-render a session from its log (same path as `sessions replay`). |
| `nexus daemon status\|stop\|logs` | Manage the workspace daemon. `status --json`; `logs --lines N`. |
| `nexus sessions list` | List sessions with state, `last_seq`, viewers, title. |
| `nexus sessions fork <id> [--at-seq N]` | Branch a session. |
| `nexus sessions replay <id>` | Re-render a session from its log. |
| `nexus sessions export <id> [--format json\|markdown\|jsonl]` | Export a consistent prefix. |
| `nexus sessions delete <id> [--force]` / `restore <trash-id>` | Move to trash / restore. |
| `nexus ext list` | List live external modules and the manifest generation. |
| `nexus ext reload` | Run one extension rebuild and report loaded/unloaded/failed. |
| `nexus ext validate [target]` | Quarantine-check extensions without swapping the manifest. |
| `nexus ext trash <target> [--reason R] [--force]` | Move one managed extension file to trash and rebuild; rolls back unless `--force` when the rebuild fails. |
| `nexus tools list` | List the model-facing tool catalog for the current profile. |
| `nexus models list [--provider P] [--tier T] [--selectable] [--search Q]` | Reachable models from the registry. |
| `nexus models show <id>` / `refresh` / `tiers` | Inspect a model, force a catalogue refresh, list tiers. |
| `nexus agents list` | Discovered subagent definitions. |
| `nexus auth codex login [--profile NAME] [--headless]` | Experimental local ChatGPT OAuth device login; never contacts the daemon. |
| `nexus auth codex status [--profile NAME]` / `logout` | Check credential presence / delete the local credential without exposing identity or tokens. |

`--workspace PATH` is a global flag (defaults to the current directory). There is
no CLI `ext restore`: `ext trash` moves the file under the extension trash with
a `delete_after` retention record and rebuilds the manifest, but it is not
restorable from the CLI.

### Commands in `chat`

`/new` (`/clear`), `/sessions`, `/archived` (`/resume`), `/model`, `/effort`
(`/reasoning`), `/agent`, `/tools`, `/skills`, `/mcp`, `/settings`, `/details`, `/cost`,
`/copy`, `/diff`, `/theme`, `/verbose`, `/hotkeys`, `/reload`, `/review`, `/commit`,
`/reconnect`, `/cancel`, `/fork`, `/export`, `/help`, `/exit` (`/quit`). Aliases in
parentheses run the same command. `/effort` opens the reasoning-effort picker
(`/effort LEVEL` sets it directly); `Ctrl+T` cycles the supported levels in place.
They are available in the
multiline editor and the searchable `Ctrl+P` command palette. Model selection,
root-agent selection, session switching, fork, export, details, and reconnect
remain daemon operations through the host client.

The conversation begins with a four-block context header for the system prompt,
tools, skills, and MCP servers. User turns can be collapsed by their chevron.
The composer shows agent, model, provider, effort, and context usage such as
`24k (24%)`: the last request's size as the provider reported it (cached tokens
included), plus the estimated growth since, against the model's context window
(models.dev `limit.context`; `limit.input` also caps the prompt budget when
stated). `[context] max_tokens` is unset by default; set it only to cap the
window below the model's.
Before the first reply it is the assembler's estimate. The bar below it shows
context fill or turn activity. Clicking the System prompt block shows it
rendered as Markdown; the Tools block opens every tool definition, one
expandable row per tool (families of several tools are grouped). `Ctrl+I` (or
`/context`) opens the next request grouped as it is sent: the system prompt by
part, the tools, then each turn with its user message, replies, and every tool
call paired with its result, each with a token estimate. On wide terminals a
sessions sidebar (all current and archived sessions, live status, a spinner for
running turns, and a Delete button that moves each session to restorable trash)
and a details sidebar (session facts, files modified this session
with diffs, MCP server health) sit beside it; both hide on narrow terminals and
toggle with `Ctrl+B` / `Ctrl+L`. `Ctrl+O` (or `/sessions`) opens a searchable
Sessions dialog grouped by day, where `Ctrl+D` archives a session and `Ctrl+Z`
undoes it. Old idle sessions auto-archive after two days by default. `/archived`
opens the searchable archive browser to preview or resume them. `Ctrl+S` opens
Settings (a full-screen page with a sidebar: theme, panels, keyboard, workspace,
agents with model/effort/fallback fields, tools, MCP, skills, hooks, config, and soul),
saved to `$XDG_CONFIG_HOME/nexus/tui.json`. `/details` expands the reduced `ConversationView` into model,
usage, context budget/compaction, queued inputs, pending approvals, and the
subagent tree. `/reconnect` re-attaches from the last reduced `seq`, replays the
missed tail, and resumes an active turn without polling.

Tool calls stream as blocks headed `$ command`, `→ Read path`, or
`← Edit path`, showing the first lines of output; click a tool to expand it (edits
show their diff). Clicking a subagent's task opens a large modal with that
agent's whole conversation, rendered like the root and updated live while it runs, and a bounded preview where the event carries one (a permission request's key/preview, a tool's
progress line, or a result/summary field); control characters and obvious
credentials are escaped or redacted, and byte payloads are shown by size rather
than dumped. Assistant prose is rendered as Markdown with terminal controls
escaped, and the final `text` event is reduced without duplicating streamed
deltas.

The single-column transcript is a reducer-backed chronological timeline. User
messages, streamed assistant prose, and stable tool cards reconcile by their
durable turn/message/call IDs, so replay and reconnect update in place rather
than duplicate content. `Task` cards render their linked child agents inline;
Enter or click opens a live reducer-backed child transcript, including nested
agents. Scrolling away pauses tail-follow until the conversation is returned to
the bottom. Provider thinking collapses to a `Thought: …` line, and each
completed turn ends with `AGENT · model · duration`. The context header stays at
the top of the timeline and opens request details on click. `Ctrl+I` opens the
full request inspection view.

### Approval prompts

By default nothing asks: `permissions.mode` is `"allow"`, so every tool runs
without a prompt (hard path boundaries and any `deny` rules still apply). Set
`mode = "ask"`, or add `ask` rules, to approve calls. The chat and web clients
show an approval as a list above the composer, like the model and command
pickers: ↑/↓ and Enter, or `y`/`a`/`n`/`d`, with Esc denying once.

Interactive runs prompt before an `ask` action with four choices: `y` allow
once, `a` allow always (a session-scoped grant, replayed on later turns), `n`
deny once, `never` deny always. A denial is returned to the model as an error
tool result and the turn continues. EOF, an unrecognized answer, or an
exhausted prompt always resolves as **deny once** — never auto-allow.

`--json` is headless: it streams every persisted event envelope as JSONL on
stdout, sends diagnostics to stderr, and never prompts. With no approver
attached, the configured `permissions.on_unattended` policy applies (`deny` by
default, or `allow` / `fail_turn`).

## Configuration

Configuration is layered, increasing precedence:

```
built-in defaults  <  ~/.nexus/config.toml  <  <workspace>/nexus.toml
                   <  NEXUS_* environment  <  CLI flags
```

It is validated with `msgspec` and reread at the start of every turn; unknown
keys are hard errors. A minimal `nexus.toml`:

```toml
config_version = 2

[agent]
profile = "coding"            # coding | research | chat | ops

[models]
default = "medium"            # a tier name, "provider/model", or a bare id

[providers.anthropic]
api_key = "${env:ANTHROPIC_API_KEY}"

[permissions]
mode = "allow"                # default: no prompts; "ask" approves each call
deny  = ["Bash(rm -rf*)", "Read(**/.env)"]   # optional; deny always wins
write_roots = ["./"]
on_unattended = "deny"
```

The full surface (`[agent]`, `[models]`, `[model]`, `[providers.*]`,
`[context]`, `[permissions]`, `[tools]`, `[ext]`, `[agents]`, `[hooks]`,
`[mcp]`, `[session]`, `[telemetry]`) is defined in `nexus/config/schema.py`.
`[model]` is a compatibility section; `[models]` is canonical. If both set the
same field to different values, that is an error, not a precedence puzzle.

### Secrets references only

Config values may reference a secret; they may never contain one:

- `${env:VAR}` — resolved from the environment at request time.

Generic `${keychain:...}` config references are not implemented. The experimental
ChatGPT OAuth route uses the OS keychain directly; no token is ever in config.

Secrets are never written to the session log, never rendered into context, and
are scrubbed from logs and events. `~/.nexus/credentials.json` is created `0600`
and is the only file allowed to hold literal tokens. Every provider config
`repr` hides keys, env values, and credential-shaped argv/URL text.

## Providers

One adapter per wire protocol, selected by a `[providers.<name>]` block.

| Adapter | Covers | Notes |
| --- | --- | --- |
| `anthropic` | Claude | Reference adapter: Messages API, extended thinking + signatures, prompt-cache breakpoints, `/v1/messages/count_tokens`. |
| `openai` | OpenAI, **every** OpenAI-compatible endpoint, and the Codex models | `api = "responses"` (default) or `"chat"`. `base_url` is the extensibility lever. The Codex models are Responses-API models reached with a normal key, so they need no CLI adapter. |
| `gemini` | Google Gemini | `generateContent` streaming; different wire shape, and the adapter that proves the IR carries a second dialect. |
| `ollama` | Ollama, llama.cpp server | Local, keyless by default. `api = "ollama"` (default) or `"openai"`. Conservative capabilities: `tools = false` unless the registry says otherwise. |
| `opencode_agent` | OpenCode | Integrated **only** over its documented ACP subprocess surface (`opencode acp`). Nexus never reads OpenCode's credential store. Advertises `tools = false`: ACP tool calls are the agent's own loop, surfaced as raw events. |

Adding an OpenAI-compatible vendor is a config block, not code:

```toml
[providers.groq]
kind = "openai_compatible"
base_url = "https://api.groq.com/openai/v1"
api_key = "${env:GROQ_API_KEY}"
# api = "chat"   # default for compatible endpoints
```

An OpenAI-compatible provider **must** set `base_url`; Nexus will not silently
default a vendor to `api.openai.com`.

### Experimental ChatGPT OAuth for Codex

This optional route uses a private ChatGPT Codex endpoint, not a documented
public API. It is experimental and can change or stop working without notice.
It uses HTTP/SSE only, never Codex CLI, OpenCode ACP, or daemon RPC. Nexus keeps
ownership of prompts, Nexus tool schemas, permissions, tool execution, hooks,
sessions, cancellation, and tool-result replay; the provider only transports
Responses requests and normalizes model output.

```toml
[models]
default = "codex/gpt-5.6-luna"

[providers.codex]
auth = "chatgpt_oauth"
profile = "default"
api = "responses"
```

Do not set `api_key`, `base_url`, executable/ACP fields, or `OPENAI_API_KEY` for
this route; OAuth ignores that environment variable. API-key Codex remains
supported by omitting `auth` and setting `api_key = "${env:OPENAI_API_KEY}"`.
Run `nexus auth codex login --headless` from a terminal, open the printed URL,
and enter the displayed device code. Credentials are one bounded versioned JSON
 record in macOS Keychain or a Secret Service-compatible Linux keyring. Nexus
 refuses null, plaintext, file, and fail keyring backends and has no plaintext
 fallback. Access and ID tokens remain in memory only.

The deliberately gated live OAuth smoke test makes one real, text-only Codex
request through this provider path. It consumes a real subscription request and
the OAuth refresh may rotate the stored refresh token. Run it only when intended:

```sh
NEXUS_CODEX_OAUTH_LIVE=1 pytest -m live tests/test_codex_oauth_live.py
```

The final `-m live` overrides the repository's default `-m 'not live'` pytest
selection. Without `NEXUS_CODEX_OAUTH_LIVE=1`, the test skips before touching
the keychain or network.

Provider-level failure (a connection error, 429, or 5xx before any output)
falls back through `models.fallback` with a visible `model.retrying` event. A
refusal or a partial stream never falls back; the turn completes or surfaces the
error.

### Deliberate omission: GitHub Copilot

There is **no raw GitHub Copilot adapter.** Its OAuth token-exchange endpoint and
the licence terms for non-editor clients are unresolved, so shipping one would
mean guessing at both a network contract and a legal boundary. Nothing else in
Nexus depends on it.

## Models: registry and tiers

Nexus reads the [models.dev](https://models.dev) catalogue to know what models
exist, which adapter serves them, and what they cost. The catalogue is
descriptive data only: it can never set an endpoint or a credential.

- Fetched on first use to `.nexus/cache/models.dev.json`, TTL
  `models.refresh_ttl_days` (default 7). `[models] offline = true` pins to the
  vendored snapshot and never fetches.
- Only providers whose env vars are present (or that appear in
  `[providers.*]`) are considered reachable, so `nexus models list` stays small.
- The vendored fallback (`nexus/model/data/models.min.json`) and its models.dev
  MIT attribution ship inside the package; `nexus/model/data/NOTICE` carries
  the full licence text. The upstream catalogue (`anomalyco/models.dev`, the
  renamed `sst/models.dev`) is MIT-licensed, `Copyright (c) 2025 models.dev`,
  and the vendored file is a hand-authored, filtered subset with no logos. A
  fetched catalogue tagged `_license: "pending"` is flagged `license_pending`
  rather than trusted. `nexus models refresh` forces an update.

Models are grouped into **tiers**, usable anywhere a model string is accepted:

```toml
[models]
default = "medium"

[models.tiers]
high   = ["anthropic/claude-opus-5", "openai/gpt-5.6"]
medium = ["anthropic/claude-sonnet-5"]
low    = ["anthropic/claude-haiku-4-5"]
```

Resolution order: an explicit `[models.tiers]` pin, then the curated map, then a
blended-cost fallback (`input + output/4`; `low <= 2.5 < medium <= 10 < high`),
then `low` when no cost data exists. Custom tier names are allowed; the three
built-ins always resolve. A tier name works in `models.default`, a skill's
`model:`, an agent definition's `model:`, and `Task(model=...)`.

An optional exact supported-effort override can be declared per explicit model
reference. Values use the canonical Nexus effort vocabulary (`none`, `minimal`,
`low`, `medium`, `high`, `xhigh`) and are exposed in that canonical order:

```toml
[models.reasoning_efforts]
"openai/o3" = ["low", "high"]
"anthropic/claude-sonnet-5" = ["none", "medium", "high"]
```

An override replaces catalogue metadata, including with an empty list. Catalogue
`reasoning = true` alone does not imply supported effort levels.

Capabilities are authoritative from the registry. If a provider rejects a
feature the registry claimed, the loop emits `context.degraded`, retries once
without the feature, and emits a `registry.mismatch` event. `nexus doctor`
aggregates those durable mismatch events so a bad upstream entry can be fixed.

## Tools and permissions

Tools execute **on the host** under the permission engine. There is **no OS
sandbox and no container isolation**: an approved `Bash` call runs with your
user's privileges.

### Local document reading

`read` converts supported local documents to Markdown with
[Firecrawl AnyDoc](https://github.com/firecrawl/anydoc). Install the optional
dependency with:

```sh
pip install 'nexus-harness[documents]'
```

Supported extensions are `.pdf`, `.doc`, `.docx`, `.docm`, `.ppt`, `.pptx`,
`.pptm`, `.pps`, `.pot`, `.ppsx`, `.ppsm`, `.xls`, `.xlsx`, `.xlsm`, `.xlsb`,
`.odt`, `.ods`, `.odp`, `.rtf`, and `.epub`. CSV conversion is available only
when requested with `csv_as_markdown: true`; otherwise `.csv` is read as
ordinary raw UTF-8 text. Source documents are limited to 16 MiB and converted
Markdown to 4 MiB, with the normal configured result and line limits applied
after conversion. A scanned PDF that needs OCR returns a clear error: conversion
is local-only, does not call hosted OCR, and makes no conversion network request.

Document reads remain non-mutating and pass through path-keyed `read` permission
checks and hard `read_denyroots`. Individual file reads are not globally confined
to the workspace; directory listing is limited to the workspace. The conversion
worker runs as the same OS user as Nexus with a scrubbed environment, not inside
a strong OS sandbox. Images remain a separate future goal and are not attached
or converted by this document feature.

Bundles group tools; profiles compose bundles. Nothing in `core/` knows what
"coding" means.

| Bundle | Tools |
| --- | --- |
| `fs` | Read, Write, Edit, MultiEdit, Glob, Grep, LS |
| `shell` | Bash, BashOutput, KillShell |
| `task` | Task, TodoWrite, question |
| `meta` | ReloadExtensions, ListExtensions, WriteTool |
| `ext` | Skill |
| `mcp` | Everything bridged from MCP |

| Profile | Bundles | Notes |
| --- | --- | --- |
| `coding` | fs, shell, task, meta, ext, mcp | Default. |
| `research` | fs, mcp, + Task | Structurally read-only: Write/Edit/MultiEdit are dropped, and no mutating tool can be enabled. |
| `chat` | none | |
| `ops` | shell, mcp | |

### Asking the user (`question`)

The `question` tool lets the root agent or any subagent pause for a decision
that is genuinely the user's: a prompt with one to three options, or free text.
The chat and web clients show it as the same list prompt as approvals (number
keys pick an option); the answer returns to the waiting agent as its tool
result. `question.requested`/`question.resolved` are durable events, and a
pending question is simply the running `question` call, so it reappears after a
reconnect. A question never opens an approval of its own and its answer never
changes a permission. With no one attached, it fails fast and tells the model to
proceed on its own judgment; an unanswered question times out after 15 minutes.

### Permission rules

```
Tool                    whole tool, any arguments
Tool(pattern)           glob match against the tool's permission_key
Bundle:name             every tool in a bundle
mcp__server__*          MCP wildcard tool names
```

Evaluation is first-match-wins: `deny` → session grants → `allow` → `ask` →
`mode`. **`deny` is absolute** and cannot be overridden by a session grant, a
path trick, or the model. `write_roots` and `read_denyroots` are hard boundaries
checked before any rule allow, after path resolution (symlinks followed), so
`../` and symlink escapes fail closed.

A tool's `permission_key` says what a rule matches: `Bash` returns the command
string, `Read`/`Write` return the resolved absolute path, MCP tools return the
server-qualified name. A keyed grant persists only as an exact-action rule, so
an `ALLOW_ALWAYS` decision can never silently broaden into a whole-tool grant.

Headless behaviour is a config choice, not an accident:
`permissions.on_unattended = "deny" | "allow" | "fail_turn"` (default `deny`),
and a denial tells the model exactly which rule to ask the user for.

## Self-extension: trusted code, quarantine, hot reload

Extensions come in two tiers:

- **Data** — skills, agents, hooks, MCP servers, `nexus.toml`, `SOUL.md`,
  `MEMORY.md`. Parsed, not imported.
- **Code** — `*.py` tools and in-process hooks under `.nexus/tools/` and
  `.nexus/hooks/`. Imported under a version-stamped module name, never
  `importlib.reload`, so an in-flight call keeps its generation while new calls
  get the next one.

Three triggers land in the same serialized rebuild: the directory watcher
(`ext.watch_interval_ms`, default 500; `0` disables), the model calling
`ReloadExtensions()`, and `nexus` / UI API calls. The loop re-reads one
immutable manifest each iteration, so a tool written in iteration N is callable
in iteration N+1, same turn, no restart. A turn never sees a half-updated world.

**Honesty about quarantine.** Before an extension is imported it is read under a
size cap, `ast.parse`d, scanned for import-time side effects, imported in an
isolated subprocess with a hard timeout, and schema-validated. A broken file
leaves the previous manifest untouched and its error is returned to the model so
it can fix and retry. Quarantine catches syntax errors, import crashes, hangs,
and obvious side effects. It does **not** sandbox arbitrary Python: a loaded
extension is **trusted code**, exactly as `nexus.toml` and `SOUL.md` are. The
permission engine gates *calls*, not *loading*. Only enable extensions you have
reviewed.

**What still needs a restart.** Core Nexus modules (`core/`, `model/message.py`,
`runtime.py`), new pip installs, and the manifest structure itself. `nexus doctor
--explain-reload` states exactly what is hot and what is not.

## Skills

A skill is a directory with `SKILL.md`. Only `name: description` enters the
context (one line each); the body loads when the model invokes it. A hundred
skills cost little while idle.

```markdown
---
name: risk3-docker-testing
description: Run DS-pack tests locally inside the Risk3 container. Use when...
allowed-tools: [Bash, Read, Glob]
bundles: [fs, shell]
model: inherit
version: 1
---

# body: loaded only on invocation
```

Precedence is workspace > user > builtin. Adding `SKILL.md` makes the skill
available on the next loop iteration. A skill may bundle `scripts/`,
`references/`, and `tools/*.py`; skill tools register only while the skill is
active.

## Subagents (`Task`)

`Task` spawns a nested runtime with its own session, tool set, and model. Its
events re-emit on the parent bus with an agent field; its final report returns
as a tool result.

```toml
[agents]
max_tier = "medium"        # requested tiers clamp, never widen
max_concurrent = 4
max_depth = 3
max_fanout = 16
default_type = "task"
```

Nexus ships one root agent and three subagents. They are built in and apply to
every workspace. Edit them in **Settings → Agents**: saving writes an override to
`~/.nexus/agents/<name>.md`, and "Reset to default" moves that override to trash.

Every new session starts with `build`. To change that, pick another root agent
under **New sessions start with** in Settings → Agents (terminal and browser).
It writes `[agent] name` to `~/.nexus/config.toml`, or to `.nexus/nexus.toml`
with the project scope, which wins over the global one. Choosing an agent inside
a session (`/agent`, `Shift+Tab`) changes only that session.

| Agent | Kind | Tools | Purpose |
| --- | --- | --- | --- |
| `build` | root | the session's set | The default agent. Makes the change, verifies it, and delegates. |
| `advisor` | subagent | read-only | Gives a reasoned recommendation on a design, plan, or bug. Cannot edit files. |
| `task` | subagent | inherits the parent's set | Does a self-contained, multi-step piece of work. |
| `quick` | subagent | inherits the parent's set | Handles small, well-specified jobs fast. |

Model, provider, reasoning effort, and an ordered `fallback` list are unset by
default, so every agent inherits the session model. Set them per agent in
Settings. An agent's fallbacks are tried, in order, before the workspace
`models.fallback` chain when its model fails before replying.

Every subagent ends with a report. When `task` or `quick` edit files, the report
lists them, and Nexus appends the files each child changed through its edit
tools to the report the root agent receives.

A child can never exceed its parent: tool sets intersect, permissions inherit,
`deny` stays absolute, tier is capped by `agents.max_tier`, and fan-out/depth
are bounded. `advisor` has no write path at all, enforced structurally rather
than by prompt. The legacy names `general` (now `task`, or `build` as a root)
and `explore`/`plan`/`planner` (now `advisor`) still resolve when no definition
with that name exists. Older releases copied the roles into `.nexus/agents/`;
copies you never edited are moved to `.nexus/trash/agents/` once, so they stop
shadowing the built-ins. Set `agents.seed_roles = true` to copy the built-ins
into a workspace for per-project editing.

## Hooks

Deterministic behaviour the model cannot skip. A hook attaches to a lifecycle
event and returns allow / warn / block / modify.

Events: `SessionStart`, `UserPromptSubmit`, `ContextAssembled`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `TurnEnd`, `SessionEnd`, `ExtensionLoaded`.

```toml
# .nexus/hooks.toml
[[hooks.PreToolUse]]
matcher = "Write(**)"
type = "command"
command = ["python3", "-c", "import json,sys; sys.exit(1 if 'TODO' in json.dumps(json.load(sys.stdin)) else 0)"]
on_nonzero = "block"       # block | warn | ignore
timeout_s = 10
```

Command hooks are argv with no shell unless `shell = true` is opted into; they
receive a bounded `NEXUS_*` environment and the invocation as JSON on stdin.
In-process Python hooks live in `.nexus/hooks/*.py`, load through the same
quarantine path as tools, and are trusted code. A `modify` decision must be
revalidated and re-gated by the caller; a hook is policy, not a permission
grant.

## MCP

MCP servers are declared in `.nexus/mcp.json` (JSONC: comments and trailing
commas allowed):

```jsonc
{
  "servers": {
    "github": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "${env:GITHUB_TOKEN}" }
    },
    "internal": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:MCP_TOKEN}" }
    }
  }
}
```

Transports: `stdio`, `http` (Streamable HTTP), `sse`. Only explicit
`${env:VAR}` interpolation is performed; a bare `$VAR` is left literal. Servers
connect lazily, with health checks, exponential backoff, and a circuit breaker.
A dead server never fails a turn: its tools vanish from the manifest, an
`mcp.failed` event is emitted, and every other server is unaffected. Editing
`mcp.json` takes effect live.

MCP tool descriptions and results are **untrusted data**: they are wrapped in a
delimiter with a standing no-authority instruction. The permission engine is the
real backstop.

## Sessions, context, and cache

Sessions are an append-only JSONL event log plus periodic snapshots:

```
.nexus/sessions/<id>.jsonl       one event per line, fsync'd, never rewritten
.nexus/sessions/<id>.snap.json   {seq, messages, summary?, usage}
```

Fork, replay, and compaction are cheap and lossless: compaction writes a new
snapshot, and the log still holds the original. "Omission from the prompt does
not delete history." `nexus sessions export` renders a consistent prefix.

```python
sessions.open(id), sessions.fork(id, at_seq=None), sessions.list()
sessions.delete(id), sessions.restore(trash_id), sessions.export(id, format=...)
```

Context is assembled from composable, priority-budgeted parts (identity, SOUL,
environment, tools, skills index, MCP index, memory, attachments, history,
user). The budget is `min(config.context.max_tokens, model context) −
max_output − safety_margin`, and at most the model's input limit minus the
safety margin when the catalogue states one. An unset `context.max_tokens` means
the model's window (180,000 if the catalogue does not know the model). Priority-0 parts are never dropped; if they alone
overflow, the turn fails with an actionable error. Compaction strategies are
`drop_oldest`, `evict_tool_results`, `summarize`, and `hybrid` (default), all
explicit and observable via `context.compacted`.

Token counts use the provider's `count_tokens` where available, cached on disk
by a canonical semantic hash; otherwise a calibrated heuristic. Prompt-cache
boundaries are placed after the stable system+tools prefix and after the last
stable history boundary when the provider supports caching. The cache file
stores only the hash, the count, and a timestamp — never prompt content.

## Surfaces and security

The core is exposed through a transport-neutral facade
(`nexus/host/facade.py`); a UI may call the facade and nothing else. The local
CLI speaks a length-framed JSON protocol over a Unix domain socket.

- The socket lives at `~/.nexus/daemon/<hash-of-workspace>.sock`, mode `0600`
  inside a `0700` directory. One workspace maps to exactly one daemon.
- A duplicate daemon refuses to start (`flock` + pid file). A stale socket left
  by a dead daemon is reclaimed, not reported as an error.
- A version handshake rejects a client/daemon built from different protocol
  revisions loudly; it is never retried.
- `deny` rules are evaluated daemon-side and are absolute; credentials never
  traverse the facade in either direction, and every error the wire can observe
  is redacted.

`nexus web` opens the browser client: the daemon starts a loopback listener and
prints a one-use launch URL whose ticket is exchanged for an `HttpOnly` cookie
and CSRF token (`nexus/host/web.py`). The page (`nexus/ui/web/`, plain
HTML/CSS/JS) shows the same sessions with live status and archive, the
conversation, approvals, a details panel with modified files and MCP health,
and settings. See `docs/web.md`.

`http_sse` is also the peer transport: commands as POST,
events as SSE (whose `Last-Event-ID` maps exactly onto the log's `seq`). It is
implemented and tested in `nexus/host/transports/http_sse.py` and
`tests/test_http_sse_transport.py`, with its constraints fixed: bind `127.0.0.1`
only, a bearer token required by every request, `Origin` checked on every
request, credentials never crossing the facade, and `deny` evaluated
daemon-side.

The daemon serves it only as an **opt-in, off-by-default** surface, enabled with
`NEXUS_HTTP=1`, the daemon entrypoint's `--http` flag
(`python -m nexus.host.daemon --http ...`), or programmatically through
`Daemon(http=True)`. The root `nexus` CLI is a pure client and has **no** flag to
enable it: turning the surface on is a daemon-start decision, not a per-command
one. It then binds a loopback port and publishes the host, port, bearer token,
and allowed origins in a mode-`0600` discovery file beside the socket
(`default_http_path` / `read_http_endpoint`), removing it on shutdown; the token
is never logged and never folded into health/status. The end-to-end path is
covered by `tests/test_http_daemon_e2e.py`. The web *frontend* remains deferred,
and the terminal CLI still speaks only the Unix socket.

A UI may import only `nexus.host`, `nexus.view`, `nexus.events`, and the
standard library. That boundary is enforced by a test, not by discipline.

Strict tests enforce independent physical-line budgets: `core/` + `model/` +
`tools/spec.py` under 14,000; `host/` under 7,000; `view/` under 2,200; and `ui/`
under 5,000. The host allocation was raised from 6,500 for the added browser,
file-completion, metadata, and redacted-diagnostics surfaces; the UI allocation
was raised from 4,500 to 5,000 for the browser client and expanded Textual
composition, including the right drawer, composer/picker, session diagnostics,
and completion. The host/view/ui values are separate reviewed budgets and
ratchets, not a combined allowance.
The closeout report refuses to record any directory at or above its cap. See
`ARCHITECTURE.md` and `tests/test_phase3_exit.py`.

## Offline and local

Everything except a hosted model call works with no network:

- `[models] offline = true` uses the vendored models.dev snapshot and never
  fetches.
- `ollama` is keyless by default (`base_url` defaults to
  `http://localhost:11434`). Capabilities are conservative: `tools = false`
  unless the registry says otherwise, and the loop adapts.
- The whole test suite is offline; provider adapters are exercised against
  recorded fixtures and a deterministic `ScriptedProvider`.

A minimal local-only config:

```toml
config_version = 2

[models]
default = "ollama/qwen3:32b"
offline = true

[providers.ollama]
base_url = "http://localhost:11434"

[permissions]
mode = "ask"
on_unattended = "deny"
```

## Troubleshooting and `doctor`

```sh
nexus doctor                 # config, providers, registry, extensions, MCP
nexus doctor --explain-reload
```

`doctor` validates the layered config, reports which providers are configured
and reachable, whether the model registry is fresh or stale, which extensions
loaded or were refused (with reasons), and which MCP servers are connected,
degraded, or failed. It also aggregates the durable `registry.mismatch` events
recorded when a provider rejects a capability the registry claimed: a count, the
sessions involved, per-provider/model/reason tallies, and a bounded sample. The
scan reads a bounded tail of a bounded set of session logs, skips an
unreadable/corrupt one, and never opens a session or surfaces a raw provider
error. It never performs a model request.

Common checks:

- **`Executable not found` / auth errors** — this checkout no longer requires
  the Codex CLI. Configure a provider block and a key reference.
- **`another daemon already owns <workspace>`** — a daemon is running for that
  workspace; `nexus daemon status` then `stop` if needed.
- **Protocol version mismatch** — upgrade the client and daemon together; the
  handshake will not retry.
- **`daemon did not become ready`** — run `nexus daemon logs`.
- **`AF_UNIX path too long` on a deep `$HOME`** — the socket is
  `~/.nexus/daemon/<hash-of-workspace>.sock`, and a Unix socket path is capped
  near 104 bytes (108 on Linux). A long home directory makes the daemon's bind
  fail, and client auto-start then reports `daemon exited ... before readiness`.
  Run the client with a shorter `HOME` (for example `HOME=/tmp/nx nexus doctor`),
  which also relocates `~/.nexus` config and credentials to that shorter tree.
- **A tool call is denied** — the error result names the rule to grant. `deny`
  rules cannot be overridden.
- **Context overflow** — lower `context.max_tokens`, raise
  `context.compact_at_fraction` selectivity, or enable `summarize` compaction.
- **A new extension is not visible** — check `nexus ext list`; a failed load is
  reported to the model and logged, and the previous manifest stays live.

## Python API

The facade is transport-neutral; a host application can drive it directly
without a daemon. The offline example uses a `ScriptedProvider` so it runs with
no credentials:

```sh
python3 examples/python_api.py
```

See [examples/](examples/) for a facade example plus custom tool, skill, agent,
hook, and MCP config samples.

## Project decisions

- **Raw GitHub Copilot is omitted**; the token-exchange endpoint and licence
  terms for non-editor clients are unresolved.
- **OpenCode is integrated over ACP only** (`opencode acp`). Nexus never reads
  OpenCode's credential store, and ACP tool calls stay inside the agent.
- **Codex models are reached through the OpenAI adapter** (Responses API with a
  normal key), not through a Codex CLI adapter.
- **models.dev attribution is preserved**: the catalogue is MIT-licensed
  (`Copyright (c) 2025 models.dev`) and its notice ships in
  `nexus/model/data/NOTICE`. The vendored snapshot is a filtered subset; a
  fetched catalogue marked `_license: "pending"` sets `license_pending` on the
  registry status (unverified redistribution) rather than being trusted.
- **No OS sandbox.** Tools run on the host under permissions. This is a
  permanent property, stated plainly rather than papered over.
- **Extensions are trusted code.** Quarantine validates before loading; it does
  not sandbox.

## Development

The live agent benchmark has two scenarios and requires a configured provider;
its workspaces and run logs are written under ignored `artifacts/benchmark/`.
See the [benchmark guide](benchmark/README.md) for commands, grading, and safety
notes. The `shell-command` scenario requires `--allow-shell` and is
**UNSANDBOXED**; live runs may consume tokens.

```sh
pip install -e '.[dev]'
pytest                       # full offline suite; live tests are deselected
ANTHROPIC_API_KEY=... pytest -m live tests/test_anthropic_live.py

# The linter is not part of the `dev` extra; install it separately.
pip install ruff
ruff check nexus tests

# Terminal key-protocol regression: raw Shift+Enter / Ctrl+Enter / Ctrl+J bytes
# through the real decoder, a pseudo-terminal driver run, and an end-to-end PTY
# run of the real NexusTextualApp/ChatEditor over that driver.
pytest tests/test_tui_keys.py

# Textual web rendering plus browser screenshots (writes ignored artifacts/visual-tui/).
python tests/visual_tui_check.py

# Real-browser functional check of the Textual shell: types into the editor,
# asserts Shift+Enter newlines before Enter submits the multiline draft, opens
# Commands, and captures screenshots. Uses the deterministic fixture transport
# (no daemon or model) served through tests/browser_serve.py. Install the browser
# once with `python -m playwright install chromium`.
python tests/playwright_tui_check.py
```

Tests use temporary workspaces and recorded fixtures; they need no network and
no authentication. Provider conformance runs every adapter against the same
fixture suite, which is what makes "add a provider later" safe.
