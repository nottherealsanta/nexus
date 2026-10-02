---
name: nexus-ratatui
description: Work on the native Ratatui terminal client for Nexus (rust/tui/, nexus/ui/ratatui/, shared ui_support helpers). Use when changing how `nexus chat --renderer ratatui` looks or behaves, adding a chat command/shortcut/panel to the native shell, closing a Textual-parity gap, debugging the Python<->Rust bridge, or verifying native changes with Rust tests, the PTY check and Playwright screenshots.
---

# Working on the Nexus Ratatui client

The goal of this client is to replace the Textual shell (`nexus/ui/tui/`) while
keeping the same host contract, the same information, and as close to the same
look and behaviour as possible. **Textual is the reference.** When in doubt, read
what Textual does and match it; do not invent a new design.

Read first, in this order: `AGENTS.md` (rules tests enforce), `plans/RATATUI_PLAN.md`
(current state, open work, honest "not verified" list), `docs/ratatui-parity.md`
(ledger). Update those with every behaviour change.

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
- Rust must stay dumb: Python decides *what* (order, blank-row gaps, text, colours
  as tokens); Rust decides *how* (wrap, pad, paint).
- Details: [reference/architecture.md](reference/architecture.md).

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
| Pure helpers shared with Textual | `nexus/ui_support/{timeline,details,context_header,completion,model_choice,shortcuts,session_groups,voice_settings,session_controller}.py` |

## Workflow for any change

1. **Find the Textual behaviour.** Grep `nexus/ui/tui/` and `nexus/ui_support/tui_*.py`.
   Compare label, order, empty state, error wording, shortcut.
2. **Put shared logic where both can use it.** If the logic is pure text/data, it
   belongs in `nexus/ui_support/<name>.py` (no Textual/Rich imports) and Textual
   should call it too. Never import `nexus/ui/tui` or Textual from native code:
   `tests/test_ui_layering.py` and the PTY/launch tests enforce it, and the point is
   that the native runtime works without Textual.
3. **New data or action for the UI?** Add a host command (`nexus/host/protocol.py`
   -> `facade.py`) rather than reading files; see AGENTS.md rule 4.
4. **Project it** in `prototype.py` (control-safe: pass text through
   `redact(escape_controls(...))`). Add fields to `bridge.rs` structs with
   `#[serde(default)]` so older/partial snapshots still parse.
5. **Draw it** in `render.rs` / `transcript.rs` using palette tokens only
   (`Palette`, mirrors `nexus/ui/tui/theme.py`). No hard-coded colours.
6. **Handle input** in `main.rs`, send an action, handle it in `prototype.py`
   (action dispatch) or `workflows.operate`.
7. **Test** (see below), **screenshot** it, **update docs** (`ratatui-parity.md`,
   `plans/RATATUI_PLAN.md`, `module-map.md` for new Python modules).

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
- **Margins collapse like Textual.** Python computes `gap` (blank rows before a
  block) with `max(prev_bottom, top)`; do not add blank rows in Rust.
- **Wide characters and graphemes** stay whole; wrap with `transcript::wrap`.
- **Web mirrors the TUI.** A wording/feature change in one surface goes in the other
  (`docs/web.md`).

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
[reference/testing-and-verification.md](reference/testing-and-verification.md).

## Reference docs

- [reference/architecture.md](reference/architecture.md): bridge protocol, snapshot
  fields, action types, block kinds, data flow of a keypress and of a streamed token.
- [reference/rendering.md](reference/rendering.md): layout regions, palette, row
  building, wrapping, mouse hit-testing, Textual-to-native style mapping.
- [reference/testing-and-verification.md](reference/testing-and-verification.md):
  tests, fixtures, screenshots, PTY, common failures.
- [reference/parity-checklist.md](reference/parity-checklist.md): per-feature
  parity audit list and the definition of done for replacing Textual.

## Done means

Rust tests, Python `-k ratatui`, layering/docs tests and Ruff pass; a screenshot
of the changed screen sits next to Textual's; the ledger and plan say what is
verified and what is not. Do not claim parity from unit tests alone. Do not commit,
merge, publish or bump versions unless asked.
