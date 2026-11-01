# Plan: file explorer, file editor and Git page

Status: agreed 2026-11-01. Step 0 (the proofs) and Step 1 (the panel) are done; Step 2 (the
editors) is next. Notes on what was measured are under [Steps](#steps).

Applies to the TUI and to NSH on desktop and web. None of it ships in the iOS app or any
other native mobile target (`AGENTS.md`). The web app is not restricted, at any width.

## Decisions

| Topic | Decision |
| --- | --- |
| Editor size | Replaces the user's editor, but stays small: very good highlighting, undo/redo and the `ratatui-code-editor` feature set. No tabs, language servers or Vim mode. |
| TUI editor | Vendor `ratatui-code-editor` and adapt it (see [TUI editor](#tui-editor)). |
| NSH editor | Use `Editor` and `DiffView` from `nsh/base` (see [NSH editor](#nsh-editor)). |
| Placement | A panel on the right. Narrow: slides over the timeline and composer, with a back button top left. Wide: sits beside the timeline. |
| Git scope | Worktrees, changes and branches, with actions. No merge, stash, rebase, conflict UI or hunk staging. |
| Agents | The editor and Git page are silent to the model. No timeline notices and no new agent tools. |
| Git confirmation | Every Git action except stage and unstage opens one popup after the click, and says so when a session is live on that worktree. |
| Auth | No new auth. Core's token rules stay as they are. |

## Layout

```text
Wide (TUI >= 140 columns, NSH >= 1100 px): beside the timeline
┌ Files Git ────┬ timeline ────────┬ ‹ Back  server.py   ● ──┐
│ ▾ nexus/      │ agent is         │  1 def main():          │
│   server.py   │ working…         │  2     run()            │
└───────────────┴──────────────────┴─────────────────────────┘

Narrow: slides in from the right, over the timeline and composer
┌ ‹ Back  server.py   ● ─────────────────────────────────────┐
│  1 def main():                                             │
│  2     run()                                               │
└────────────────────────────────────────────────────────────┘
```

- **One panel, two positions.** The panel is one element that stays mounted, anchored right.
  Wide: it takes 55% of the space right of the left sidebar, the timeline keeps the rest, and
  the right sidebar yields while the panel is open. Narrow: it covers the whole window, the
  composer included. Resizing across the breakpoint keeps the text, the undo history and the
  scroll position.
- **Thresholds** are constants to tune in the screenshot pass. Existing ones are
  `NARROW_BELOW = 100` and `RIGHT_OPEN_FROM = 120` (TUI) and `NARROW = 820` (NSH).
  The panel uses `PANEL_WIDE_FROM = 140` columns (TUI) and `PANEL_WIDE = 1100` px (NSH).
- **Motion.** NSH slides by animating the panel's translation. The TUI steps in over two frames,
  since `frames.rs` animates at 125 ms. Wide mode opens without a slide.
- **Closing.** Back, or Esc through the manifest's escape chain. Unsaved text asks first, as today.
- **Waiting agents.** When the panel covers the timeline and a session needs approval, the panel
  header shows a "waiting" badge, so a request can't sit unseen.
- **Same panel for diffs.** Files and Git both open into it. An open file has Edit and Diff views.
- **Manifest.** `interface/manifest.toml` says "exactly one centre view" and lists `editor` as a
  centre view. Move it to a new `[navigation.panel]`, rename the `view.editor` term to `panel.file`,
  add `panel.diff`, and update `check_manifest.py`.

## TUI editor

**Approach: vendor the crate.** Copy it to `nexus-tui/vendor/ratatui-code-editor/` as a path
dependency and credit it in `nexus-tui/NOTICE` (MIT). It is about 4,600 lines of Rust.

Why not depend on it directly:

| Finding (measured 2026-11-01, crate 0.0.6, `40ff181`) | Consequence |
| --- | --- |
| It renders to `ratatui-core` 0.1 types; the TUI pins `ratatui` 0.29. It compiles beside 0.29, but the types don't match. | Changing the imports to `ratatui` 0.29 was enough: `cargo check` passes on a scratch copy. |
| All grammars are mandatory: 18.4 MB stripped against a 0.36 MB baseline. The TUI is 13 MB today. | Without C#, C++ and Java it is 9.1 MB. Vendoring lets us choose. |
| Input needs crossterm 0.29; the TUI pins 0.28. | Turn the feature off, map TUI keys to the crate's `apply(action)`. |
| Clipboard goes through `arboard`, which won't work over SSH. | Remove it: copy with the TUI's OSC 52, paste from bracketed paste. |

Speed is good enough. On this Mac, release build, 100×40:

| File | Open | Keystroke plus frame (mean / worst) | Cached frame |
| --- | --- | --- | --- |
| `render.rs`, 276 KB | 73 ms | 10.9 / 13.0 ms | 0.19 ms |
| Python, 1 MiB | 97 ms | 30.8 / 37.5 ms | 0.28 ms |

Keep from the crate: the rope text, Tree-sitter highlighting of visible lines, selection with word
and line snapping, folds, marks, comment toggle, duplicate and delete line, indent, Unicode
handling, and diff views with word-level changes.

Add to it:

1. **Movement and deletion.** Its input handling has no Home/End, Page Up/Down, word moves or
   forward delete.
2. **Undo grouping.** Undoing 50 typed characters took 50 undos. Group runs of typing, as NSH does.
3. **Theme.** Line numbers and default text are hard-coded (`DarkGray`, `White`). Replace these and
   the `vesper` palette with `interface/theme.toml` tokens, and honour ASCII mode.
4. **Find and replace.** The crate has none. Build on `set_marks`, with `editor.find` (`ctrl+f`,
   already in the manifest). Plain text and a case toggle in both clients.

Languages: the agreed starting set is Rust, Python, JavaScript, TypeScript, Go, C, HTML, CSS, YAML, JSON, TOML,
Shell and Markdown. Measure the binary before adding more. Anything else opens as plain text.

## NSH editor

Based on `website/elements/` and `docs/dev/` in the NSH source at the pinned `09c1632`.

- **`Editor`** replaces the plain `Input` in `src/ui/editor.tsx`. It brings highlighting for 50+
  languages, undo/redo with grouping, folds, several carets, line numbers and find and replace.
  Set `language={path}` and restyle through `classes`, with `fontFamily` set to Monaspace Argon.
- **Find bar:** `Editor` has the matching (`search`, `findNext`, `replace`, `replaceAll`), but the
  bar is ours to draw. Use the same layout as the TUI's.
- **`DiffView`** shows diffs from `oldValue` and `newValue`, in the same highlighting. Import it
  under another name, since `src/ui/diff.tsx` already exports a `DiffView`.
- **Do not use `Sheet` for the panel.** It has a backdrop that takes presses, and a comment in
  `overlays.tsx` says it breaks Escape routing for dialogs opened after it (not retested on the pinned
  build). Follow `EdgeDrawer`: plain views, positioned by the app.
- **Web:** syntaxes load on first use from `nsh-syntaxes.bin`, so text is plain until they arrive,
  and highlighting runs on the main thread at 4 ms per frame. Check the web bundle serves that file.

**Reading and editing are one mode.** Both clients drop the separate read-only view and `editor.edit`.
A clean file reloads when it changes on disk. A modified one shows "changed on disk", as today.

**Known differences, for the porting ledger:** NSH has several carets and more languages; the TUI
does not. NSH folds by indentation unless given ranges, and the TUI by Tree-sitter. Syntax colours
map to the same theme tokens but tokenise differently (syntect against Tree-sitter), so they won't
match exactly.

## File explorer

- **Same tree in both.** The TUI has an expanding tree. NSH's Files tab goes one directory at a time
  with a parent button, so bring it up to the TUI's tree, with expanded state kept.
- **New actions:** new file, new folder, rename and delete, from the keyboard, the row menu and the
  palette. Delete asks first, gives the item count for a folder, and is permanent.
- **Core route:** `POST /api/project/{id}/files?session=` with
  `{action: "create_file" | "create_dir" | "rename" | "delete", path, to?}`, using the same path
  confinement and no-symlink rules as `files.py`. Publishes `files.changed`.
- **Limits stay** at UTF-8 and 1 MiB. Binary and oversized files show why they can't open.
- **Bug to fix first:** the 1 MiB limit on request bodies applies to the JSON, so a file just under
  1 MiB opens but can fail to save once it is escaped. Allow more for `PUT …/file` and keep the
  content limit at 1 MiB.

## Git page

A new left sidebar tab: Sessions, Files, **Git**, Projects. Repo-wide, starting on the current
session's worktree. Changes follow the selected worktree. The right sidebar's Git section stays as a
summary and links here.

```text
WORKTREES (3)
● main        2 changes
  feat-x      clean      ↳ "Auth" running
BRANCHES
  LOCAL   main ↑1  · feat-x (worktree) · old-spike
  REMOTE  origin/main · origin/release
CHANGES on main
  STAGED    M server.py
  UNSTAGED  ?? notes.md
```

| Section | Shows | Actions |
| --- | --- | --- |
| Worktrees | Path, branch, dirty count, sessions using it | Create, remove |
| Changes | Staged and unstaged, with line counts; Enter opens the diff in the panel | Stage, unstage, discard, commit |
| Branches | Local and remote, upstream, ahead/behind, last commit, which worktree has it | Switch, create, delete |
| Remote | Upstream state in the section header | Fetch, pull, push |

Safe defaults for each action:

- **Pull** is `--ff-only`. A diverged branch shows Git's error rather than starting a merge.
- **Push** never forces. With no upstream, the dialog says it will set one.
- **Switch** never stashes for you; Git's refusal is shown as it is. Switching to a remote branch
  creates the tracking branch.
- **Branch delete** is `git branch -d`, local only. An unmerged branch gets its own second warning
  before `-D`. A branch checked out in a worktree is refused.
- **Discard** is `git restore`, or deleting the file if untracked. The dialog says it can't be undone.
- **Commit** has no amend. Hooks run as usual, and hook failures are shown.
- **Worktree remove** keeps today's rules: never the main checkout, a locked or dirty worktree, or a
  path in use by a session.
- **In-progress merge or rebase** started elsewhere: show a banner and disable Git actions.

### Confirmation

Every action on this page opens one popup after the click, including fetch. Stage and unstage don't
ask, even while a session is working, and neither does refreshing. Deleting an unmerged branch is the
only action with a second warning. The dialog names the action and target, and focus starts on Cancel. Commit asks after you write the
message. The session lines come from a fresh read when the dialog opens:

```text
┌ Switch branch ──────────────────────────────────┐
│ Switch feat-x to main?                          │
│                                                 │
│ ⚠ Working on this worktree now:                 │
│   "Auth" (running)  "Docs" (waiting on approval)│
│ Also open here: "Old idea" (idle)               │
│                                                 │
│                        [ Cancel ]  [ Switch ]   │
└─────────────────────────────────────────────────┘
```

- **Working:** a session in this worktree, or a child of one, with an active turn, a pending approval
  or question, or a running background job. Shown as a warning, with the danger button style.
- **Idle:** a non-archived session whose directory is in this worktree. Shown plainly.
- **Race protection:** the request carries `confirmed_live`, the session ids the user saw. If another
  session has started working by then, Core returns 409 `worktree_live` with the new list, and the
  dialog reopens. Without this, the warning could be out of date when you confirm.

### Core API

Project-scoped, next to `files`, because branches and worktrees belong to the repo:

| Route | Returns or does |
| --- | --- |
| `GET /api/project/{id}/git` | Worktrees with their live sessions, local and remote branches, remotes |
| `GET /api/project/{id}/git/status?worktree=` | The shape of today's session `git` response |
| `GET /api/project/{id}/git/file?worktree=&path=&side=head\|index` | That version's text, for diffs (UTF-8, 1 MiB) |
| `GET /api/project/{id}/git/live?worktree=` | Working and idle sessions, for the dialog |
| `POST /api/project/{id}/git/action` | `{action, worktree, confirmed_live, ...}` (`confirmed_live` is not used for `stage` and `unstage`), one of `stage`, `unstage`, `discard`, `commit`, `switch`, `branch_create`, `branch_delete`, `fetch`, `pull`, `push`, `worktree_create`, `worktree_remove` |

- New module `nexus-core/nexus/git_ops.py`, using `worktrees._git`, which reports stderr
  (`files._git` drops it). `worktrees.ENV` already sets `GIT_TERMINAL_PROMPT=0`.
- `worktree` must be a registered worktree of the project's repo. One lock per repo, as
  `worktree_lock` does.
- Timeouts: the existing 120 s, 300 s for network actions. The clients show progress and must not
  give up first.
- Add the route to the mutation matrix in `docs/api.md`. Like `POST …/worktrees`, it is an external
  effect: a crash leaves 409 `mutation_outcome_unknown`, and it is not retried blindly.
- These are control-plane routes for clients. No task tool or control tool reaches them.
- Events are `git.changed` and `files.changed` with no `session_id`, so nothing enters a session's
  log or timeline. Confirm this against `bus.py` when implementing.

### Credentials

Fetch, pull and push run as Core, using Core's environment. If Core was started without an SSH agent,
they fail, and the page shows Git's message. No credential UI in this plan.

## Live updates

`files.changed` is only published when a client saves, so the tree goes stale when the agent writes.

- Add a watcher in Core with `watchfiles` (`uv add watchfiles`), run while a client has the Files or
  Git tab open, on a 60 s lease the client renews.
- Debounce to 250 ms. Publish `files.changed` with `source: "disk"` and `git.changed` for changes to
  `HEAD`, `index` and `refs`. Skip Git-ignored paths.
- If the watcher can't start (for example, Linux inotify limits), fall back to a 3 s status poll while
  the tab is open, and show that the view is polling.

## iOS app gate

- NSH: add `desktopTools = !(native && touch)` in `store.ts`. It hides the Files and Git tabs and makes
  `editor.*`, `files.*` and `git.*` unavailable.
- This removes today's Files tab and read-only file view from the iPad and iPhone apps. Record it in
  the porting ledger.
- Check what idiom a Mac Catalyst build reports. It should keep these tools, since it is a desktop.
- Core routes are not gated. The rule is about what the app ships, and Core doesn't know the client.

## Steps

Each step lands in both clients together, with its docs, manifest entries and `PLAN.md` updated.

| # | Step | State | Done when |
| --- | --- | --- | --- |
| 0 | **Prove the risky parts.** Run NSH `Editor` and `DiffView` in the app on web and macOS with Monaspace Argon and a 1 MiB file. Slide the plain-view panel. Vendor the crate and render it in the TUI. | Done 2026-11-01 | Both show a real file; any blocker is recorded here |
| 1 | **Panel.** Move the current file view into the one panel, in both positions, with back, Esc, the waiting badge and the manifest change | Done 2026-11-01 (screenshots pending) | Screenshots at narrow and wide in both clients |
| 2 | **Editor.** Swap in both editors; add TUI movement, undo grouping, theme and find and replace; NSH find bar; fix the save-size bug | Next | Save, stale-file and reload tests pass in both, including the NSH browser paths `PLAN.md` lists |
| 3 | **Explorer and watcher.** File actions, NSH tree, watcher events | | An agent's write shows in the tree without a refresh |
| 4 | **Git page, read-only.** Read routes, the tab, and diffs in the panel | | All sections match `git` output on a repo with worktrees and remotes |
| 5 | **Git actions.** `git_ops.py`, the dialog and `worktree_live` | | Each action tested on a temporary repo with a bare remote, with and without a running session; stage and unstage run with no popup |
| 6 | **iOS gate and finish.** Gate, ledger rows, docs, remove the `PLAN.md` items this covers | | Tools absent in the iOS layout and present on macOS and web |

The Worktrees section of `PLAN.md` (review, merge, remove after merge) stays separate. It builds on
this page later.

**Step 0 notes.** The NSH `Editor` and `DiffView` run on the web and macOS hosts
(`nsh test --target web|macos editor.test.tsx`) with Monaspace Argon and a 1 MiB
file; no blocker. The vendored crate compiles against ratatui 0.29 with edition
2021 after rewriting 13 let chains; its own 19 tests pass. The TUI release
example is 9.9 MB with the 13 starting languages; the shipping binary is
unchanged at 13.0 MB until the editor is wired in. The TUI now renders the
vendored editor through `src/code_editor.rs` and `examples/editor_demo.rs`, with
a 1 MiB render test.

**Step 1 notes.** The panel is in both clients. NSH animates it in below
1100 px; the TUI appears at once (see the porting ledger). The narrow panel
covers the composer as well as the timeline. The waiting badge uses the viewed
session's approvals and questions. The manifest has `[navigation.panel]` with
`panel.file`/`panel.diff`; `editor.*` commands and the escape chain use
`panel.file`. The TUI binary's `PANEL_WIDE_FROM = 140` matches the manifest's
`wide_from`.

**Step 2 progress.** The save-size bug is fixed: Core allows an 8 MiB envelope
for `PUT /api/project/{id}/file` (`MAX_FILE_BODY_BYTES`) while the file content
limit stays 1 MiB, with a test for a just-under-1 MiB file of control
characters. The TUI adapter has Home/End, Page Up/Down, word movement and
forward delete, undo grouping (adjacent single-character typing or deleting
within 750 ms is one step) and a syntax/chrome palette built from the shared
theme tokens, with no hard-coded colours left in the crate. NSH has its find
bar (`cmd+f`, plain text, a case toggle, next/previous, count) with Esc closing
it before the panel. The TUI integration of the vendored editor (replacing the
`Composer` file buffer) and the TUI find bar remain.

## Testing

- **Core:** pytest on temporary repos with a bare remote, a scratch `$NEXUS10_HOME`, no network. Cover
  path confinement, a refused dirty worktree, unmerged branch delete, `--ff-only` on a diverged branch
  and `worktree_live`. Never run `nexus migrate`.
- **TUI:** unit tests for the vendored additions and key mapping; PTY screenshots at 80×24, 120×40 and
  160×48; `--bench` for typing on a 1 MiB file.
- **NSH:** `nsh test --target web` for the panel, editor, dialog and gate. NSH browser tests have a known
  mock-stream problem (`PLAN.md`, Browser tests), which may need fixing first.
- **Visual:** screenshot the TUI through xterm.js and NSH web, and compare for one design language.
- **Size:** report the TUI binary's size before and after each language added.

## Docs to update

`docs/api.md`, `docs/tools.md` (the freshness guard sees user edits; agent tools unchanged),
`docs/daemon.md`, `docs/development.md` (vendored crate), `docs/porting-ledger.md`,
`interface/manifest.toml`, `interface/design-language.md`, `interface/capability-matrix.md`,
`nexus-tui/NOTICE`, `nexus-nsh/README.md`, `PLAN.md`.

**Ledger rows to add:** the differences under [NSH editor](#nsh-editor); no Files or Git tab in the iOS
app; Git actions stay silent to the model, so a mid-turn branch switch updates files that the re-read
check catches, but the model may still believe it is on the old branch, an accepted exception to the
one-timeline rule.

## Risks

- **NSH components are unproven here.** Nexus hasn't used `Editor` or `DiffView`, and pinned `09c1632`
  isn't a published release. Step 0 covers this.
- **The vendored crate is young** and now ours to maintain. Upstream fixes must be pulled by hand.
- **Binary size:** about 9 MB more for the starting languages if sizes hold, taking the TUI from 13 MB
  to around 22 MB. The final language list isn't measured yet.
- **Permanent deletes.** Discard and file delete bypass the trash. The dialogs say so.
- **Core off loopback** serves without a token, per `docs/api.md`. These routes add discard, delete and
  push to that surface. The decision is to keep it, for trusted networks only.
