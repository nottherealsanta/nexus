# GPUI desktop client

`nexus desktop` opens a Rust GPUI window in `rust/desktop/`. It is an additional
client of the existing workspace daemon. The terminal remains available.

## Build and run

```sh
cargo build --release --manifest-path rust/desktop/Cargo.toml
.venv/bin/nexus desktop
.venv/bin/nexus desktop --session SESSION_ID
NEXUS_HOME=/tmp/nexus-desktop-dev .venv/bin/nexus --dev desktop
```

The source executable is discovered automatically; `NEXUS_DESKTOP_BINARY` selects
an explicit executable. Source discovery prefers release over debug regardless of
modification time; a debug-only source fallback prints a performance warning.
Installed executables beside Python retain priority. Development builds optimize
dependencies at level 2. Desktop builds are separate from the terminal wheel build.
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

The private desktop wire is encoded by `nexus/ui/desktop/wire.py` as schema 3.
It sends per-topic deltas and transcript block operations: a stable block ID can
carry a Unicode-safe suffix append when its text grows, otherwise a splice updates
the changed block-list range. A generation or agent-page change resets topics and
transcript. One-shot composer insert/restore values are resent once, and unchanged
snapshots produce no message. Rust applies updates transactionally, rejects stale
revisions, and accepts schema 1 and 2 snapshots for compatibility. Metadata-only
updates do not clone transcript blocks in the decoder. The input reader is bounded
at 16 MiB per snapshot and a 32-message channel. Reconnect and session changes use
the existing generation checks and durable replay. Each session keeps a local
unsent draft; durable queued messages stay in the daemon.

## Interface

- When the daemon is unreachable the projection sets `disconnected`: a top banner
  ("Disconnected — Reconnect ⌘R") appears, the composer becomes read-only with
  its draft kept, and submit is refused; the timeline stays readable and the
  Reconnect route replays durable state.
- Real native title bar controls, `#0B0B0B` workspace surfaces, curved
  controls, Lucide stroke icons, neutral selection, and semantic status colors. Independently
  designed dark and light palettes (`/theme`), and standard macOS shortcuts.
- Session search filters by title, id and workspace; Up/Down move a highlighted
  match, Enter opens it, Escape clears the field, and a clear control appears
  while the field is non-empty. The sidebar, conversation tabs, workspace breadcrumb, context chips,
  virtualized variable-height transcript, focused Session/Files/MCP/Logs inspector tabs, tool/thought expansion, subagent pages,
  Markdown, diffs and complete parameter/result inspection. Diffs render as unified rows with tabular old/new gutters, a `+`/`-`
  marker and add/remove background tints rather than a colourless 50/50 split.
- The transcript follows new content while the viewport still reaches the newest
  item. Scrolling above it turns follow off and, when content then arrives, a
  bottom-centre "↓ New messages" pill restores follow (also Ctrl+End / Cmd+J).
- Above 900 px a 12 px turn minimap sits at the left of the column: a long tick
  per user turn, a short tick per assistant turn, and a band for the visible
  range. Hovering shows the turn's first line; clicking scrolls to it. Ticks are
  derived from projected block kinds, with no invented counts or timestamps.
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
  dismissible drawers. Widening past the threshold re-docks the pane and clears
  the drawer flag, so it does not reappear on the next narrow resize. Dialogs fit the current window.
- Transient notices are a bounded toast stack (at most three) bottom-centre above
  the composer: info toasts auto-dismiss after 6 s, warnings and errors persist
  with an explicit Dismiss, and notices with a host action (Reconnect, Update
  help) render that button. Repeated identical messages replace the current
  toast instead of stacking. This replaces the previous fixed-position error box.

## Keys

The Ratatui Ctrl-key semantics are canonical; macOS Cmd bindings are aliases.
`rust/desktop/src/keymap.rs` is a declarative table checked against the shared
`ui_support/shortcuts.py` tables by `tests/test_desktop_keymap_parity.py`.

| Action | Key |
| --- | --- |
| Send / newline | Enter / Shift+Enter or Ctrl+J |
| Queue / interrupt submit | Ctrl+Enter / Alt+Enter |
| New session | Ctrl+N (Cmd+N) |
| Commands | Ctrl+P (Cmd+K) |
| Sessions list | Ctrl+O (Cmd+O) |
| Fork session | Ctrl+F |
| Agent picker | Ctrl+G, or `a` outside the editor (Cmd+Shift+G cycles) |
| Sessions/details sidebar | Ctrl+B / Ctrl+L (Cmd+B / Cmd+L) |
| Settings | Ctrl+S (Cmd+,); Ctrl+S saves inside a form; Ctrl+F/S/R are scoped in the model picker |
| Context / provider usage | Ctrl+I / Ctrl+U (Cmd+I / Cmd+U) |
| Cycle effort / toggle logs | Ctrl+T / Ctrl+E |
| Dictation | Ctrl+Space |
| Attach | Cmd+Shift+A |
| Stop turn | Ctrl+C or Escape twice within 1.5 s (Cmd+.) |
| Dismiss panel | Escape |
| Reconnect / quit | Ctrl+R / Ctrl+Q |
| Latest transcript | Ctrl+End (Cmd+J) |
| Scroll transcript | PageUp / PageDown (outside the editor) |
| Inspector tabs | `[` / `]` (outside the editor) |
| Prompt history | Up / Down on a single-line draft, or Option+Up / Option+Down |
| Completion selection / insert / dismiss | Up/Down / Enter or Tab / Escape |
| Tab | insert a completion, force a completion, or enter transcript navigation on an empty draft |
| Transcript navigation | j/k or Up/Down or Shift+Tab move, Enter/Space opens, Escape or any other key leaves |
| Focus traversal | Ctrl+Tab / Ctrl+Shift+Tab |
| Composer focus / theme | Cmd+Enter / Cmd+Shift+T |
| Ctrl+X leader | m v n o f g b l s i e t u r c z ? (model, dictate, new, sessions, fork, agent, sidebars, settings, context, logs, effort, usage, reconnect, context popover, update help, shortcuts) |

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
Composer, user bubbles and floating decisions use matching 14 px curves; compact
controls and session rows retain smaller radii. A subtly raised neutral sidebar
and quiet pane dividers separate navigation from the #0B0B0B reading surface.
Send uses a contrasting neutral fill with readable hover and keyboard-focus states.
Selection, focus and links use neutral greys; success, input and error colors
retain their actual meanings. Lucide SVGs are embedded and licensed locally;
consistent monochrome strokes follow SF Symbols' optical principles without
shipping Apple-only assets.

The supplied screenshots inform compact title controls and activity trees. The
latest density pass removes repeated user-role labels, reduces tool-row gaps,
compresses code/diff padding and uses two-line session rows (name, message count
and last activity). Session summaries report activity, not last-opened timestamps;
no unsupported timestamp or turn count is invented. Utility controls in the title
bar and composer are icon-only with hover names and existing keyboard routes.

The composer follows Ratatui's editor → controls ordering: agent/model/effort
choices and reported context figures share the row below the editor frame.
The context label comes directly from the shared Ratatui projection, including
reported pricing/window thresholds; missing limits never become invented totals.
Attachments, dictation and speech are compact icons inside the frame. Context
remains clickable. Floating sheets have a brief 140 ms entrance fade that uses a
stable identity and does not restart on polling. New thumbnails fade in over 160 ms; there is no
perpetual decorative motion.

Draft and sent-message images have clickable thumbnails from host data. The
private snapshot carries at most eight recent inline images within a shared
4 MiB encoded budget. Full previews remain bounded to 4 MiB per image. Thumbnails
that exceed the inline budget remain accessible through attachment/message
inspection; these surfaces show every content block without raw base64 dumps.
Draft reads are cached, limited to two seconds each and guarded by generation.
Rust never reads attachment paths or fetches remote image URLs.

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
| Logs, daemon reconnect, notices and update help | Details tabs (`[` / `]`), Ctrl+E / Cmd+Shift+L, Ctrl+R, Ctrl+X c context popover and Ctrl+X z update help |
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

Desktop Rust tests: 66 passed. Ratatui Rust tests: 59 passed, 2 ignored. Focused
Python launch/action/layering/docs checks: 156 passed, one known baseline failure
deselected. The complete Python suite had 5266 passed and 13 failures; all 13 were
reproduced on the pre-desktop commit `6c46af4`, including the PTY failure after
supplying its native binary. Full-suite, baseline and Ratatui subset logs live
under `artifacts/desktop/`. These existing failures were not changed by this work.

The compact neutral/image-preview follow-up passed 20 desktop Rust tests,
59 Ratatui Rust tests (2 ignored), and 193 focused Python checks (2 known baseline
failures deselected). Native captures cover both themes, expanded tool rows,
draft and sent thumbnails, full previews and keyboard attachment removal.
The final composer context-label rebuild passed all 20 desktop tests again.

## Large session lists

The session sidebar uses GPUI's variable-height virtual list with 100 px of
overscan. Only visible session rows and their workspace/date headings create
controls. Search still covers every host-projected session (up to the host's
1,000-session limit), and filtering resets list measurements. Ordinary redraws
retain the list's scroll position. A native rendering regression loads 1,000
sessions, verifies fewer than 100 rows are instantiated for a frame, and checks
that searching reaches session 999. This addresses the previously eager sidebar;
it does not establish a frame-rate guarantee for every conversation or machine.

## Overhaul performance foundation

Phase 0 of [the overhaul plan](../plans/in-progress/DESKTOP_OVERHAUL_PLAN.md) is in progress.
Composer edits paint locally. Draft synchronization waits for 250 ms of quiet;
completion waits for 120 ms, and Tab requests completion immediately. Transcript
re-measurement is debounced the same way: a width change during a window drag
schedules one re-measure after 120 ms of quiet instead of resetting the list
every frame, so the scroll position no longer jumps mid-drag. Submission,
composer blur, session-opening commands and quit flush pending drafts and cancel
pending completion tasks. Local drafts remain per session; this does not add
persistent draft storage. Generation guards prevent delayed work crossing a reconnect.

Preview and inline image base64 decoding runs on a background executor, bounded
to eight inline images plus a preview and 4 MiB decoded per image. A retained task
cancels superseded batches; generation and image revision guards reject stale
results. GPUI still owns image format decoding and rendering. Theme/input state
updates happen on snapshot and input events; log visibility reports happen on
snapshot, drawer and window-bounds changes. Transcript rendering borrows blocks
directly instead of cloning their full content on every visible row.

Set `NEXUS_DESKTOP_TRACE=1` to collect bounded timing samples. Two-second stderr
summaries report p50/p95/max for snapshot bytes, snapshot application, root element
layout requests, prepaint, paint and composer change-to-CPU-paint. No content or
credentials are logged. `trace.rs` holds at most 4,096 samples; ordinary launches
bypass the tracing element. These are CPU timings, not GPU/display presentation
or complete Taffy layout timings, and do not prove 60 fps or 120 Hz scrolling.

Schema-3 wire support is an incomplete Phase 1 slice. Python still projects a full
snapshot, while Rust applies it through the monolithic root entity and copies
internally accumulated transcript text. Entity splitting, update coalescing, and
image addressing are not implemented. Native performance for
schema 3 is not verified; performance targets and the full native review matrix
remain unverified. The serialiser byte target is covered by a Python test: a
token appended to a 2,000-block session emits no topics and a single append op
under 8 KiB, with 500 updates totalling under 64 KiB.

Phase 7 keyboard parity is complete in the shell keymap: every row of
`SHORTCUTS` and `LEADER_SHORTCUTS` has a desktop route, `Ctrl+X ?` opens the
shared `/hotkeys` reference, `Ctrl+X c` / `Ctrl+X z` dispatch the context
popover and update help, `a` opens the agent picker outside the editor,
PageUp/PageDown scroll the transcript, `[` / `]` cycle inspector tabs and a
double Escape within 1.5 s stops the turn. The `Form` context saves with
`Ctrl+S`. A Rust test asserts every bound action has a handler. A live manual
key sweep and the remaining native states are still unverified.

Phase 0 verification: 27 desktop Rust tests and 159 focused Python checks passed.
The actual isolated dev desktop completed the tool marathon (8/8), multiline
input, light/dark transition and settings review. `artifacts/desktop/perf-baseline.md`
records post-change dev CPU timings and captures; no before/after or release
frame-rate claim is made. Historical Phase 0 schema-2 traffic was above the planned
8 KiB target; this is not a schema-3 performance measurement.
