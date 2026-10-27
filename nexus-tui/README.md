# nexus-tui

The Ratatui terminal client for Nexus Core (Rust). It speaks only the Core HTTP
API (`/api/*` plus the `/api/event` stream, see `../docs/api.md`) and never
reads Nexus state files, SQLite or provider credentials. Old Nexus's client in
`~/repos/nexus/rust/tui` is reference.

**Status (PLAN.md Steps 12.3 and most of 12.4):** the keyboard-first shell is
usable against a real Core: workspace header, both sidebars, session view, waiting
card, composer, command palette, settings navigation and unified alerts, with an
explicit focus model, the manifest's Esc chain, mouse and keyboard parity, and
terminal hygiene.

The timeline is complete for one session: turn headers and footers, folding,
collapsed reasoning, Markdown-lite text, tool-specific expansion (diffs, output
caps, raw arguments/results), child cards, context notices, diagnostics, and the
frozen starting-context section. Selecting a session loads its full stored
history (all messages, the retained diagnostics that carry duration and usage,
and the snapshot) after the bounded snapshot view. Model, effort and agent
pickers, session export and a directory change are wired.

Still Step 12.4: the Files tab and built-in editor, Git status/diff, worktree
management in the UI, settings editing beyond theme/monochrome, and
steering/queueing (blocked on Core, Step 12a). Those commands are listed
disabled with their reason, never as a control that appears to work.

## Build, run, test

```sh
cd nexus-tui
cargo build
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --check

cargo run                  # the TUI (starts Core if needed)
cargo run -- --once        # no UI: print connection state + sessions once live, then exit
cargo run -- --dump-frame 80x24,120x36 --open first --out DIR
                           # no TTY: render the real shell against the real Core to text files
```

Display options (flags override the environment): `--dark | --light`, `--mono`
(also `NO_COLOR` or `TERM=dumb`), `--256 | --16` (otherwise `COLORTERM=truecolor`
means truecolor), `--ascii` (also `NEXUS10_ASCII=1` or a non-UTF-8 locale),
`--reduced-motion` (also `NEXUS10_REDUCED_MOTION=1`: spinners stay still and an
idle terminal is never redrawn).

Environment:

| Variable | Meaning |
| --- | --- |
| `NEXUS10_HOME` | Nexus home (default `~/.nexus10`). `~/.nexus` is refused. |
| `NEXUS_TUI_CORE_CMD` | Command that runs the Core CLI, default `nexus`. Used as `<cmd> daemon start`, for example `uv run --project ../nexus-core nexus`. |
| `NEXUS_TUI_NO_START` | If set, never start Core; report offline instead. |

Dependencies are pinned to exact versions in `Cargo.toml` (`=x.y.z`) and
`Cargo.lock` is checked in. Rust 1.85+.

Try it against a real isolated Core:

```sh
export NEXUS10_HOME=$(mktemp -d) NEXUS_PORT=4799
chmod 700 $NEXUS10_HOME          # Core requires a private home
NEXUS_TUI_CORE_CMD="uv run --project ../nexus-core nexus" cargo run
```

## Module layout

Lower layers know nothing of higher ones.

| Module | Role |
| --- | --- |
| `transport` | HTTP (bearer auth, `Idempotency-Key`, `If-Match`, error classification, keyed transport retries) and SSE (incremental parser, `Last-Event-ID`, `EventStream`), `Backoff`. No Nexus types. |
| `api` | Typed responses and the endpoints the TUI needs; `/api/info` and the API-version check (`check_compat`); cursors; SSE `Frame` decoding; `RequestId`, `PendingMutation`, `Outcome` (done vs revision conflict). |
| `reduce` | Pure `Model` (projects, sessions, running turns, waits, streaming text, selected session's messages and every event): `apply_snapshot`, `apply_event`, dedup by durable id, generation check. |
| `discovery` | `daemon.json` under the Nexus home, `/api/info` verification, `nexus daemon start` bootstrap, daemon log tail. |
| `sync` | The connection supervisor task: handshake, snapshot, SSE, reconnect with backoff, resync, restart detection. Emits `SyncMsg`, takes `Command`. |
| `state` | `ConnectionState`, `Banner`, `AppState` (model + connection + banners; `apply(SyncMsg)`). |
| `manifest`, `theme` | `interface/manifest.toml` and `interface/theme.toml` embedded with `include_str!` and parsed into typed structs: commands, keys, Esc chain, navigation, settings; colour tokens per mode and tier (truecolor, 256, ANSI-16, monochrome), glyph tables (Unicode/ASCII). No command list, key, reason, colour or glyph is copied by hand. |
| `text`, `keys`, `composer` | Display-width helpers (`unicode-width`), key names in the manifest's syntax, the multi-line composer buffer. |
| `ui`, `layout` | View-state types (focus, overlays, dialogs, alerts, mouse targets). `Regions::compute` is the one geometry function; the renderer registers every clickable rect in a `HitMap` as it draws it and the map is rebuilt each frame. |
| `app` | Focus model, `when`-term evaluation, command availability, the palette, the Esc chain, keyboard/mouse/paste handling. Produces `Effect`s (API call, supervisor command, clipboard, quit, suspend). |
| `actions` | The mutations the shell makes (prompt, new session, continue, interrupt, archive, rename, delete, allow/deny), each with its own `RequestId`; a failed send hands the draft back. |
| `timeline` | The session timeline: blocks in model order (starting context, turns, user/assistant text, reasoning, tools and their diffs, notices, diagnostics, child cards) with fold state and the list of selectable blocks. Folding is view state only. |
| `render` | Ratatui drawing from the model, theme and manifest. Timeline blocks become hit targets as they are drawn. `render_text` renders to a text grid for tests and frame dumps. |
| `term` | Raw mode, alternate screen, mouse, bracketed paste, kitty keyboard flags; restored on error, panic (hook) and Ctrl+Z suspend/resume. |

Tests (111): unit tests live in the modules; `tests/timeline.rs` covers turn
grouping and footers, folding (including "all but last"), tool diffs and the raw
toggle, history merging, and keyboard/mouse block selection; `tests/sync.rs` runs the supervisor
against a scripted loopback mock Core (live, reconnect, cursor rejection,
overflow, restart, auth failure, API major/minor, offline, selection);
`tests/mutations.rs` covers keyed retry, replay and conflicts. Shared fixtures
are in `fixtures/` (see `fixtures/README.md`): captures from a real daemon and
shapes from the docs.

## Connection contract (as implemented)

- **Discovery.** Read `<home>/daemon.json` (`url`, `pid`, `token`), trust it only
  after `GET /api/info` answers with that token. If absent or stale, run
  `nexus daemon start` once per outage, then rediscover.
- **Handshake.** `/api/info` `api_version` (`"1.2"`, `"1"` or `{major, minor}`):
  same major goes; newer Core minor gives a banner; a different major halts in
  `Incompatible` naming which side to update, and no other request is sent until
  `daemon.json` changes. Core reports `{major, minor}`; `Unreported` remains a
  fallback for older daemons.
- **Snapshot, then stream.** `GET /api/snapshot` (with the selected session's
  bounded messages), then `GET /api/event` with `Last-Event-ID: <head_cursor>`.
  The cursor advances only after an event reached the app; reconnect resumes from
  it, dropping events at or below the applied id. A rejected cursor
  (`invalid_cursor`, `cursor_generation`, `cursor_ahead`, `history_unavailable`),
  `bus.overflow` or an undecodable event refetches the snapshot and resumes after
  its watermark. Gaps in ids are never treated as loss.
- **Restart.** Every reconnect rereads `daemon.json` and `/api/info`; a different
  pid or token forces a full snapshot (`ResyncCause::DaemonRestarted`).
- **States.** `Starting`, `Connecting`, `Live`, `Reconnecting`, `Offline`,
  `AuthFailed` (401; waits for a new token), `Incompatible`. Anything but `Live`
  is read-only: the last model stays visible.
- **Mutations.** One `RequestId` (an `Idempotency-Key`) per user action, reused
  for transport retries only (three attempts, same key; HTTP errors are never
  retried). A stale revision (`If-Match`) is `Outcome::Conflict { current_revision }`,
  not an error: re-read, ask the user, retry with a new key. `PendingMutation::retry_safe`
  checks the server's deduplication lifetime before retrying a lost response.

## The shell

Anchors (INTERFACE_PLAN.md): left sidebar toggle at the header's leading edge,
title and breadcrumbs centred, notification count and right toggle trailing;
the toggles stay when a sidebar is closed. Left tabs Sessions, Files, Projects
with Settings fixed at the bottom; right sidebar Session info and Git status
(shown with its manifest reason). Below 100 columns the sidebars become drawers
opened from the same toggles. The waiting card sits directly above the composer.

Keys come from the manifest (`F1` lists them; the palette shows each command's
key). Beyond the manifest: `F6`/`Shift+F6` cycle focus, `Ctrl+J` inserts a
newline where `Shift+Enter` is not reported, `Ctrl+C` quits, `Ctrl+Z` suspends.
`/name` in the composer runs the command with that slash name (`/rename New
title`, `/continue`, `/new`, `/settings`...); an unknown `/word` is sent as text.

In the timeline, Up/Down (or j/k) move between blocks, Enter or Space opens or
closes the selected block (a child card opens the child), Left/Right close or
open it, PageUp/PageDown scroll, and `[`/`]` jump between turns. F1 lists the
manifest keys (`alt+z` fold all but last, `r` raw, `y` copy, `i` inspector).
An export is written to `<Nexus home>/exports/<session>-<scope>-<utc>.<md|json>`
and the success toast names the path.

**Esc** walks the manifest's chain once, top to bottom, and stops at the first
rule that holds: overlay, dialog, inline edit, narrow drawer, focused alert,
interrupt a running turn, (editor), back from a child, leave settings, refocus
the composer. Esc never clears composer text. Overlays record the focus and
scroll they replaced and give them back.

Read-only: anything but a live connection disables every mutating command with
"Read-only until Core is connected again"; the last view stays visible and the
draft is kept. A different API major shows a blocking dialog and nothing else
reacts except `Ctrl+Q`.

## Visual test harness

The real binary in a real PTY, shown by xterm.js with Monaspace Argon and
truecolor, photographed by headless Chromium. Needs Node 22, `uv` and the
built binary; nothing calls a provider.

```sh
cd nexus-tui && cargo build
cd tools/webterm && npm ci                    # pinned: xterm 5.5.0, node-pty 1.1.0, playwright 1.64.1
npx playwright install chromium               # once
ROOT=/tmp/nexus-shots
./seed.sh $ROOT                               # two isolated Cores (mode 700, ports 4799/4798): seeded and empty
node shots.mjs --env $ROOT/env --out $ROOT/png            # all shots (add --only palette to filter)
./seed.sh $ROOT stop
```

`shots.mjs` captures 80x24, 120x36 and 160x48 in dark and light, monochrome,
ASCII and 256-colour variants, and the states: empty workspace, session with
messages, palette (plain, filtered, light, narrow), settings, confirm dialog,
toast, multi-line composer, drawers, closed sidebars, Projects and Files tabs,
and the offline banner (it stops the seeded daemon under a live TUI). Each PNG
has a `.txt` of the screen beside it. `npm run serve` serves one profile so the
TUI can be driven by hand in a browser.
