# GPUI desktop client

`nexus desktop` opens a Rust GPUI window in `rust/desktop/`. It is an additional
client of the existing workspace daemon. The terminal remains available.

## Build and run

```sh
cargo build --manifest-path rust/desktop/Cargo.toml
.venv/bin/nexus desktop
.venv/bin/nexus desktop --session SESSION_ID
NEXUS_HOME=/tmp/nexus-desktop-dev .venv/bin/nexus --dev desktop
```

The source executable is discovered automatically; `NEXUS_DESKTOP_BINARY` selects
an explicit executable. Desktop builds are separate from the terminal wheel build.
GPUI is pinned to 0.2.2. macOS needs Xcode's Metal toolchain. Other platforms have
not been verified. No TTY is required. Closing the window detaches the viewer and
leaves daemon turns running.

For a local macOS bundle (also gives native automation a stable app identity):

```sh
python3 skills/native-app-review/scripts/package_macos.py \
  rust/desktop/target/debug/nexus-desktop rust/desktop/Nexus.app \
  --name Nexus --bundle-id dev.nexus.desktop
NEXUS_DESKTOP_BINARY="$PWD/rust/desktop/Nexus.app/Contents/MacOS/nexus-desktop" \
  .venv/bin/nexus desktop
```

Launch through `nexus desktop`: opening the raw bundle through Finder does not
start the Python presentation bridge. The development wrapper is unsigned and is
not an installer.

## Architecture

`nexus/ui/desktop/run.py` reuses `nexus/ui/ratatui/prototype.py`'s host controller,
workflow actions and presentation projection. The Python process owns canonical
view reduction and host RPCs. Rust receives versioned JSONL snapshots on stdin
and emits typed actions on stdout. Diagnostics go to stderr. Neither Rust nor the
Python surface reads session storage or owns providers/tools. Credentials and
permissions remain in the daemon. The wire is private presentation data, not a
second public host API.

Rust includes the existing native bridge schema so protocol structs stay aligned.
Schema 2 transcript patches are applied in order, never dropped. The input reader
is bounded at 16 MiB per snapshot and a 32-message channel. Reconnect and session
changes use the existing generation checks and durable replay. Each session keeps
a local unsent draft; durable queued messages stay in the daemon.

## Interface

- Real native title bar controls, `#0B0B0B` workspace surfaces, restrained angled
  corners, Lucide stroke icons, cyan selection, and semantic status colors. Independently
  designed dark and light palettes (`/theme`), and standard macOS shortcuts.
- Session search/sidebar, conversation tabs, workspace breadcrumb, context chips,
  virtualized variable-height transcript, focused Session/Files/MCP/Logs inspector tabs, tool/thought expansion, subagent pages,
  Markdown, diffs and complete parameter/result inspection.
- Multiline composer with Unicode selection, native IME integration, copy/paste,
  undo, image clipboard attachments, Shift+Enter newline and Enter send. Agent/model/effort selection sits inside the composer; context
  occupancy remains below it.
- Settings navigation, configuration file editing with 700 ms autosave where the host enables it, explicit Save and confirmed delete/reset, model pickers,
  providers, tiers, titles, voice/speech, tools, skills, MCP and hooks use the native
  shell workflows. All native slash commands remain available.
- Permission and question sheets retain disabled approval choices and host validation.
- Native attachment file picker; `/attach PATH` also works. Voice and speech use
  existing daemon consent/download workflows.
- Sessions collapse below 820 logical pixels; details collapse below 1150. The Sessions/Details toolbar controls and Cmd+B/Cmd+L open collapsed panes as
  dismissible drawers. Dialogs fit the current window.

## Keys

| Action | Key |
| --- | --- |
| New session | Cmd+N |
| Commands | Cmd+K |
| Settings | Cmd+, |
| Models | Cmd+M |
| Sessions/details sidebar | Cmd+B / Cmd+L (Ctrl variants retained) |
| Context / provider usage | Cmd+I / Cmd+U (Ctrl variants retained) |
| Attach | Cmd+Shift+A |
| Dictation | Ctrl+Space |
| Queue / interrupt submit | Ctrl+Enter / Alt+Enter |
| Model favorite / sort / refresh | Ctrl+F / Ctrl+S / Ctrl+R in model picker |
| Stop turn | Cmd+. / Ctrl+C |
| Dismiss panel | Escape |
| Save settings file | Cmd+S |
| Cycle agent / effort | Cmd+Shift+G / Cmd+Shift+E |
| Toggle logs | Cmd+Shift+L |
| Reconnect | Cmd+R |
| Latest transcript | Cmd+J |
| Prompt history | Option+Up / Option+Down |
| Completion selection / insert / dismiss | Up/Down / Enter or Tab / Escape |
| Composer focus | Cmd+Enter |
| Theme | Cmd+Shift+T |

## Visual verification

The repo-local [GPUI skill](../skills/gpui-nexus/SKILL.md) adapts reviewed Longbridge
GPUI guidance to the pinned client. Its provenance and licenses travel with it.
The reusable [native-app-review skill](../skills/native-app-review/SKILL.md) defines
launch/capture/review of the actual application with computer-use tools. Captures
and the review ledger live in ignored `artifacts/desktop/`. `--preview SNAPSHOT.json`
on the Rust binary renders a labelled, read-only fixture for repeatable visual QA;
fixture actions cannot mutate the daemon. Host journeys are checked separately.
`NEXUS_DESKTOP_WINDOW_SIZE=780x720` sets an initial logical size for reproducible
narrow-window checks (bounded to the supported window range). For computer-use
review, `NEXUS_DESKTOP_REVIEW=1` additionally enables Cmd+Shift+Y (780×720) and
Cmd+Shift+O (1440×940) to resize the real window through GPUI; these diagnostic
bindings are absent in normal launches.

Generate bounded approval/question/image fixtures with
`python3 rust/desktop/scripts/visual_fixtures.py artifacts/desktop/fixtures`, then
launch the bundled executable with `--preview artifacts/desktop/fixtures/approval.json`.
These fixtures never connect to a daemon or execute their example actions.

See the review ledger for actually captured states and outstanding limitations;
appearance and platform behavior must not be inferred from compilation alone.

GPUI 0.2.2 does not expose the custom controls as a native accessibility tree in
this macOS run. Keyboard navigation is implemented; screen-reader support is not
verified. Physical microphone/speaker workflows and other operating systems are
not verified by the scripted UI checks.

## Visual direction

OpenChamber's persistent sessions/work area/inspector composition informs density
and hierarchy, rather than its web implementation. The dark workspace background
is #0B0B0B; only controls, code and raised decisions have near-black surfaces.
A small 45-degree cut on composer and decision frames adds the requested restrained
cyberpunk geometry. Color identifies selection or actual state, never fictional
telemetry. Lucide SVGs are embedded and licensed locally; consistent monochrome
strokes follow SF Symbols' optical principles without shipping Apple-only assets.

References: [OpenChamber](https://github.com/openchamber/openchamber),
[GPUI Kit guides](https://gpui-kit.com/docs/design-guides),
[SF Symbols](https://developer.apple.com/sf-symbols/),
[Lucide](https://github.com/lucide-icons/lucide).

## Ratatui parity audit

The desktop shares native host workflows, but a shared command implementation alone
is not proof that every interaction is verified. The current audit checks each
projected action and its desktop entry point, followed by native integration journeys.
Submitted attachment chips open the existing message inspection page; the bridge
allows chip actions only when present in the current projection. Secret forms save
on Enter and never send draft updates; normal host-enabled autosave is debounced
700 ms and guarded by form identity, revision and session generation. Escape flushes
autosave before closing. Dictation exposes insert, send and discard; Escape discards
an active recording/transcription. Hardware microphone, provider sign-in, and every
Ratatui workflow have not yet been verified end to end in the desktop.

The supplied Comet reference screenshots further inform the compact title bar,
right-aligned user messages, borderless expandable tool trees, and inline composer
choices. Nexus retains an opaque near-black/light workspace and modest angled
frames; every condensed tree remains expandable to full host-projected detail.

| Ratatui capability | Desktop entry point / evidence |
| --- | --- |
| Submit, steer, queue, interrupt, cancel and restored drafts | Composer, Ctrl+Enter, Alt+Enter, Stop; ordered-patch/session-draft native tests |
| History, Unicode editing, selection, IME, undo/redo, attachment markers | Native editor; Option+Up/Down; native input regression tests |
| Slash/argument/file completion | Host completion with current-prefix guard; all choices in a bounded scroll region; `@` preservation test |
| Sessions, workspace groups, tabs, rename/archive/export/fork | Sidebar, searchable Sessions and session action menus; native session switching journey |
| Context, tools, skills, MCP, hooks and budgets | Context chips, Cmd+I, inspector and shared settings workflows |
| Model, provider, tier, effort and root agent choices | Inline composer choices, settings, favorite/sort/refresh and cycle actions |
| Tool/thought/exploration groups, complete inputs/results and diffs | Focusable expandable transcript trees; actual marathon replay and keyboard expansion |
| Subagents, nested agent pages, worktrees and background tasks | Task rows, session details and shared `/agents`/`/tasks`/`/worktrees` menus |
| Settings scopes, file creation/edit/autosave/save/delete/reset | Settings navigation, native editor, explicit Save and host confirmation; project autosave journey |
| Approval choices and free-form questions | Host-validated sheets; native fixtures and live question-answer journey |
| Attachments: files, image clipboard, preview, remove, submitted inspection | Native file picker, `/attach`, draft chips and message chips; marker/operation tests and live preview |
| Local voice and speech | Composer controls and settings; insert/send/discard; physical audio remains unverified |
| Logs, daemon reconnect, notices and update help | Details tabs, Cmd+Shift+L, Cmd+R and update notice action |
| Dark/light themes, pane toggles and narrow drawers | Host preferences and semantic palettes; real screenshots; native narrow drawer test |

This table audits implemented entry points; it does not claim every external
provider, hardware device or destructive workflow has been exercised. Native
screen-reader exposure and platform coverage remain the limitations above.

## Verification record

The macOS native review uses an isolated dev daemon and actual computer-use
screenshots, with a capture ledger at `artifacts/desktop/review-ledger.md`. Final
Session, Files and MCP inspector captures verify that each tab filters its own
sections. Approval and image sheets were checked in dark and light fixtures;
completion selection/insertion and expanded tool parameters were checked against
the live host. Fixtures are rendering evidence, not executed approval workflows.

Desktop Rust tests: 20 passed. Ratatui Rust tests: 59 passed, 2 ignored. Focused
Python launch/action/layering/docs checks: 156 passed, one known baseline failure
deselected. The complete Python suite had 5266 passed and 13 failures; all 13 were
reproduced on the pre-desktop commit `6c46af4`, including the PTY failure after
supplying its native binary. Full-suite, baseline and Ratatui subset logs live
under `artifacts/desktop/`. These existing failures were not changed by this work.
