# Interface manifest

`manifest.toml` is the single machine-readable source for the screen map,
commands and settings that the TUI (`nexus-tui/`) and NSH (`nexus-nsh/`) both
build from. The visual rules are in [design-language.md](design-language.md); the work
order is [plans/](../plans/README.md). The former prose spec is archived
(`git show fdb1860:INTERFACE_PLAN.md`). If the manifest and the design language
disagree, fix both together.

Validate after every edit:

```sh
python3 interface/check_manifest.py     # stdlib only, Python 3.11+
```

The check covers unique command ids and slash names (aliases included), no key
conflict within one `key_context` per client, `when` terms from the vocabulary,
placements, categories and statuses, the exact settings section order, unique
settings row ids, the navigation anchors and every cross reference to a command.

`/close` closes the current session tab without archiving or deleting the session.
`/archive` archives an idle session tree and archive events close all affected open tabs,
including when another client or Core initiated the archive. `/search [text]` opens the
sessions switcher with its search field focused (the Sessions list on mobile).

## Schema

`[vocabulary]` declares the allowed `terms`, `placements`, `statuses` and
`categories`. Add a term there before using it.

### `[[command]]`

| Field | Meaning |
| --- | --- |
| `id` | Stable dotted id (`session.archive`). Never reused. |
| `title` | Palette label, sentence case. |
| `category` | Palette grouping. |
| `slash` / `slash_aliases` | Composer `/name` (lower-case letters, optionally separated by hyphens). Same command, same behavior. |
| `keys` | `{ tui = "ctrl+p", nsh = "cmd+p" }`. Either may be absent (unbound by default). |
| `key_aliases` | Other spellings of the same key, per client: ``{ tui = ["ctrl+shift+`"], nsh = ["ctrl+shift+`"] }``. Use when a medium reports one physical key two ways; aliases resolve to the same command and take part in conflict checks. |
| `key_context` | Scope for key conflict checks (see `[keymap] contexts`). |
| `when` | Availability: a list of terms, all must hold. `"!term"` negates. |
| `unavailable_reason` | Shown beside the disabled command; required when `when` is not `["always"]`. Unavailable commands are disabled with this text, never hidden. |
| `placement` | Where it appears: `palette`, `row_menu`, `header`, `composer`, `sidebar`, `timeline`, `tool_line`, `card`, `toast`, `banner`, `dialog`, `drawer`, `editor`, `settings`, `menu` (native desktop menu). Every command is also reachable from the palette unless the note says otherwise. |
| `clients` | `both` (default), `tui` or `nsh`. |
| `status` | Absent: Core API ready. `blocked_core`: needs a Core increment (Steps 11/11a/12a). `deferred`: deliberately not built. Clients show the unavailable reason. |
| `note` | Behavior notes for implementers. |

Key syntax: lower case, `+`-joined, modifiers in the order `cmd ctrl alt shift`,
then the key (`cmd+shift+n`, `alt+enter`, `esc`). `cmd` is only valid in
`keys.nsh` and maps to `ctrl` where NSH runs on a non-Mac platform. Users can
rebind keys in Settings > Keybindings (view setting, per client); the manifest
holds defaults only. For the tilde key, write the physical key as
``ctrl+` `` and its Shift spelling as ``ctrl+shift+` ``; `key_aliases` carries
the other form a terminal or browser reports.

### `[[settings.section]]` and `[[settings.section.row]]`

Section order is fixed: Appearance, Providers, Agents, Tools, MCP, Context,
Voice and speech, Keybindings, Notifications, About. Both clients draw Settings
as a left section rail plus the selected section in the right pane. Row fields: `id`
(prefixed by the section id), `label`, `kind` (`choice toggle number text list
action info keymap`), `scope` (`core`: read/written through the Core API;
`client`: per-client view state), `clients` (`both`, `tui`, `nsh`), optional
`default`, `options`, `command` and `note`. Do not ship a row whose capability
does not exist yet: remove it instead, and add it back with its control. Row
order within a section is the display order in both clients; client-only rows
(TUI monochrome and terminal notifications) are inserted in place and labelled,
never reorganizing the section.

### `[navigation]`

Header anchors (left toggle leading, title/breadcrumbs centre, right toggle
trailing), the project rail, left tabs (Sessions, Files; Settings fixed at the bottom),
right section (Session info), centre views (`session`,
`new_session`, `editor`, `settings`), the waiting card (above the composer) and
narrow-layout drawer behavior.

## `when` vocabulary

Terms are evaluated by each client from Core state plus view state.

| Term | True when |
| --- | --- |
| `always` | Always. |
| `has_project`, `has_session`, `has_turns` | A current project, a session in the centre column, the session has at least one turn. |
| `session.idle` | Viewed session has no running turn. |
| `session.busy` | Viewed session has a running turn. |
| `session.interrupted` | The last turn was interrupted or failed, and Core has reconciled incomplete calls (`/continue` is offered). |
| `session.archived`, `session.child`, `session.in_worktree` | Viewed session is archived, is a child session, lives in a worktree. |
| `session.linked_worktree` | Viewed session's directory matches a linked (non-main) worktree in the existing worktree listing. |
| `has_waiting` | Any approval or question waits anywhere (this session, its children, other sessions). |
| `waiting.approval`, `waiting.question`, `waiting.multiple` | The focused waiting item is an approval / a question; more than one item waits. |
| `composer.has_text`, `composer.empty`, `has_queued` | Composer content; queued messages exist. |
| `timeline.scrolled_up`, `timeline.has_older`, `tool.output_clipped_in_ui` | Following suspended; unloaded older turns; UI (not model) clipping. |
| `focus.*` | The named item has keyboard focus or hover/selection (`turn`, `tool`, `user_message`, `assistant_block`, `code_block`, `file`, `project`, `session_row`, `child_card`, `git_file`). |
| `panel.file`, `panel.diff`, `view.settings`, `editor.dirty` | The right-hand panel shows a file or a diff; the settings view; editor state. |
| `overlay.open`, `dialog.open`, `alert.open`, `drawer.open`, `layout.narrow`, `text_edit.active` | Transient UI state; `text_edit.active` is an inline text field other than the composer. |
| `connection.lost`, `connection.no_daemon` | Event stream dropped; no daemon found. |
| `provider.signed_in`, `has_notifications` | At least one provider signed in; the notifications drawer is not empty. |

`session.idle`, `session.busy` and `session.interrupted` are about the viewed
session only; archive checks on descendants are enforced by Core and surface as
its refusal text.

## Esc precedence

Esc is not a per-command lookup. `[[keymap.escape]]` is an ordered list; the
first rule whose `when` holds consumes the key and the walk stops:

1. `overlay`: palette, menu, popup, completion closes.
2. `dialog`: a confirm dialog cancels.
3. `text_edit`: inline rename, find bar, settings field or key capture cancels.
   Esc never clears composer text.
4. `drawer`: a narrow-layout drawer closes.
5. `alert`: a focused toast or banner is dismissed (it stays in the drawer).
6. `interrupt`: a running turn in the viewed session is interrupted. One press
   interrupts only; it does not also navigate back.
7. `editor`: the built-in editor closes (asks if dirty).
8. `back`: a child session returns to its parent. Backspace does the same when
   no text field has focus.
9. `settings`: the settings view returns to the previous view.
10. `refocus`: move focus to the composer.

So text editing and dialogs beat interrupt, interrupt beats back navigation.
Every command bound to `esc` has `key_context = "escape_chain"` and must be the
`action` of exactly one rule; the check enforces this.

## How clients consume it

- **TUI:** `include_str!("../../interface/manifest.toml")` parsed with `serde` and
  `toml` at startup, into a command registry (palette, slash completion, key
  hints, availability) and the settings and navigation trees. `theme.toml` is
  embedded the same way. Generated code is not needed. A unit test runs the same
  invariants as the checker.
- **NSH:** `nexus-nsh/scripts/gen.py` converts this file and `theme.toml` into
  `src/gen/manifest.ts`, `src/gen/theme.ts` and `gen/theme.json` at build time.
  The app imports those; ids and labels are never copied by hand.
- **Renderers stay uncoupled.** Only data is shared: ids, labels, order,
  placement, defaults, availability terms and reasons. Each client evaluates the
  `when` terms itself.
- Platform adaptations are allowed in key modifiers, native menus and window
  chrome; they never rename or relocate a command.

## Stability rules

- Ids (commands, settings sections and rows, navigation ids) are stable.
  A rename, removal or move needs a ledger note in docs/porting-ledger.md and a migration for
  saved keybindings and settings.
- Add a command or row by appending in its intended position; do not reorder
  shared rows without a ledger note.
- Labels and notes may be edited freely; ids may not.
