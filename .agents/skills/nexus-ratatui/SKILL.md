---
name: nexus-ratatui
description: Work on the native Ratatui terminal client for Nexus (rust/tui/, nexus/ui/ratatui/, shared ui_support helpers). Use when changing how `nexus chat --renderer ratatui` looks or behaves, adding a chat command/shortcut/panel to the native shell, improving terminal behavior, debugging the Python/Rust bridge, or verifying native changes with Rust tests, the PTY check and Playwright screenshots.
---

# Working on the Nexus Ratatui client

Ratatui is the sole `nexus chat` renderer. Read `AGENTS.md`, `docs/README.md`,
`docs/ratatui-parity.md` and `docs/surfaces.md` for current contracts.
The native Python bridge and Rust renderer must expose every parameter and output
through the host contract. Update matching docs with behavior changes.

## The one-minute architecture

```
host daemon  <->  Python (nexus/ui/ratatui/)  --JSONL snapshots-->  Rust (rust/tui/)
                  reducer, commands, workflows  <--typed actions---  keys, mouse, drawing
```

- **Python owns state.** It talks to the daemon through the normal client, reduces
  events with `nexus/view`, runs every host command, and *projects* a presentation
  snapshot (`prototype.py: project()`).
- **Rust owns the terminal.** It reads one JSON snapshot per line on stdin, draws
  with Ratatui to stderr, reads keys/mouse from `/dev/tty`, and writes typed actions
  as JSON lines on stdout. It never reduces domain state or calls the host.
- Python supplies labelled, redacted content, ordering, gaps and colour tokens.
  Rust owns disclosure, wrapping, painting and optimistic presentation toggles;
  it never reduces domain events. Static command completion stays local.
- Details: [references/architecture.md](references/architecture.md).

## Where things live

| Concern | Files |
| --- | --- |
| Snapshot projection, run loop, polling | `nexus/ui/ratatui/prototype.py` |
| Slash commands, attachments, notices | `nexus/ui/ratatui/actions.py` |
| Menus, settings, providers, worktrees, forms | `nexus/ui/ratatui/workflows.py` |
| Voice, logs, preferences, desktop clipboard | `voice.py`, `logs.py`, `preferences.py`, `desktop.py` |
| Key/mouse loop, action sending | `rust/tui/src/main.rs` |
| Layout, palette, top bar, sidebars, composer, dialogs | `rust/tui/src/render.rs` |
| Transcript blocks -> wrapped, styled rows | `rust/tui/src/transcript.rs` |
| Markdown, editor, snapshot structs | `markdown.rs`, `editor.rs`, `bridge.rs` |
| Pure presentation helpers | `nexus/ui_support/{timeline,details,context_header,completion,model_choice,shortcuts,session_groups,voice_settings,session_controller}.py` |

## Workflow for any change

1. **Find the current contract.** Read the matching docs and native tests.
   Preserve labels, ordering, errors and shortcuts unless the requested change
   calls for a new behavior.
2. **Keep pure logic in `ui_support/`.** Python clients use host commands and
   canonical reduced views. Widget toolkits stay outside the Python bridge;
   Rich remains only in shared context Markdown rendering.
3. **New data or action for the UI?** Add a host command (`nexus/host/protocol.py`
   -> `facade.py`) rather than reading files; see AGENTS.md rule 4.
4. **Project it** in `prototype.py` (control-safe: pass text through
   `redact(escape_controls(...))`). Add fields to `bridge.rs` structs with
   `#[serde(default)]` so older/partial snapshots still parse.
5. **Draw it** in `render.rs` / `transcript.rs` using palette tokens only
   (`Palette` in `rust/tui/src/render.rs`). No hard-coded colours.
6. **Handle input** in `main.rs`. Keep disclosure local; send host/persistence
   actions and handle them in `prototype.py`
   (action dispatch) or `workflows.operate`.
7. **Test** (see below), **screenshot** it, **update docs** (`ratatui-parity.md`,
   `module-map.md` for new Python modules).

## Rules that keep biting

- **Show everything, labelled.** No raw JSON dumps; clipping must be announced.
  Use `labelled()` / `tool_details.flatten`. Never hide what the agent can see.
- **Control-safe text.** Everything from the daemon is escaped and redacted in
  Python before it reaches Rust.
- **Bounded.** Sizes, counts, timeouts. Caches are capped (see `turn_cache`,
  `Cache.parts`).
- **Stale results.** Async results carry a session/generation guard; ignore them if
  the session or panel changed (`shell.generation`).
- **Snapshots are deduplicated and coalesced.** Identical snapshots are not resent;
  Rust skips queued backlog except snapshots with one-shot fields (`restore`,
  `insert`). If you add another one-shot effect to the snapshot, teach both sides
  (the string check in `main.rs` and `one_shot` in `prototype.py`).
- **Turn blocks are cached by turn identity.** If projected output depends on
  something besides the turn (verbose flag, expanded set, `view.agents`), add it to
  the cache `flags` in `project()`.
- **Margins collapse in Python.** Python computes `gap` (blank rows before a
  block) with `max(prev_bottom, top)`; do not add blank rows in Rust.
- **Wide characters and graphemes** stay whole; wrap with `transcript::wrap`.
- **Web mirrors the TUI.** A wording/feature change in one surface goes in the other
  (`docs/web.md`); new web features are opt-in.

## Commands

```sh
cargo build --locked --manifest-path rust/tui/Cargo.toml        # debug binary (needed by PTY test)
cargo test  --locked --manifest-path rust/tui/Cargo.toml        # Rust unit tests
env -u FORCE_COLOR PYTHONPATH=. .venv/bin/python -m pytest -q -p no:cacheprovider tests -k ratatui
ruff check nexus tests
env -u FORCE_COLOR PYTHONPATH=. .venv/bin/python tests/playwright_ratatui_check.py   # screenshots
PYTHONPATH=. .venv/bin/python -m nexus chat --renderer ratatui   # run it (needs a TTY)
```

Always `env -u FORCE_COLOR` when running tests; a forced colour environment makes
two argparse help tests fail. macOS has no `timeout`; `sed -i` needs an argument
(use Python for edits in scripts).

Full verification recipe and what each check proves:
[references/testing-and-verification.md](references/testing-and-verification.md).

## Reference docs

- [references/architecture.md](references/architecture.md): bridge protocol, snapshot
  fields, action types, block kinds, data flow of a keypress and of a streamed token.
- [references/rendering.md](references/rendering.md): layout regions, palette, row
  building, wrapping, mouse hit-testing, native row and style contracts.
- [references/testing-and-verification.md](references/testing-and-verification.md):
  tests, fixtures, screenshots, PTY, common failures.
- [references/parity-checklist.md](references/parity-checklist.md): per-feature
  feature audit list and verification requirements.

## Done means

Rust tests, Python `-k ratatui`, layering/docs tests and Ruff pass; a screenshot
of the changed screen is inspected; the ledger says what is
verified and what is not. Do not claim parity from unit tests alone. Do not commit,
merge, publish or bump versions unless asked.
