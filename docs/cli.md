# CLI, one-shot runs and chat commands

`nexus/cli.py` is the canonical terminal entry (`nexus = nexus.cli:main`). It is
a **pure client**: every subcommand except a few local ones (`init`, `auth`,
`claude`, `update`, `searchserver`, `daemon`) is a host command over the daemon's
Unix socket, and the daemon auto-starts. Global flags: `--workspace PATH`
(default cwd), `--version` (reads the cached update notice, never the network),
`--dev` ([devtools.md](devtools.md)), `--session ID` (reopen in chat).
With no subcommand, `nexus` opens `chat`. Bare `nexus voice` shows voice status.

## Subcommands

| Command | What it does |
| --- | --- |
| `chat` | Native Ratatui chat; needs stdin/stdout TTYs and never falls back to a line reader |
| `desktop [--session ID]` | native Rust GPUI window; no TTY required, separate source build ([desktop.md](desktop.md)) |
| `web [--no-browser]` | `WebLaunch` → one-use browser URL ([web.md](web.md)) |
| `run MESSAGE [--session ID] [--json]` | one turn (session defaults to `default`) over the daemon; `-` reads stdin; human rendering with terminal approvals, or `--json` JSONL event envelopes (unattended) |
| `replay SESSION [--json]` / `sessions replay` | re-render a session from its log through the reducer |
| `sessions list\|fork\|replay\|export\|delete\|restore` | session management (`fork --at-seq`, `export --format json\|markdown\|jsonl`, `delete --force`) |
| `models list\|show\|refresh\|tiers\|select` | registry inspection (`list --provider --tier --selectable --search`); `select --session` |
| `agents list\|select\|current\|reset` | subagent definitions and the session's root agent |
| `tools list` | the model-facing tool catalogue for the current config |
| `ext list\|reload\|validate\|trash` | extension inspection, rebuild, quarantine check, safe trash |
| `worktrees list\|inspect\|review\|acknowledge\|integrate\|discard` | child worktree review flow ([agents.md](agents.md#worktrees)) |
| `voice status\|download\|remove\|transcribe` | local dictation ([voice.md](voice.md)) |
| `doctor [--json] [--explain-reload]` | config, providers, registry, extensions, MCP; no model request |
| `daemon status\|stop\|restart\|logs` / `restart` | workspace daemon management |
| `init` | create `nexus.toml`, `SOUL.md`, `MEMORY.md` without overwriting |
| `auth codex login\|status\|logout` | local ChatGPT OAuth (no daemon RPC) |
| `claude init` | set up the Claude Agent SDK provider |
| `update [--channel stable\|git --ref --version --no-restart]` | upgrade Nexus and restart running daemons ([release.md](release.md)) |
| `searchserver start` | start the local SearXNG Docker service |
| `mock list\|run\|clean` | dev mode scenarios |

## Layout

| Path | Role |
| --- | --- |
| `client/protocol.py` | `Client`: typed, transport-neutral methods over the host protocol (one per command), `Transport` protocol, `handshake` |
| `client/turn_stream.py` | `turn_events`: attach to a turn's stream before starting it, so no event is missed |
| `ui/cli/uds.py` | UDS transport for the CLI |
| `ui/cli/run.py` | one-shot `nexus run` |
| `ui/cli/render.py` | `TerminalRenderer`: events → terminal text, per-session replay dedup |
| `ui/cli/approve.py` | terminal approval prompt (`y`/`a`/`n`/`never`); input exhaustion falls back to deny-once, never allow |
| `ui/cli/details.py` | `detail_lines(session, view)`: pure function of the reduced view; used by `/details` and the status line so they cannot disagree |
| `ui/cli/commands.py` | slash commands as data |
| `ui/cli/stream.py`, `ui/jsonl.py` | streaming helpers and JSONL passthrough (every envelope, unattended) |
| `ui_support/text.py` | control-safe text (nothing is redacted) |

Rendering rules: control characters are escaped before any tool name, key, error
or permission preview reaches the terminal; streamed assistant prose is
control-escaped with newlines/tabs kept. The terminal clients do not redact
credential-shaped text: the user sees what the agent sees (see
[decisions](decisions.md)). ANSI only on a TTY (`NO_COLOR`, `TERM=dumb` disable; `FORCE_COLOR`
forces). The `details` model/provider shown is the *effective* one
(`model.started`, or the durable `model.selected` until the next turn reports).

## Chat slash commands (`commands.SPECS`)

Declared once and used by the TUI palette, the CLI and the web client (same
names, usage, summaries, aliases). `parse` resolves aliases to the canonical name.

| Command | Usage | Aliases | Summary |
| --- | --- | --- | --- |
| `/new` | `[id]` | `/clear` | start a new session |
| `/sessions` | `[id]` | `/session` | list and switch |
| `/model` | `[list\|tier\|provider/model\|id]` | | list models or set this session's model |
| `/effort` | `[LEVEL]` | `/reasoning` | reasoning effort |
| `/agent` | `[list\|current\|reset\|NAME]` | | root agent |
| `/tools` | | | tools used in this transcript |
| `/details` | | | model, context, queue, approvals, subagents |
| `/context` | | | assembled prompt, tools, messages, accounting |
| `/reconnect` | | | reattach and replay missed events |
| `/cancel` | | | cancel the active turn |
| `/fork` | `[at_seq]` | | branch this session |
| `/export` | `[json\|markdown\|jsonl]` | | export |
| `/help`, `/hotkeys` | | | help, keyboard shortcuts |
| `/exit` | | `/quit` | leave |
| `/worktrees` | | | review and manage child worktrees |
| `/copy` | | | copy assembled context as JSON |
| `/diff` | `[--staged] [ref]` | | workspace Git diff |
| `/cost` | | | token usage and available cost estimate |
| `/usage` | | | plan usage and limits for connected providers (also `Ctrl+U`) |
| `/theme` | `[dark\|light]` | | switch theme |
| `/settings` | | | open Settings |
| `/verbose` | | | toggle full tool output previews |
| `/mcp`, `/skills`, `/tasks` | | | MCP servers, active skills, background agent tasks |
| `/reload` | | | reload extensions and MCP |
| `/review`, `/commit` | | | ask the agent to review / commit changes |
| `/archived` | | `/resume` | browse archived sessions |
| `/voice` | `[status\|download\|on\|off]` | | dictation |
| `/speak` | `[download]` | | speak the latest completed answer with local Kokoro; a missing model opens the consent and download dialog (`download` runs only that) |
| `/mock` | | | dev mode only (`DEV_SPECS`) |

Multiline input: a trailing backslash or an unclosed triple quote continues the
prompt (`is_continuation`).

## Adding a command

- **Slash command:** add a `CommandSpec` in `commands.py`; add a branch in
  `nexus/ui/ratatui/actions.py`. Existing browser behavior lives in
  the web client's `SLASH_COMMANDS` / `runSlash` ([web.md](web.md)). The palette
  reads `SPECS`.
- **Subcommand:** add the parser in `build_parser`, call the host command through
  `Client`, render with the shared pure helpers. Tests: `tests/test_cli.py`,
  `tests/test_chat_command_aliases.py`.

### Native terminal client

`nexus chat` launches the Rust/Ratatui client. `--renderer ratatui` is explicit;
`auto` is an alias. Missing native executables cause an actionable error.
Installed native wheels include `nexus-ratatui`; source checkouts build it with
`cargo build --locked --manifest-path rust/tui/Cargo.toml`.
`NEXUS_TUI_BINARY` overrides executable discovery. See [ratatui-parity.md](ratatui-parity.md).
