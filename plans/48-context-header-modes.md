# 48. Context header: an editable draft, then fixed

User request (2026-11-17): the context header has two modes.

1. **Draft** (a new session that does not exist yet): switches can be turned on and off, as today. Below the `total`
   row is a new switch: **start the session in a worktree**.
2. **Fixed** (from the first message on): the header shows what the session started with and offers nothing to
   change. The header loses its panel background to show this.

Decisions (user, 2026-11-17):

- **The worktree is a nudge, not something Core forces.** With the switch on, the model's context gets a visible
  instruction to create a new worktree and move into it before working. The model does this with its own control
  tools (`worktree`, `change_session_directory`) and chooses the branch name. Core never creates the worktree. This
  is the Manager's mechanism ([manager-design.md](../docs/manager-design.md) D3), and both now share it.
  (This replaces the first draft of this plan, in which Core created the worktree before the session.)
- **Outside Git, the worktree row is hidden.** It is not shown as disabled.
- **Switching stays possible, but only through slash commands.** A fixed header and its detail dialogs are read-only.
  `/skills` and `/mcp` are the only way to switch a started session's skills or servers. They keep today's PATCH and
  the visible JSON notice in the timeline.

Desktop/web, TUI and NSH in the same change ([parity skill](../.agents/skills/nexus-ui-parity/SKILL.md)). Mobile has no
context header, so only the slash commands reach it (mobile already gets every command, approved 2026-11-09).

## Today

- **Draft header.** TUI `draw_new_session_context` (`nexus-tui/src/render.rs` ~4046) draws a `PANEL` box whose
  switches are `Target::ContextToggle` / `ContextInfo`. Its rows are built by `paint::context_rows` and memoized by
  `App::preview_key`. NSH: `StartingContext` → `ContextSections` (`nexus-nsh/src/ui/newsession.tsx` ~395–432,
  `bg-card`), with `toggleDraftInventory`. Both clients take their figures from `POST /api/draft/context`.
- **Header after the first message.** The same box, built from timeline lines. TUI hit targets are at render.rs
  ~4640–4660 (`line.context_items` → `ContextToggle`). NSH: `RowView` → `StartingContext` with
  `toggleSessionInventory` (`nexus-nsh/src/ui/timeline.tsx` ~979). Both still switch skills and MCP, through a PATCH
  of `disabled_tools` ([tools.md](../docs/tools.md#skills-and-the-skill-tool)).
- **Workspace tools.** A new session has only `expose_workspace_tools`. `worktree` and `change_session_directory`
  appear once the model calls it ([tools.md](../docs/tools.md#workspace-control-tools-on-demand)).
- **Manager.** `start_session(worktree=true)` writes the worktree instruction into the first user message text.
- **No `/skills` or `/mcp` command** in either client.

## Plan

### Core

- [x] `POST /api/session` and `POST /api/draft/context` accept `worktree: true`. It applies to root sessions in a Git
  checkout only. Outside Git it returns 400, and the draft context reports `worktree: {available: false}`. The draft
  also reports `dirty` (uncommitted changes in the checkout), so the row can warn.
- [x] With the flag set, the session starts with the workspace tools already activated (`workspace.activated` in the
  snapshot, both schemas in the stored definitions). The nudge can then be acted on in the first call, and the
  header's Tools count shows the extra schemas. This is preview-honest: the draft context with the flag on returns the
  same tool list and token figures.
- [x] **The nudge.** A JSON notice is recorded in the context before the first user message, in the same form as the
  skill notices. For example: `{"start_in_worktree": {"repository": "...", "base": "<branch>@<short sha>", "note":
  "Before working, create a new Git worktree for this task with `worktree` (choose a short descriptive branch) and
  move into it with `change_session_directory`. Uncommitted changes in the checkout are not in it."}}`. The exact
  wording is settled in review and kept short. It goes to the model as a notice, separate from the user's message,
  and the timeline shows it as a notice row (plan 35 style) between the header and the first message.
- [x] **Follow-up check.** When the first turn ends, if the session's directory is still the main checkout, emit a
  visible `worktree.not_entered` notice for the user. The clients show it as a warning row. It is not sent to the
  model. The user can say so, or the Manager does, as its risk row already plans.
- [x] Manager: `start_session(worktree=true)` passes `worktree: true` instead of adding text to the first message.
  The timeline shows the same notice, labelled `via Manager`. Update manager-design D3/D7 and the risk row.
- [x] Tests (scratch `$NEXUS10_HOME`, temporary repo): the flag records the notice before the first message and
  activates the workspace tools; non-Git returns 400; the draft context matches the created session's first request;
  `worktree.not_entered` fires only when the directory did not change; the Manager path uses the flag.

### Clients: draft mode

- [x] Below `total`: a `Worktree` switch row in the same cell style as the other switches (`●` marker plus label),
  with the muted text `ask the agent to start in a new worktree`. It adds `· uncommitted changes stay in the
  checkout` when `dirty`. Hidden when `worktree.available` is false.
- [x] The state lives in the session draft next to `disabled_tools`. It is sent in the draft-context and create
  bodies, survives agent/model changes in the draft, and is off for each new draft. Keyboard: the TUI context cursor
  reaches the row; NSH uses a `role="switch"` button.
- [x] TUI: add the row to `paint::context_rows` (or draw it after them), include the flag in `preview_key`, and add a
  new `Target::ContextWorktree`. NSH: a row in `ContextSections` after `context-total`
  (`testID="context-worktree"`), only for a draft.

### Clients: fixed mode

- [x] Fixed starts at the first send, during `You · Sending…`
  ([first-prompt](../reports/first-prompt/README.md)), and stays fixed after `session.created`. On a create failure,
  the header goes back to draft along with the restored draft.
- [x] No panel background: TUI clears with the timeline background instead of `PANEL`. NSH drops `bg-card` for a
  `fixed` variant. Padding and widths stay the same, so nothing jumps.
- [x] No switches: the TUI stops registering `ContextToggle` for timeline lines and draws switch markers as plain
  state (on, or struck through for off). NSH passes no `onInventoryToggle`, and `Section` renders static markers.
  Headings and names still open their detail dialogs, which become read-only. Their checkboxes become state marks,
  and the dialog hint says `/skills` or `/mcp` switch them.
- [x] Below the fixed header, the timeline shows the `start_in_worktree` notice row, the user's message, the model's
  worktree calls, and `session.directory_changed` (already shown), all in order. A `worktree.not_entered` warning row
  appears when the check fires.

### Slash commands (`/skills`, `/mcp`)

- [x] `/skills` and `/mcp` open the existing inventory dialog in switch mode for the current session (the skill
  cards in NSH and the inventory dialog in the TUI). Same PATCH, `locked` rules and timeline notice as today. In a
  draft they switch the draft, as the header does.
- [x] Manifest entries and command help in both clients. Mobile reaches them through its command list, using the
  compact dialog.

### Docs, tests and evidence

- [x] Docs: api.md (`worktree` on create and draft, the notice, `worktree.not_entered`), tools.md (the workspace
  tools start active with the flag; switches now come from `/skills` and `/mcp`, and the header is read-only once
  started), stream-events.md (the notice), manager.md/manager-design.md, both client READMEs,
  `interface/design-language.md` (two header modes).
- [x] Porting ledger row: the header no longer switches mid-session.
- [x] Tests: TUI render tests (draft with the worktree row, row hidden outside Git, fixed with no `PANEL` background
  and no toggle targets, the notice row in the timeline). NSH web tests, the same four. A first-send test in which a
  create failure goes back to draft. `/skills` and `/mcp` command tests in both clients.
- [ ] Paired screenshots (TUI and NSH web) of draft, draft with the worktree on, and fixed with the notice, in
  `reports/48-context-header-modes/`.

## As built (2026-11-17)

Everything above landed except the paired screenshots. Where the build differs from the text above:

- **The nudge is a text notice, not JSON.** A user-role message starting `[Nexus notice: start in a worktree]`
  (`runtime.worktree_notice`), the same form as the other `[Nexus notice: …]` rows both clients already show as
  notice rows. It names the base (`branch at sha`), the repository and the number of uncommitted changes.
- **No `worktree.not_entered` event.** The session JSON carries `worktree_request` with `state`
  `requested` / `entered` / `not_entered`; `session.updated` is published when a turn ends with the request unmet.
  Both clients show it as one line under the fixed header (`⌥ new worktree requested · not entered` as a warning).
- **TUI target:** the worktree switch is `ContextToggle(context_items().len())`, not a new `Target`.
- **Draft answer** carries `worktree: {available, on, repository, checkout, branch, head, dirty, changes}` and,
  when on, `notice`. The dirty line is its own row with the count (`2 uncommitted changes stay in the checkout`).
- **While sending** the switch, if on, shows as the fixed line `⌥ new worktree requested`.

Tests: `tests/test_workspace.py` (plan 48 block), `tests/test_manager_authority.py`, TUI `tests/behaviour.rs`
(`draft_worktree_*`, `the_header_is_fixed_from_the_first_send`, `started_header_is_fixed_and_slash_skills_switches_the_session`,
`fixed_header_shows_the_worktree_request_state`), NSH `context-modes.test.tsx` and `first-prompt.test.tsx`.

## Settled questions (user, 2026-11-17)

1. **The branch name is left to the model.** The nudge does not suggest a prefix.
2. **The switch is not remembered.** It is off for every new draft, with no per-project memory.
3. **No extra re-read of instructions.** The frozen system prompt and AGENTS.md come from the main checkout, where
   the session starts. When the model moves, the `change_session_directory` announcement already gives it the old
   and new paths, the branch and repository, and the worktree's own `# Instructions file` section, read from the new
   directory. It also says that later calls resolve from there ([tools.md](../docs/tools.md#change_session_directory-directory)).
   The docs say this; no new mechanism is needed.
