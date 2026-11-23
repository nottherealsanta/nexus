# Antigravity ACP approval card — TUI / NSH parity check (2026-11-23)

## Result

For the actual ACP permission request in the repository's fake `agy` fixture
(`read_file`, title `Read a.txt`, args `{path: "a.txt"}`), the TUI and NSH render
the existing approval card with the same title, sub-line, parameter table,
choices and letters. The capture pair is visually equivalent apart from
medium-level presentation.

A second paired capture with an ACP `execute` permission whose `command` is one
long line found a real, pre-existing card difference: **the TUI clips the value
to one row, while NSH wraps it up to three lines.** The TUI frame ends
`…/verify-artifacts --f…`; NSH shows the full value on two lines, including
`--fail-on-warning --output dist/report.json`. This is not introduced by ACP and
is not specific to ACP, but an ACP `execute` request can hit it, so full parity
for every ACP permission is not yet established. It needs a fix or an explicit
client difference decision; no product source was changed here.

### Pair 1 — the read_file permission the fixture sends

| | TUI | NSH (web) |
| --- | --- | --- |
| State | child `ACPPermissionChild2`, ACP `session/request_permission` pending | same Core and session |
| Capture | 170x50 cells, dark, `--dump-frame` | 1440x900, dark, Chromium |
| Card title | `Allow Antigravity · read_file — Read a.txt?` | same |
| Sub-line | `waiting on you · 0:00` | `waiting on you · 0:01` (capture timing) |
| Args table | `path  a.txt` | `path  a.txt` |
| Choices | `y Allow once`, `a Allow for session`, `n Deny`, `d Deny with a note` | same labels and letter keys |
| Focus hint | `alt+w answer` | `⌥⌘W answer` |
| Screenshot | `tui-acp-approval-170x50-dark.png` / `.txt` / `.svg` | `nsh-acp-approval-1440x900-dark.png` |

Both captures show the same surrounding state: child heading
`ACPPermissionChild2` with `ZMX · Antigravity · gemini-test · agy · default`, the
ZMX context-incomplete card, the user message `do it`, the approval card, and the
composer. Neither capture shows a tool row: both clients were opened after the
ACP `tool_call` live update, so the live-only part was not replayed. This is the
same in both clients and is not an approval-card difference; the card/tool-row
join should be checked with a client already open before the permission request.

### Pair 2 — a long single-line execute argument

| | TUI | NSH (web) |
| --- | --- | --- |
| State | child `ACPPermissionLong`, ACP `execute` permission pending | same Core and session |
| Card title | `Allow Antigravity · execute — Run the release build and verify the artifact checksums?` | same |
| Args table | `command` clipped to one row, ending `…/verify-artifacts --f…` | `command` wrapped over two rows, full value shown |
| Screenshot | `tui-acp-long-170x50-dark.png` / `.txt` / `.svg` | `nsh-acp-long-1440x900-dark.png` |

The long request was produced by a scratch copy of
`tests/fixtures/workers/fake_agy_acp.py` whose permission `toolCall` is an
`execute` with the command
`cd /private/tmp/acp-parity.EAdnzc/ws && npm run build -- --mode production && ./scripts/verify-artifacts --fail-on-warning --output dist/report.json`
(`pending-long-approval.json`). The repository fixture was not edited.

## How the state was made

A real Core ran against a scratch home and workspace. `NEXUS10_AGY_CLI` pointed at
a scratch launcher for `nexus-core/tests/fixtures/workers/fake_agy_acp.py` with
scenario `permission`, so Core received a genuine ACP `session/request_permission`
request; no provider, real `agy` or credentials were used.

```sh
S=$(mktemp -d /tmp/acp-parity.XXXXXX)
# $S/agy: python launcher for tests/fixtures/workers/fake_agy_acp.py
# $S/ws/acp-scenario.txt: "permission"
cd nexus-core
NEXUS10_HOME="$S/home" NEXUS10_AGY_CLI="$S/agy" NEXUS_PORT=47931 \
  uv run python -m nexus serve --port 47931
# enable zmx.antigravity in $S/home, then:
# POST /api/project {"path": "$S/ws"}
# POST /api/session {"project": ..., "title": "ACP Approval Root", "permission": "allow"}
# POST /api/session {"parent": ..., "title": "ACPPermissionChild2",
#                    "model": "agy/gemini-test", "permission": "allow"}
# POST /api/session/<child>/prompt {"text": "do it"}
```

Core's pending approval (`pending-approval.json`):

```json
{
  "tool": "Antigravity · read_file — Read a.txt",
  "call_id": "agy:t1",
  "args": {"path": "a.txt"},
  "external": "agy"
}
```

### TUI capture

`nexus-tui` was rebuilt from the current source and shared manifest before the
capture (`cargo build`, 3.9 s incremental), then run as:

```sh
cd nexus-tui && cargo build
NEXUS10_HOME="$S/home" NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 \
  target/debug/nexus-tui --dark --dump-frame 170x50 \
  --out "$S/shots" --name tui-acp-current --svg --open first
```

`--open first` selected the prompted child. The text frame was converted to PNG
with headless Chromium at 8.4 x 18 px per cell, as `nexus-tui/tools/shots.sh` does:

```sh
CHROME=$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell | tail -1)
"$CHROME" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files \
  --window-size=1429,901 --screenshot="$S/shots/tui-acp-current.png" \
  "file://$S/shots/tui-acp-current.svg"
```

### NSH capture

`nsh` was missing (broken symlink to an absent release binary), so NSH 0.4.0 was
built from `~/.nsh/source`, then the current `nexus-nsh` bundle was packaged:

```sh
cd ~/.nsh/source && cargo build --release -p nsh-cli
cd nexus-nsh && PATH="$HOME/.local/bin:$PATH" npm run build
NEXUS10_HOME="$S/home" NEXUS_NSH_NO_START=1 node scripts/serve.mjs --port 47933
node "$S/nsh-shot.mjs" http://127.0.0.1:47933 "$S/shots/nsh-acp-current.png" 1440 900 dark
```

The screenshot files were replaced after both clients were rebuilt, so the pair
shows current source and current `interface/manifest.toml`. `nsh-capture.mjs` is
the capture script. It opens the waiting child through the banner's `go` action
(the child is not a root row in the rail), then asserts and reads
`[data-testid="waiting-card"]`. The project's `e2e/shots.mjs` was not used: it only
captures its seeded `bash` approval and its fixed session ids, and the browser
tool's bundled Chromium was unavailable (`browser-use` exited -5), so a minimal
Playwright script against the real Core was the faithful option. The onboarding
overlay was dismissed with `Skip` when present.

## Tests

| Command | Result |
| --- | --- |
| `cd nexus-tui && cargo test --test shell waiting` | 2 passed |
| `cd nexus-tui && cargo test --test shell a_allows_for_the_session` | 1 passed |
| `cd nexus-tui && cargo test --test shell d_denies_with_the_composer_text_as_the_note` | 1 passed |
| `cd nexus-nsh && node scripts/nsh-web.mjs test --target web i8.test.tsx --filter "an approval shows"` | 1 passed |
| `cd nexus-nsh && node scripts/nsh-web.mjs test --target web i8.test.tsx` | 8 passed, 4 failed |
| `cd nexus-core && uv run pytest -q tests/test_zmx_runtime.py -k permission` | 6 passed |
| `cd nexus-core && uv run pytest -q tests/test_zmx_antigravity_acp.py -k permission` | 9 passed |

The four failures in the full `i8.test.tsx` run are `open("./App.tsx"): the browser
is gone: IO error: Connection reset by peer` in unrelated tests (edit diff, arrows,
toasts, banner), not assertion failures. The approval test passed in both runs.

## Differences and open questions

- **Long single-line parameter values (resolved, see the follow-up fix below).**
  The TUI used to put a value on one row and truncate it; NSH wrapped it up to
  three rows. Both clients now use the same rule: wrap to at most three rows,
  then end in `…`.
- **Key hint:** `waiting.focus` is `alt+w` in the TUI and `cmd+alt+w` in NSH, so the
  card footer reads `alt+w answer` vs `⌥⌘W answer`. This comes from the shared
  manifest and is unrelated to ACP; it is the only other non-medium difference
  seen. No ledger row was added because this check made no product change.
- **Tool row absent on both captures:** both clients were opened after the live
  ACP `tool_call` update; same in both, but the documented card/tool-row join with
  a client already open before the permission request was not verified here.

## Working tree

At the time of the original check, no product source was edited and no existing working-tree change was reverted.
`npm run build` regenerated `nexus-nsh/src/gen/manifest.ts` and
`nexus-nsh/src/gen/terminal-page.ts`, which were already modified before this
check, and wrote the ignored `nexus-nsh/dist/`. This report directory is the only
workspace artifact added by this check. The scratch Core, fake `agy` launchers and
test-only long scenario live under `/tmp/acp-parity.EAdnzc` and are not in the
repository.

## Follow-up fix — long parameter values (2026-11-23)

The user requires TUI/NSH peers and asked for the shared behaviour that shows as
much as possible consistently, so both clients now wrap a parameter value to at
most three rows and end the last row with `…` when it does not fit. This is one
product rule, not a client-specific difference; no ledger row was needed.

### Changed

| Client | Files |
| --- | --- |
| TUI | `nexus-tui/src/render.rs`: `WAIT_VALUE_ROWS = 3`; `wrap_value`; `WRow::Table(k, first, clipped)` and `WRow::TableValue(line, clipped)`; fitting drops whole later parameters first, then a value's continuation rows (marking the last visible row clipped), before options; drawing aligns continuation rows under the value column and adds `…` to the clipped row. `nexus-tui/tests/alerts.rs`: two tests; `nexus-tui/README.md` wording |
| NSH | `nexus-nsh/src/ui/composer.tsx`: `WAIT_VALUE_ROWS = 3`, used for the parameter value's `numberOfLines`; `nexus-nsh/i8.test.tsx`: one test; `nexus-nsh/README.md` wording |

No generated file was regenerated for this fix: the NSH capture used `nsh dev`
with `scripts/serve.mjs --upstream`, not `npm run build`.

### Tests

| Command | Result |
| --- | --- |
| `cd nexus-tui && cargo test --test alerts` | 13 passed, 1 ignored (`shots`); includes `a_long_parameter_value_wraps_instead_of_clipping_to_one_row` and `a_parameter_value_wraps_to_three_rows_then_ends_in_an_ellipsis` |
| `cd nexus-tui && cargo test --test shell waiting` | 2 passed |
| `cd nexus-nsh && node scripts/nsh-web.mjs test --target web i8.test.tsx --filter "a long parameter value"` | 1 passed |
| `cd nexus-nsh && node scripts/nsh-web.mjs test --target web i8.test.tsx --filter "approval"` | the approval test passed; `an edit approval shows a capped diff` failed with the known `open("./App.tsx"): the browser is gone: Connection reset by peer`, unrelated to the card |

The NSH test keeps the full value in `approvalParts` and asserts the card text
contains it; the TUI test asserts the three-row cap, the ellipsis and that the
fourth-row content is absent. Visual wrapping in NSH is confirmed by the pair
below (the headless NSH test API exposes text, not laid-out line counts).

### Pair 3 — the same long execute argument after the fix

Same scratch Core, home and fake `agy` long-permission fixture as pair 2; the
only new state is child `ACPPermissionLongFix`, created under the root and
prompted with `do it`.

| | TUI | NSH (web) |
| --- | --- | --- |
| Capture | `tui-acp-long-after-170x50-dark.png` (170x50 cells, dark, `--dump-frame`) | `nsh-acp-long-after-1440x900-dark.png` (1440x900, dark, `nsh dev` + proxy, Playwright) |
| Args table | `command` wraps over two rows, showing the full command through `dist/report.json` | same value, wrapped over two rows, full through `dist/report.json` |
| Structure | title, `waiting on you · 0:00`, wrapped value, four choices, `alt+w answer` | same title, `waiting on you · 0:01`, wrapped value, four choices, `⌥⌘W answer` |

Both images were read after capture. The only remaining differences are medium
ones (wrap points and cell/pixel widths) plus the previously reported key-hint
label `alt+w` / `⌥⌘W`, which comes from the shared manifest and is unchanged by
this fix. The TUI text frame is `tui-acp-long-after.txt`; its two command rows
read `…./scripts/verify-artifacts` and `--fail-on-warning --output
dist/report.json`, so the old one-row clip ending in `--f…` is gone.

### Working tree at the end of the fix

Only the approval-card rendering, tests and matching README paragraphs above
were edited by this fix. Concurrent edits in both clients (including
`nexus-nsh/src/gen/*`, `nexus-tui/src/app.rs`, timeline and scenario files) were
left untouched. The scratch Core, dev host and proxy were stopped; the scratch
state stays under `/tmp/acp-parity.EAdnzc` and is not in the repository.
