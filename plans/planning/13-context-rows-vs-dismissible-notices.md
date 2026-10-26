# Plan 13: Separate "what the agent sees" from dismissible notices (Ratatui)

Status: proposed (2026-10-25). Nothing in this plan is implemented. Source: root `TODO.md` item 6,
quoted unchanged:

> - "Notice" needs to be better. We need to classify that there are two things that will be on the screen. One is we are showing the user one-to-one mapping of the context of the agent. Second one, things like notice should appear in a box, and there should be an X button to close them.

## 1. Intent

Every row on the conversation screen belongs to exactly one of two classes, and looks like it:

1. **Context rows**: a one-to-one rendering of something in the agent's context (a message, tool
   call, tool result, injected notice the model reads). Never dismissible, never hidden: this is the
   Nexus principle "nothing the agent can see is hidden from the user" (`AGENTS.md:12-15`,
   `docs/README.md:9-11`).
2. **Notices**: information for the user only, which the model never sees. Drawn in a bordered box
   with an `×` in the top-right corner; clicking `×` hides it.

### Non-goals

- Toasts already exist, are dismissible and float over the screen (`docs/ratatui-parity.md:1336-1365`);
  they are not changed here. This plan is about rows *in the transcript*.
- Desktop paused; web deprecated.
- No change to what the model receives.

## 2. Current state (checked 2026-10-25)

All transcript "Notice"-style rows come from one place, `project()` in
`nexus/ui/ratatui/prototype.py`:

- `context.notice` events kept in `view.diagnostics`, placed by time among turns, drawn as
  `kind: "literal"` blocks titled `Notice` (or `Subagent reports paused`, `Tool-call wrap-up
  reminder`) (`prototype.py:1232-1251`).
- Worktree moves (`view.worktree.moves`) drawn as `Worktree` literal blocks in the same list (`:1245-1248`).
- Projection failures and `error` drawn as a `Notice` block at the end (`:1315-1319`).

Those `context.notice` events are **not all the same kind of thing**:

| `kind` | Model sees it? | Emitted at |
| --- | --- | --- |
| `tool_call_nudge` | **Yes**: paired user message with `MessageMeta.notice` in the same batch | `nexus/session/session.py:1727-1750` |
| subagent roster change | **Yes** (paired; baseline event alone is not) | `session.py:1752-1806` |
| worktree model notice (`_NOTICE_KIND`, e.g. move failed) | **Yes** (paired) | `nexus/worktree_sessions.py:1124-1128` |
| `worktree` (e.g. "Merged into … @ sha", "Worktree action failed") | No, event only | `worktree_sessions.py:131-132`, `:550`, `:571` |
| `subagent_<state>` (child merge result) | No, event only (its `files` feed commits, `:594-595`) | `nexus/agents/session_subagents.py:96-98` |
| `shell_wake_paused` | No, event only | `session.py:1626-1632` |
| `session_root_cwd` | No, event only | `nexus/runtime.py:4775-4777`, `:4831` |
| projection failure / `error` | No, client-only | `prototype.py:1315-1319` |

So today a model-visible notice and a UI-only notice render identically, and nothing can be closed.
Not verified: whether the paired model-visible **message** of a notice also renders as a user card
somewhere (check `_is_prompt` and the reducer's handling of messages with `meta.notice` before
implementing; if it does, the context row and the notice row are double-drawn).

## 3. Assumptions

- A1. "one-to-one mapping of the context" means: if the model read a piece of text, the user sees
  that text as a context row, in order. A model-visible notice is therefore a **context row**, not a
  closable box.
- A2. Closing a notice is a per-client viewing choice; it does not change the log.

## 4. Design

### 4.1 Classification (durable, not guessed by the client)

- Add `"model_visible": true` to the `context.notice` event data whenever a paired
  `MessageMeta.notice` message is written in the same batch (three emitters above). Absent means
  `false`.
- Legacy logs have no field: the reducer (or the projection) treats the known paired kinds
  (`tool_call_nudge`, the roster kind, the worktree model-notice kind) as model-visible when the
  field is missing. Keep that list in one constant next to the emitters' kinds so it cannot drift.
- `docs/events-and-view.md` documents the field; `docs/context.md` states that model-visible
  notices are context rows.

### 4.2 Projection (`nexus/ui/ratatui/prototype.py`)

- Model-visible notice → block `kind: "context_notice"` (drawn like other context rows: labelled,
  not boxed, no `×`), title from the existing title map.
- UI-only notice (event-only kinds, worktree moves if Q1 says so, projection failures) → block
  `kind: "notice"` with `dismiss_operation: {"kind": "notice_dismiss", "id": "<session>:<seq>"}`.
  Add that operation to `allowed_operations` (`prototype.py:1599-1606`) so the bridge accepts it.
- Dismissed ids are filtered out before projecting. Projection failures use a synthetic id
  (`failure:<digest>`), so a new failure shows again.

### 4.3 Persistence of dismissals

- Store dismissed ids in the terminal preferences (`nexus/ui/ratatui/preferences.py`, new key
  `dismissed_notices`, list of strings, bounded to the newest 500, per `(workspace, session, seq)`).
  This survives a client restart without a host change. A second client still shows the notice
  (A2).
- Alternative (not chosen): a host command writing a durable `notice.dismissed` event. Rejected
  because a viewing choice would then enter the session log that the model's context is built from.

### 4.4 Rust drawing

- `kind: "notice"`: a rounded/plain border box (`Block::bordered()`, `p.border`), title on the top
  border, `×` at the top-right inside the border, three-cell hit target (the header hit-target
  convention, `docs/ratatui-parity.md` native chrome section). Hover and pressed styles as other
  buttons. Click `×` → send the block's `dismiss_operation`. Keyboard: when the block is focused
  (transcript block focus exists), `x` or `Delete` dismisses.
- `kind: "context_notice"`: the existing literal-row style with a muted `context` label so it reads
  as part of the agent's input.
- Layout shared between draw and hit-test (one function returning cells), as in `render/chrome.rs`.

## 5. Implementation steps

1. Verify the double-draw question in §2; fix in the projection if needed.
2. Emitters add `model_visible` (three call sites) + legacy kind list + docs.
3. Projection split + operation + preference key.
4. Rust box drawing, `×` hit-test, keyboard dismiss.
5. Docs: `docs/ratatui-parity.md` (transcript rows), `docs/events-and-view.md`, `docs/context.md`,
   a `docs/decisions.md` entry (classification rule and why dismissal is client-local).

## 6. Tests and acceptance

- Python: each emitter writes `model_visible` correctly; legacy event without the field is
  classified by kind; projection produces `context_notice` vs `notice`; a dismissed id is filtered;
  preference list is bounded; replay after reconnect keeps the classification.
- Rust: box drawing (border, title, `×` position at several widths, ASCII mode); click on `×` sends
  the operation; click elsewhere on the box does not.
- PTY: a UI-only notice shows a box, `×` removes it, restart keeps it removed.
- Acceptance:
  - [ ] Model-visible notices are never closable and read as context.
  - [ ] UI-only notices are boxed with a working `×`; closing survives a restart on that client.
  - [ ] No model-visible text is hidden from the user.

## 7. Risks and open questions

- Q1. Are worktree move rows (`Worktree`) UI-only notices? The agent gets its own model notice for
  moves (`_move_text` → model notice), so the move row duplicates it for the user. Default: treat
  the move row as a context row when a paired model notice exists for the same move, otherwise a
  notice.
- Q2. Should dismissing be global (all clients)? Default: per client.
- Risk: a user may close an error-ish notice ("Worktree action failed") and miss it later; the same
  information is in the Logs drawer and the details sidebar, so it is not lost (state that in the
  docs).
