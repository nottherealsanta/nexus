# Development

## Repo layout

```
PLAN.md           remaining work; docs/porting-ledger.md records porting decisions
AGENTS.md         instructions for agents; points to docs/
docs/             this documentation
nexus-core/       Python (uv): everything that is not a UI
nexus-tui/        Rust Ratatui client (its README has build, run and test steps)
nexus-nsh/        NSH web/desktop client (TSX; its README has build and test steps)
interface/        shared command manifest, theme and capability matrix for both clients
scripts/nexus     development launcher (tui, web, build, restart, checks)
```

The clients speak only the HTTP API ([api.md](api.md)). They never read SQLite,
provider credentials or Nexus state files; only the local bootstrap discovers and
starts the daemon. Both build from `interface/manifest.toml` and
`interface/theme.toml`; check edits with `python3 interface/check_manifest.py` and
`python3 interface/check_theme.py`.

Inside `nexus-core/` (a uv project: `pyproject.toml`, `uv.lock`, `.python-version`):

```
nexus/
  llmhub/            types, sse, responses, chat, providers, catalog, hub
  llmhub/auth/       store, device, codex, copilot, grok, opencode, errors
  tools/             base, read, bash, edit, write, ask, code
  observations.py    per-session read observations and the read-before-edit guard
  workspace.py       control tools: expose, session-directory moves, worktree specs
  worktrees.py       Git worktree list/create/delete through argument arrays
  questions.py       the ask contract: schema, bounds, validation, result text
  snapshot.py        serializable session configuration and instruction-file records
  agents.py          agent files: parse, load (builtin, global, project)
  builtin_agents/    orchestrate, build, quick, scout, plan (.md) + NOTICE
  loop.py            the agent loop
  codec.py           JSON encoding of messages and events
  config.py          TOML config, schema, revision-checked patches and MCP import
  paths.py           home(): $NEXUS10_HOME or ~/.nexus (marker-checked)
  migrate.py         `nexus migrate`: old ~/.nexus aside, ~/.nexus10 in
  export.py          read-only session/tree exports: Markdown and JSON
  store.py           SQLite: projects, sessions, messages, observations, turn lifecycle, mutation journal, durable event log (~/.nexus/nexus.db)
  bus.py             in-process event bus; persists before publishing, bounded per subscriber
  eventlog.py        log generation, cursors, session filtering, atomic replay-to-live stream
  retention.py       bounded event/lifecycle retention and the retention floor
  retry.py           bounded, visible provider-request retry policy
  recovery.py        startup reconciliation of turns a crash left unfinished
  continuation.py    explicit manual continuation and provider-valid context reconciliation
  kernels.py         daemon-owned persistent Monty kernels
  bash_jobs.py       daemon-owned background bash jobs
  runtime.py         projects, sessions, turns, approvals, questions, workspace controls, interrupts
  server.py          the HTTP API (Starlette) — docs/api.md
  daemon.py          serve, start, stop, discovery via daemon.json — docs/daemon.md
  inspector/         index.html: the session inspector page — docs/inspector.md
  client.py          HTTP client for the API, used by the CLI
  cli.py             local: login | logout | status | models | migrate | serve | daemon
                     daemon clients: project | session | workspace | run | chat
tests/               offline tests (test_server.py drives the API with a scripted provider)
```

State lives in `~/.nexus` (`$NEXUS10_HOME`), once it is migrated (below). Use a scratch `$NEXUS10_HOME` for tests and experiments.

### Migrating to `~/.nexus`

Before 0.10.0, 0.10 builds kept state in `~/.nexus10` and old Nexus owned `~/.nexus`. A home is
Nexus 0.10's when it holds the `.nexus10-home` marker file. Until `~/.nexus` has one, Core,
the TUI, the NSH host and the launcher refuse to use it (and a pre-release `~/.nexus10` is not
used silently either); each says to run `nexus migrate`. That command:

1. Stops if a daemon is running from either directory (its `daemon.json` pid is alive).
2. Prints what it will do and asks, or proceeds with `--yes`; with no terminal it refuses without `--yes`.
3. Renames old Nexus's `~/.nexus` to `~/.nexus.old-<YYYYmmdd-HHMMSS>` (never deleted) and
   `~/.nexus10` to `~/.nexus`. Both are renames in `$HOME`, so nothing is copied.

The marker is written into `~/.nexus10` before the rename, so a crash never leaves a 0.10 home that
looks like old Nexus: either run `nexus migrate` again, or it is done. If the second rename fails,
old Nexus is put back. A machine with neither directory gets a fresh marked `~/.nexus` on first
use. A `$NEXUS10_HOME` elsewhere is used as given, with no marker.

## Shortcuts

In the TUI composer, Ctrl or Alt plus Left/Right moves by word; adding Shift
selects. Cmd plus Left/Right moves to the line edge, with Shift selecting;
Home/End and Shift+Home/End also work. Cmd shortcuts require a terminal that
forwards Super-modified arrow keys to applications.

The TUI wraps interactive redraws in synchronized-update sequences so supporting
terminals show each completed frame instead of intermediate cursor movements.
Terminals without support ignore the sequences and draw normally.

In the TUI timeline, drag over visible text to select it; releasing the mouse
copies the selected screen cells via OSC 52. Selection is limited to the
visible timeline, includes its displayed formatting and line breaks, and does
not auto-scroll. The terminal must allow OSC 52 clipboard writes. A normal
click still activates timeline controls; scrolling cancels an in-progress drag.

`scripts/nexus` is a development launcher for the whole repo. Install it once:

```sh
ln -sfn ~/repos/nexus10/scripts/nexus ~/.local/bin/nexus
```

Then, from any directory:

| Command | Does |
| --- | --- |
| `nexus` / `nexus tui` | Launch the TUI (starts Core with the repo's `uv run nexus`); runs the release binary (`--debug` for a debug build; debug redraws are about 10x slower). The TUI opens the directory you launched from as a project and shows its new-session screen (`NEXUS_TUI_LAUNCH_DIR`, set by the launcher) |
| `nexus build` | `uv sync`, release TUI build (`--debug` for a debug build), NSH web build |
| `nexus web` | Build the web app if needed, start Core, serve it on 4820 and open it (`--no-open`, `--port N`) |
| `nexus restart` | Stop and start the Core daemon |
| `nexus stop` | Stop the Core daemon |
| `nexus status` | Core daemon and provider status |
| `nexus ps` | Core daemon status only |
| `nexus logs` | Follow `$NEXUS10_HOME/logs/daemon.log` |
| `nexus test` | Core and TUI test suites |
| `nexus check` | Tests, lints, shared theme/manifest checks, NSH checks |
| `nexus fmt` | `cargo fmt` and regenerate the shared NSH files |
| `nexus doctor` | Versions and paths, then Core's `nexus doctor` rows |
| `nexus update` | Compares with the latest GitHub release; reports only |
| `./install.sh` | Installs the `nexus` uv tool, `nexus-tui` and the NSH bundle under `$PREFIX` (default `~/.local`); `--uninstall` removes them and never touches `$NEXUS10_HOME` |
| `nexus core <args>` | Core CLI explicitly |

Anything not listed is passed to the Core CLI, so `nexus login codex`,
`nexus chat`, `nexus session list` and similar commands work unchanged. The
launcher honours `NEXUS10_HOME`; it refuses an unmigrated `~/.nexus`.

Tuning (all optional, read at daemon start):

| Variable | Default | Meaning |
| --- | --- | --- |
| `NEXUS_RETRY_MAX_ATTEMPTS` | 3 | Total provider attempts per step, including the first |
| `NEXUS_RETRY_BASE_DELAY` | 1.0 | Base backoff seconds |
| `NEXUS_RETRY_MAX_WAIT` | 30.0 | Longest single backoff |
| `NEXUS_RETRY_MAX_ELAPSED` | 60.0 | Retry budget per step |
| `NEXUS10_EVENT_RETENTION_DAYS` | 14 | Diagnostic event age |
| `NEXUS10_EVENT_RETENTION_BYTES` | 268435456 | Retained event payload budget (256 MiB) |
| `NEXUS10_RETENTION_INTERVAL_SECONDS` | 3600 | Maintenance cadence |
| `NEXUS10_TURN_RETENTION_DAYS` | 30 | Terminal turn lifecycle age |
| `NEXUS10_KEEP_TURNS` | 200 | Recent turns kept per session |
| `NEXUS10_MUTATION_RETENTION_DAYS` | 7 | Idempotency-key lifetime after resolution |

## Terminal program status

The interactive TUI reports active sessions to supporting terminals using
[OSC 7501](https://www.superlogical.com/rex/docs/build/program-status). Running
sessions report `working`; pending approvals and questions report `blocked`
with the corresponding kind. A live turn summary reports `done`, `error`, or
`idle` for a completed, failed, or interrupted turn. Session titles are included;
messages are generic and do not contain prompts or tool arguments. Each session
uses its own `nexus/<session-id>` record. Unrecognized OSC sequences can be
ignored by the terminal, and the existing notification setting is independent.

The TUI clears its transient records on exit, suspension, external-editor
handoff, or loss of connection; completed and failed records survive normal
exit. After reconnect, the TUI rebuilds active records from the snapshot but
does not reconstruct past outcomes, which the snapshot does not include. It
also removes records for deleted or archived sessions. Reports are best effort;
terminal support has not been verified in a live terminal.

## Visual review of the TUI

The Sessions sidebar's scope control shows its current project/all-projects
scope on hover; clicking it switches scope. Project names in Sessions and Files
retain their original casing.

`nexus-tui --dump-frame` renders the real shell against a real Core with no TTY.
Besides text frames it can press keys through the app's own key handling and
write colour SVGs:

```sh
nexus-tui --dark --dump-frame 120x36 --out DIR --name palette --svg \
  --keys "ctrl+k Refactor enter ctrl+p"
```

`--keys` takes space-separated tokens: key names in the manifest syntax
(`ctrl+p`, `alt+2`, `ctrl+,`, `esc`, `enter`, `down`, `space`, `f1`) or text typed
character by character (`/mo`). A frame is drawn before each token, so scrolling
and other keys that need the last layout work, and Core's replies are applied between tokens.
A seeded Core has no pending approval, so the waiting card and alert shots come from
fixtures: `I8_SHOTS=DIR cargo test --test alerts -- --ignored shots` writes their SVGs.
`--svg` writes `<name>.svg` beside `<name>.txt` (truecolor unless `--256`/`--16`).

`nexus-tui/tools/shots.sh <root> <out>` runs a standard set (session at three
sizes in dark and light, palette, settings, files, new session, detail high,
delete confirmation, slash completion) against two seeded, isolated Cores and
converts the SVGs to PNG with headless Chromium, with no node. The seeded
session ends with a two-file `patch` edit (CJK comment, a long line), so detail
high shows a split diff at 200 columns and a unified one at 120 and below. The seed
also gives the right sidebar its content: `nexus-demo` is a Git repo with uncommitted
changes (modified, untracked and deleted files, so GIT lists them and the diff view
has rows); the session has a `usage` event (CONTEXT shows the meter and the share of
the window); and its `session` tool call launched a sub agent (the `↳` count).
The composer's `╱` meter and `48.2K · 5%` show because the seed writes a placeholder
codex sign-in (the Core lists only connected providers' models, and the window comes
from that list) and ends the seeded turn on an assistant reply (otherwise the composer
shows `continue`). It always stops
the Cores. `NEXUS_CMD` picks the Core CLI (default `uv run nexus`; for example
`NEXUS_CMD=/tmp/nexus10-venv/bin/nexus`), `CHROME` the browser (default the
Playwright headless shell). `NEXUS_PORT` is the seeded Core's port (default 4799)
and `NEXUS_PORT_EMPTY` the empty Core's (default 4798); set both when those ports
are held by another daemon, for example
`NEXUS_PORT=5298 NEXUS_PORT_EMPTY=5297 tools/shots.sh ROOT OUT`. The ports must
differ. The Cores it starts keep their homes under `ROOT` and are stopped on exit. The node harness in `nexus-tui/tools/webterm`
(xterm.js, mouse and hover shots) remains for what keys cannot reach.

## Commands

Run uv from `nexus-core/`. Use uv only: `uv add <pkg>`, never `pip install`.
Python is pinned in `.python-version` (>= 3.13).

```sh
cd nexus-core
uv sync                        # venv, deps and the dev group (pytest, pytest-asyncio, ruff)
uv run pytest -q               # the offline suite
uv run pytest -q tests/test_tools.py -k name   # one area
uv run ruff check nexus tests  # lint; line length 140
uv run nexus status            # which providers are connected
uv run nexus login codex       # or: login codex --device
uv run nexus models --refresh
uv run nexus chat --wire       # see every request and SSE event
uv run nexus run "prompt" --model grok/grok-4.7 --yes --workspace .
```

CLI flags for `chat` and `run`: `--model provider/model`, `--effort`,
`--session ID`, `-c/--continue`, `--workspace` (default `.`), `--wire`,
`--wire-file F.jsonl`, `--yes`, `--disable bash,write` (empty string clears switches). Model, effort and `--yes` (permission `allow`)
are saved on the session. After changing core code, run `nexus daemon stop` so the
next command starts a daemon with the new code. With
`--wire` the CLI prints the redacted request and every SSE event; `--wire-file`
appends the same wire events as JSONL. `/context` in `chat` prints the next
request body; `/exit` and `/quit` leave.

`nexus session continue ID` resumes from a session's stored context without
adding a user message. It subscribes to the session's event stream before
posting, renders the turn (including questions and approvals), and accepts
`--wire` and `--wire-file`. `/continue` does the same inside `chat`. See
[api.md](api.md#manual-continuation).

## Tests

`tests/` needs no network or credentials. `pytest-asyncio` runs in `auto` mode
(`asyncio_mode = "auto"`), so async tests need no marker.

| File | Covers |
| --- | --- |
| `test_responses.py` | Responses dialect: request body, replay rules, event parsing |
| `test_chat.py` | Chat dialect: request body, reasoning fields, event parsing |
| `test_catalog.py` | models.dev filtering and `ModelInfo` |
| `test_tools.py` | read, bash, edit, patch, write |
| `test_ask.py` | ask contract and validation, pending/reply API, cancellation, snapshots, nested code questions |
| `test_store_version.py` | schema version gate: new, current, earlier, unreadable, empty and newer files; a backup per replacement; a failed build or swap leaves the old database in place |
| `test_eventlog.py` | durable ids and generations, cursors and resync errors, replay/live boundary and paging, filtering and tombstones, overflow, persistence failure, HTTP framing via raw ASGI |
| `test_recovery.py` | crash windows (accepted turn, interrupted stream, saved/started/finished/inserted tools, mixed batches, waits, code/kernels/jobs), parent/child and archive recovery, recovery interrupted at every step, repeated startup, durable replay, no provider/tool calls, isolated subprocess restart |
| `test_continuation.py` | manual continuation: exact first request with no synthetic user message, provider failure/interrupt/recovery, completed and unknown tools, new tool calls, settings and directory, busy/archived/empty/unknown errors, parent/child independence, provenance and crash-during-continuation recovery, reconciliation idempotency, CLI slash/standalone and subscription ordering |
| `test_mutations.py` | keyed acceptance/replay, duplicate requests, crash-before-journal unknown outcomes, deleted targets and revision preconditions |
| `test_retry.py` | pre-stream retry policy, attempt records, backoff cancellation, frozen requests and no tool replay |
| `test_client_snapshot.py` | consistent snapshots, reconnect cursors, overflow/resync, rotated local tokens and CLI tree isolation |
| `test_retention.py` | event age/byte limits, bounded deletion, floor/head persistence, replay pins, protected lifecycle evidence and export boundaries |
| `test_loop.py` | The loop with a scripted provider |

Loop tests use a small scripted provider that replays one reply per request.
`test_catalog.py` also covers `split_ref`. The table is from the test names and
file headers; the tests were not read in full.

## Style

* Module docstrings state the contract and cite the PLAN.md step.
* Types are frozen dataclasses with `slots`.
* Everything is bounded (sizes, counts, timeouts); clipping is announced.
* Errors never contain credentials.
* Layering: `llmhub` does not import `tools`, `loop` or `cli`. Adapters take header
  sources and never read credentials. Task tools never change Nexus state.

## Docs travel with code

A behavior or design change updates the matching page in `docs/` in the same
change. A step that lands or a decision that changes also updates PLAN.md (and
a ledger row for deliberate changes from old Nexus). Write the contract and the
reason, briefly, in the present tense; say "not verified" where it is not. A
new module gets a mention in the doc for its area and in the layout above. Index
new pages in [README.md](README.md).
