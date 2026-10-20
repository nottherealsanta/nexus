# CLI, one-shot runs and chat commands

`nexus/cli.py` is the canonical terminal entry (`nexus = nexus.cli:main`). It is
a **pure client**: every subcommand except a few local ones (`init`, `auth`,
`claude`, `update`, `uninstall`, `searchserver`, `daemon`) is a host command over the daemon's
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
| `live [--session ID] [--voice V] [--new] [--workspace PATH]` | headless GPT Live voice call (experimental): asks for consent on the terminal the first time, prints captions, delegations and appends labelled, Ctrl+C or Ctrl+D hangs up ([live.md](live.md#headless-cli-nexus-live)) |
| `replay SESSION [--json]` / `sessions replay` | re-render a session from its log through the reducer |
| `sessions list\|show\|fork\|replay\|export\|delete\|restore` | session management (`show <name\|id>` prints name, id, title, kind, state, project; `fork --at-seq`, `export --format json\|markdown\|jsonl`, `delete --force`) |
| `models list\|show\|refresh\|tiers\|select` | registry inspection (`list --provider --tier --selectable --search`); `select --session` |
| `agents list\|select\|current\|reset` | subagent definitions and the session's root agent |
| `tools list` | the model-facing tool catalogue for the current config |
| `ext list\|reload\|validate\|trash` | extension inspection, rebuild, quarantine check, safe trash |
| `worktrees list\|inspect\|review\|acknowledge\|integrate\|discard` | child worktree review flow ([agents.md](agents.md#worktrees)) |
| `voice status\|init\|download\|remove\|transcribe` | local dictation; `init` installs the runtime when missing, then downloads the model ([voice.md](voice.md)) |
| `doctor [--json] [--explain-reload]` | config, providers, registry, extensions, MCP; no model request |
| `daemon status\|stop\|restart\|logs` / `restart` | workspace daemon management |
| `init` | create `nexus.toml`, `SOUL.md`, `MEMORY.md` without overwriting |
| `auth codex login\|status\|logout` | local ChatGPT OAuth (no daemon RPC) |
| `claude init` | set up the Claude Agent SDK provider |
| `update [--channel stable\|git --ref --version --no-restart --no-voice --no-speak]` | upgrade Nexus and restart running daemons ([release.md](release.md)) |
| `uninstall [--yes]` | remove Nexus and all its data after listing it and asking ([release.md](release.md#uninstall)) |
| `searchserver start` | start the local SearXNG Docker service |
| `mock list\|run\|all\|clean` | dev mode scenarios |

## Layout

| Path | Role |
| --- | --- |
| `client/protocol.py` | `Client`: typed, transport-neutral methods over the host protocol (one per command), `Transport` protocol, `handshake` |
| `client/turn_stream.py` | `turn_events`: attach to a turn's stream before starting it, so no event is missed |
| `ui/cli/uds.py` | UDS transport for the CLI |
| `ui/cli/run.py` | one-shot `nexus run` |
| `ui/cli/live.py` | `nexus live`: consent, the call, labelled output, hang-up ([live.md](live.md)) |
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
| `/attach` | `<path> \| clear` | | attach an image or document |
| `/new` | `[id]` | `/clear` | start a new session |
| `/open` | `<name\|id>` | | switch to a session by its immutable name (e.g. `quiet-cobalt-otter`) or id; trashed or unknown refs only show a notice |
| `/sessions` | `[id]` | `/session` | switch to `[id]`, or (native TUI) open Sessions, the same surface as `Ctrl+B` |
| `/model` | `[list\|tier\|provider/model\|id]` | | list models or set this session's model |
| `/effort` | `[LEVEL]` | `/reasoning` | reasoning effort |
| `/agent` | `[list\|current\|reset\|NAME]` | | root agent |
| `/tools` | | | tools used in this transcript |
| `/details` | | | model, context, queue, approvals, subagents |
| `/context` | | | assembled prompt, tools, messages, accounting |
| `/reconnect` | | | reattach and replay missed events |
| `/restart` | | | restart the workspace daemon and reconnect (native TUI; see below) |
| `/cancel` | | | cancel the active turn |
| `/close` | | | close the current tab (native TUI only; same action as the tab close button) |
| `/fork` | `[at_seq]` | | branch this session |
| `/export` | `[json\|markdown\|jsonl]` | | export |
| `/help`, `/hotkeys` | | | help; `/hotkeys` opens Settings → Keyboard (native TUI) |
| `/notifications` | | | open the recent-notifications drawer (native TUI only; the history is client-side, also `Ctrl+X Shift+N` or a click on the top-bar `◔ N`) |
| `/exit` | | `/quit` | leave |
| `/worktrees` | | | review and manage child worktrees |
| `/copy` | | | copy assembled context as JSON |
| `/diff` | `[--staged] [ref]` | | workspace Git diff |
| `/cost` | | | token usage and available cost estimate |
| `/usage` | | | plan usage and limits for connected providers (also `Ctrl+U`) |
| `/theme` | `[dark\|light]` | | switch theme |
| `/settings` | | | open Settings |
| `/verbose` | | | toggle full tool output previews |
| `/mcp`, `/skills`, `/tasks` | | | MCP servers (with each server's tool loading mode in the Ratatui dialog), active skills, background agent tasks |
| `/reload` | | | reload extensions and MCP |
| `/review`, `/commit` | | | ask the agent to review / commit changes |
| `/archived` | | `/resume` | browse archived sessions |
| `/voice` | `[status\|download\|on\|off]` | | dictation |
| `/live` | `[stop\|pause\|resume\|mute\|new\|status\|revoke]` | | spoken GPT Live call on this session, again to hang up (`Ctrl+X D`); `pause` toggles pause (`Ctrl+X P`, bounded by `[live].pause_seconds`, [live.md](live.md#pause-vs-stop)); first use asks for consent ([live.md](live.md#client-flow-ratatui-m4)) |
| `/speak` | `[download]` | | speak the latest completed answer with local Paradee (ONNX, CPU); a missing model opens the consent and download dialog (`download` runs only that) |
| `/mock` | | | dev mode only (`DEV_SPECS`) |

Multiline input: a trailing backslash or an unclosed triple quote continues the
prompt (`is_continuation`).

In the native TUI, `/close` takes no arguments and does not exit, delete or
archive a session, or cancel its active turn. It uses the tab close button's
existing policy: with multiple tabs, remove the current tab and select the tab
at the same position (or the previous tab if it was last). With just one tab,
start a new session via `/new` instead; the original session tab is retained.
Running sessions may appear again through the existing background-tab discovery.
Use `/cancel` explicitly to stop a turn, or `/exit` (`/quit`) to leave chat.

In the native TUI, `/restart` takes no arguments. The client stops the workspace
daemon (`nexus.host.daemon.stop`, whose refusal is an error), waits up to 10 s for
the old process to exit (`daemon.wait_stopped`: the recorded pid, or the daemon lock
when none was running — `status` is not trusted, it reads "not running" while the
old process still holds the lock), starts a fresh one through the normal auto-start
connect, reattaches the same session and replays it, like `/reconnect`. The live
subscription and its auto-reconnect are paused meanwhile so no second client is
opened, and resume afterwards (also on failure). The daemon is never asked to
restart itself, so this is the
same lifecycle as `nexus daemon restart`. Turns running when it is issued end with
the old process (durable transcript state replays; it is not resumed). It picks up
changed core Nexus source and provider connections that need a daemon restart. If
the daemon will not stop, or the workspace is unknown, the command reports an error
and the client stays on the old connection (use `nexus daemon stop` and relaunch).
Other chat clients do not implement `/restart` yet; use `nexus daemon restart`.

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

Keys on the waiting messages above the composer (Tab on an empty draft with no focusable
transcript block, or Down from the last transcript block, focuses the list): Up/Down move;
**Enter or `s`** promote (queued to steer, steer to interrupt); **Delete, Backspace or `x`**
remove (a removed steering message returns, with its attachments, to an empty draft);
**Alt+Up / Alt+Down** reorder queued rows; **Esc** leaves. The same controls show on the focused
row as on the hovered one.

### Manager entry (Plan 02)

`nexus manager` (or `nexus manager open`) opens the pinned workspace Manager in
ratatui; `nexus manager status` is the non-interactive status command. These use
HostClient ManagerOpen/ManagerState, not session storage. Native session pickers
pin Manager above ordinary sessions: press `m` in the terminal picker to jump
back, or select Manager in the desktop picker. While it is open the right-hand `◆` toggle is pressed and the left title, workspace path and git branch are hidden (presentation only).
Ask “Start a session to do X” to launch through a tool call that needs no confirmation;
ask “What finished in the last two hours?” to find sessions, then use `/sessions`
(or the desktop session picker) to open a result and return to Manager. Entry
notes show launch/find guidance and provider/voice readiness. Live real-provider
verification remains outstanding; offline UI tests do not establish it.

### Immutable session names

Saved sessions receive an immutable ASCII adjective-color-animal name, globally unique across projects and root and child namespaces. Names survive title changes, archive, and trash; purged names are never reused. Drafts remain unnamed until persistence. Exact ids take precedence over exact, case-sensitive names; prefixes and Unicode lookalikes are not normalized. `nexus sessions show <name-or-id>` resolves without opening a handle. The host `SessionResolve` command accepts `ref` and optional `namespace`, returning canonical identity and lifecycle metadata; resolution does not grant access or revive deleted sessions. There is no rename operation.

Saved immutable session names are accepted anywhere a session target is accepted,
including `sessions show` and open; exact IDs win. Completion lists saved names,
not retired tombstones or unsaved drafts. Names are case-sensitive and cannot be
renamed; titles remain independently editable.
