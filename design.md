# Nexus web app design

## Purpose

Define an implementable visual and interaction system for the Nexus browser client: a dark-first, low-noise coding-agent workspace with a session sidebar, readable transcript, pinned composer, and contextual inspector. The same layout supports light appearance and three user-selectable presentation-detail levels.

This document extends the current browser surface described in [`webplan.md`](webplan.md). It is a UI specification, not a change to session, daemon, permission, or model semantics.

## Principles

1. **Conversation first.** Keep the active prompt, assistant response, and current work at the center of attention.
2. **Progressive disclosure.** Three exact detail levels change presentation only. Every available durable item remains inspectable on demand.
3. **Quiet, legible surfaces.** Use spacing, type hierarchy, and a small number of opaque surfaces; reserve color for state and action.
4. **Stable during live work.** Streaming updates existing keyed content, preserve scroll and expansion state, and never reorder activity to make it look tidier.
5. **Trustworthy controls.** Approval, running, failure, and connection state are explicit and actionable; no decorative controls imply unavailable behavior.
6. **One shared reality.** Render the host's authoritative view projection; do not recreate session state or infer permissions in the browser.

## Non-goals

- No background image, wallpaper, simulated desktop/window chrome, traffic-light dots, or decorative gradients.
- No glass-heavy panels, blur as a defining surface, ornamental charts, excessive badges, or ambient animation.
- No browser-side model reasoning, permission evaluation, tool execution, repository inspection, or event reduction.
- No claim to provide a repository-wide Git/worktree changes panel today. The inspector may display tool diffs already exposed by the host. Aggregate branch/repository review waits for a shared host contract.
- No presentation preference that filters, deletes, rewrites, exports selectively, or changes durable Nexus data.
- No display of model reasoning that is unavailable from the host projection. When exposed, reasoning means provider-supplied thinking content only; it is not a chain-of-thought feature.

## Information architecture

### Primary regions

| Region | Purpose | Current architecture fit |
| --- | --- | --- |
| Session sidebar | Create, search, and switch workspace sessions; show activity and connection | `#sidebar`, session list, workspace feed |
| Main workspace | Session header, chronological transcript, pinned composer | `.main-pane`, `#timeline`, `#composer-form` |
| Context inspector | Overview, tools, agents, selected tool/diff, or child-agent transcript | `#inspector`, inspector tabs/content |
| Overlays | Command/session search, model/agent selection, approval decision | Existing dialog/overlay pattern; approval remains attached to active workspace |
| Settings | Appearance, browser/workspace detail defaults, layout preferences, shortcuts | Browser-local presentation settings; no host command |

Keep session navigation and conversation as the persistent shell. The inspector is optional context, not a second required workflow. On narrow screens it becomes a drawer/overlay; opening it must not replace or mutate the active session.

### Responsive layout

Use CSS Grid and media/container queries; do not add a layout framework. Widths below are CSS pixels at 100% zoom.

| Viewport | Sidebar | Main transcript | Inspector |
| --- | --- | --- | --- |
| Large: ≥1280 px | Persistent, 232–280 px; default 256 px | Flexible, minimum 520 px; readable content max-width 800 px | Optional persistent column, 320–400 px; default 344 px |
| Medium: 960–1279 px | Persistent, 224–256 px | Flexible, minimum 520 px | Closed by default; 320–380 px overlay drawer when opened |
| Compact: 700–959 px | 56–64 px icon rail or 280 px drawer | Full remaining width; max-width 800 px | Full-height overlay drawer, max-width 400 px |
| Narrow: <700 px | Session list is a drawer; conversation is single-column | Full width; 16 px side padding; 44 px minimum controls | Full-screen inspector overlay with visible Back/Close control |

At 200% zoom, follow the resulting CSS viewport: reflow to the narrow layout rather than introducing horizontal page scrolling. Keep the composer visible and usable when the virtual keyboard is open. Preserve transcript scroll offset per session and follow the tail only when the reader was already within 96 px of the bottom.

These breakpoints intentionally supersede current `app.css` values (1180 px and 740 px) and current 272/310 px desktop columns. Migrate those rules to the table above and test the 959/960 px and 1279/1280 px boundaries, as well as 699/700 px, at normal and 200% zoom. Do not retain conflicting legacy media queries.

Use a 48–52 px desktop top bar and 52–56 px narrow top bar. Sidebar and inspector dividers are 1 px. Pane resizing, if present, has a 6 px hit target and a 320 px transcript minimum; widths persist locally per browser/workspace. The conversation content column stays centered independently of sidebar/inspector width.

## Visual system

### Surface and color tokens

Use opaque, role-based tokens and independently tuned themes. Keep the current `tokens.css` names where they fit; values below are practical starting values, not a mandate to copy existing colors exactly. Accent is for focus, selection, and primary action; do not color every active item.

| Token role | Dark example | Light example | Use |
| --- | --- | --- | --- |
| `--canvas` | `#171817` | `#f6f5f2` | Main workspace background |
| `--sidebar` | `#141514` | `#eeede9` | Session navigation surface |
| `--elevated` | `#1e201e` | `#fffefa` | Cards, composer, inspector sections |
| `--hover` | `#282a27` | `#e9e7e2` | Hover and selected-neutral state |
| `--border` | `#343633` | `#dedbd5` | Standard separator and outline |
| `--border-strong` | `#4a4d48` | `#c9c6bf` | Resizer, active input boundary |
| `--text` | `#e8e9e5` | `#282a27` | Primary text |
| `--muted` | `#a4a79f` | `#6d706a` | Secondary text |
| `--quiet` | `#7b7f77` | `#8b8d86` | Timestamps and low-priority metadata |
| `--accent` | `#e49a72` | `#b95732` | Focus, primary button, current selection |
| `--accent-soft` | `#34271f` | `#f5e7df` | Low-area accent tint |
| `--success` | `#83c99a` | `#28754b` | Completed/allowed state |
| `--warning` | `#e2b466` | `#996315` | Running/approval-needed state |
| `--danger` | `#ef887c` | `#b63f36` | Failure/denial/cancel state |
| `--code` | `#20221f` | `#f0efeb` | Code blocks and diff backing |
| `--diff-add` | `#203629` | `#e4f2e7` | Added lines, plus explicit `+` marker |
| `--diff-remove` | `#3b2725` | `#f8e7e3` | Removed lines, minus explicit `−` marker |

Preserve existing `--canvas`, `--sidebar`, `--elevated`, `--hover`, `--border`, `--border-strong`, `--text`, `--muted`, `--quiet`, `--accent`, `--accent-soft`, `--accent-ink`, `--success`, `--warning`, `--danger`, `--code`, `--shadow`, `--ease`, and `--fast` names. Add only missing role tokens such as `--diff-add` and `--diff-remove`; no renaming/migration of current variables is intended. Keep `data-theme="system|light|dark"`, honor `prefers-color-scheme` only for `system`, and declare `color-scheme` to match the resolved theme. Text, muted labels, and status text must meet WCAG AA contrast against their actual surfaces. Never rely on color alone to communicate state.

**Background rule:** the app canvas and all panes use solid theme colors. Do not use a background image, remote image, wallpaper, texture, or decorative gradient. Images are allowed only as explicit conversation content when returned by a supported host projection.

### Type, spacing, shape, and depth

- UI font: `-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`; code/path font: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`. No remote fonts.
- Body/transcript: 14–15 px, line-height 1.5; headings: 17–20 px; section labels: 11–12 px; timestamps and metadata: 12 px minimum. Code: 12.5–13.5 px with 1.5 line-height.
- Transcript paragraph width: 60–82 characters where possible, maximum 800 px. Preserve readable wrapping for long paths and unbroken output.
- Spacing scale: 4, 8, 12, 16, 24, 32, 40 px. Use 8 px as the base rhythm. Transcript message gaps: 24 px; activity card internal padding: 12 px; composer padding: 12–16 px.
- Radii: 6 px controls, 8 px compact cards, 12 px composer/large cards, 14 px modal sheet. Avoid pill-shaped containers except small status chips.
- Borders: 1 px solid token; do not outline every paragraph. Shadows only on overlays/drawers: one soft, low-opacity shadow, no stacked glow. Main panes remain flat and opaque.
- Icons: local inline SVG or existing local icon assets, 16–18 px, 1.5–2 px stroke, consistent optical alignment. Pair ambiguous icon-only actions with an accessible name and tooltip; do not use emoji as status icons.

### Motion, focus, and selection

- Standard state transition: use existing `--fast` (160 ms) and `--ease` tokens for opacity/transform. No spring/bounce, parallax, shimmer, or animated background; add no competing motion-token names.
- Streaming updates text in place. Do not animate card height on each token, flash changed cards, or auto-scroll users who have scrolled away.
- Honor `prefers-reduced-motion: reduce`: remove nonessential transitions and smooth scrolling; never use motion as the only status signal.
- `:focus-visible` is a 2 px accent outline with 2 px offset and at least 3:1 contrast against adjacent color. Never remove browser focus without replacement.
- Text selection remains native and visible in both themes. Diff line selection/copy must not disable ordinary transcript selection.

## Component specifications

### Top bar

- Height 50 px desktop, 54 px narrow. One-line session title at 17–18 px medium weight; secondary workspace/session metadata at 12 px muted.
- Right actions: connection/turn state, model and root-agent selectors, inspector toggle, session menu. Hide secondary labels before truncating the title; on narrow view retain title, navigation, and a labeled overflow menu.
- Show workspace path/branch only when returned by the host. Separate “effective model” for the running turn from “selected for next turn” when both are known. Never fabricate context percentage, branch, cost, or connection certainty.
- Use a small dot plus text (`Working`, `Needs approval`, `Live`, `Offline`); icon/color is supplementary.

### Sidebar and session rows

- Sidebar width 256 px default. Header has product/workspace identity and a real collapse button; **no simulated window controls**.
- “New session” is a full-width 38 px button. Search/command actions are labeled or have tooltip and accessible name. Session list scrolls independently; footer shows connection text and Reconnect when appropriate.
- Each row is at least 40 px high (44 px narrow), with title, optional short ID/recency in the accessible description, and a distinct running/approval/unread marker. Do not put three-dot controls in the click target without keyboard access.
- Current session uses a restrained accent edge or surface tint, not a full saturated row. Running and pending approval include visible text/shape at accessible name level.
- Empty workspace: one short “Start a session” explanation and New session action. Loading list: 3–5 static skeleton rows, announced as loading, with no pulsing if reduced motion is set.
- Existing session operations only: open, fork, export, delete as available. Do not add a Git/worktree affordance without host support.

### Transcript, user prompt, and assistant prose

- Timeline is a chronological list (`role="log"` with non-chatty live announcements). Each message is keyed by durable `message_id` and turn ID. Preserve IDs and DOM nodes across projection patches.
- User prompts use a subtle `--elevated` card, right-aligned only where there is ample width; on narrow screens they are full-width. Label “You” and keep prompt text selectable. No avatar decoration required.
- Assistant prose stays left aligned and unboxed on the main canvas. Label the assistant/model quietly; render the documented safe Markdown subset (paragraphs, headings 1–3, unordered lists, bold, emphasis, and fenced/inline code) while escaping raw HTML. Links are displayed as text; no browser navigation is synthesized from model output. Unsupported Markdown remains readable text.
- Render streaming content by updating text/Markdown nodes safely; no transcript-wide `innerHTML` replacement on each patch. Keep final text deduplication and content reconciliation owned by projection IDs.
- Timestamp and model/stop metadata appear in a quiet footer or on focus/hover; never make essential chronology hover-only. At Complete, timestamps are visible without hover.
- Empty conversation shows one short prompt invitation, up to three working suggestion buttons, and a shortcut hint. Suggested prompts fill the draft; they do not send automatically.

### Activity and tool groups

- Individual activity card shows actual tool name, status label, optional elapsed duration/progress, and concise host-provided preview. Status uses text (`Running`, `Completed`, `Failed`, `Waiting`) as well as icon/color.
- Expandable detail exposes all available sanitized input parameters, progress entries, result/summary, error, and diff content subject to host bounds. Never claim the displayed result is complete when host clipping metadata says otherwise.
- Keep approvals outside generic aggregation and visible as a first-class interrupt. Failed calls remain individually identifiable and cannot disappear into a successful summary.
- Copy parameter/result/diff controls act only on the displayed host projection and expose a success/failure announcement.
- “Read”, “Search”, “Ran”, and “Edited” wording is allowed only when tool identity and available metadata justify it. Otherwise use the actual tool name (“Used `ToolName`”) rather than guessing.

### Nested agents

- A Task card shows agent type/name, requested task, state, and nesting relationship. Clicking/keyboard activation opens that agent’s live transcript in the inspector with parent breadcrumb and a visible Back action.
- Nested agents stay associated with their initiating task and are never merged with parent tool summaries. Detail level changes preview/metadata only; it does not remove agent identity, status, or access to the child transcript.
- If host projection omits child text or parameters, show the available state and do not synthesize missing output.

### Composer

- Pin below the transcript in normal document flow; do not overlay messages. Rounded 12 px opaque surface, 1 px border, visible focus ring, multiline textarea, and a separate bottom action row.
- Textarea target height 42 px minimum; expands to 160 px then scrolls. At narrow widths controls remain reachable above the on-screen keyboard. Minimum button hit size 44×44 px on touch layouts and 36×36 px otherwise.
- Enter sends; Shift+Enter inserts newline; do not send during IME composition. Preserve draft per workspace/session in browser session storage. On failure keep the text and explain the redacted host error.
- Show model and agent chips, context only when actual values exist, and one clear primary action. During an active turn show Stop; sending remains disabled unless a separately labeled queue action is implemented. Never silently reinterpret Send as queue.
- No attachment, voice, image, queue, or command icon unless its action is implemented and host-backed.
- Queue next prompt is outside this visual pass and is not currently presented as a composer action. Preserve `webplan.md` parity requirement: if added later, expose a separately named, host-backed “Queue next prompt” action with explicit cancel/drop semantics; never silently turn Send into queueing.

### Inspector and diff view

- Tabs: Overview, Tools, Agents. Current session details are derived from host projection. Selecting a tool opens its details/diff without claiming repository-wide change coverage.
- Overview includes available session phase, model, usage, context/compaction, queue, pending approvals, presence, and agent tree. Omit unknown values rather than display zeros/defaults that look authoritative.
- Tool list order is chronological; selecting an item keeps the matching timeline activity available. An agent view uses parent/child breadcrumbs and does not silently change active session URL.
- Diff view uses selectable monospace text, line numbers when supplied, and explicit `+`/`−` markers plus green/red-tinted backgrounds. Provide unified/split only if the host diff supports it; default unified. Label file path exactly as returned, escape it, and show “Diff clipped” when indicated.
- At Complete, show every edited line/diff hunk the host projection exposes. “Every” is bounded by the host’s security, response, and projection limits; never fetch arbitrary files from a browser path or imply omitted lines were inspected.
- No Git tab, branch changes summary, staged/unstaged groups, commit, or revert controls until the shared host contract described as deferred in `webplan.md` exists.
- Workspace tools (session trash/restore, model catalog/refresh, agents/tools/extensions/doctor utilities) are not part of this chat visual pass and are not implied to exist in the current browser. Keep them in a future secondary Workspace tools area/route, backed by host commands as required by `webplan.md`; do not crowd the session sidebar or inspector with placeholders.

### Approval sheet

- Treat approval as a blocking decision, not a toast. Present tool, requested action/scope, requesting agent where present, and exact safe preview supplied by host.
- Four explicit choices: Allow once, Allow for session, Deny once, Deny for session. If persistent choice is unavailable, relabel to the accurate one-time action; never imply an unavailable grant.
- Use a centered 440–560 px modal on desktop and bottom/full-width sheet on narrow screens, opaque surface, clear title and focus boundary. Default focus to Deny once (the safe choice); Escape is equivalent to Deny once and is announced.
- Reconstruct pending UI from durable pending-permission projection after reconnect. Dismiss only after confirmed durable resolution or explicit denial. If another view wins, show “Answered in another view”; if network fails, keep state and offer retry/reconnect without claiming a decision.

### Settings and detail selector

- Provide a Settings dialog/section with Appearance (`System`, `Light`, `Dark`), **Detail level** (exactly three options below), pane widths where supported, and keyboard help.
- Radio option names are exactly **Focused**, **Balanced**, and **Complete**. Under Balanced, show “Recommended” as supporting text, not part of its accessible name. Supporting descriptions: Focused — “Keep activity compact; expand any item for details.” Balanced — “Show useful activity summaries with details one click away.” Complete — “Show all detail available from this session.”
- The normal selector opened from an active session edits that session’s override. Include a “Use workspace default” action that clears only this session override. Settings separately configure the workspace default and browser default; both are browser-local presentation preferences, not `nexus.toml` or host settings. Show the effective level and its source (Session override, Workspace default, or Browser default) and describe the active scope in text and accessible description.
- Use a labeled radio group, not an unlabeled slider. Exactly these three option names exist. A new browser profile defaults to Balanced; the word “Recommended” is supporting text only.
- Scope and precedence are deterministic: session override > workspace default > browser default. The session control writes only the current session override. “Use workspace default” deletes that session key and reveals the workspace value if set, otherwise the browser value. Settings’ workspace control writes/clears only the current workspace default; “Use browser default” deletes that key. Settings’ browser control writes the browser default; “Reset browser default” removes it and resolves to Balanced. An unset browser key means the built-in Browser default of Balanced, not a fourth option; display source as “Browser default” in that case.
- Store scopes under distinct localStorage keys: browser default; workspace default keyed by bootstrap workspace identity; session override keyed by workspace identity plus session ID. Validate values against `focused|balanced|complete`; discard invalid values and fall back by precedence. Switching sessions recomputes effective value/source. Display both, for example “Focused · Session override”, and expose a scope description (“Applies to this session only; choose Use workspace default to clear”). Changing one scope never copies its value into another.
- A selector change updates the current view immediately and preserves the selected session and scroll position. It must not trigger a host command or stream resnapshot.

### Loading, disconnected, and error states

- Initial bootstrap: keep shell geometry and show labeled “Connecting to Nexus…”; do not render fake session data.
- Session snapshot loading: retain selected session title and draft; use stable skeleton rows. Announce one loading status, not each paint.
- Reconnecting: persistent compact banner: “Connection lost. The session may still be running.” Include Reconnect. Keep transcript visible and composer draft intact; do not infer turn stopped.
- Snapshot/schema failure: explain “Could not sync this session” with Retry; never partially apply a malformed patch. Reconnect from an authoritative snapshot after resync.
- Command/provider/model errors: show concise host-redacted message near the affected control and preserve user input. Details may be expanded if the host supplies safe detail.
- Empty model catalog: say no selectable models are currently available and retain the current/default selection; never show a fake successful picker.
- Turn failure/cancel: visible labeled end state on the relevant turn, preserve all messages/tool activity, and keep retry as an explicit new action.

## Three-level presentation model

The setting is a presentation preference over the same complete browser projection. Each control changes visibility, grouping, or default expansion. It never changes what the daemon records, what the reducer computes, what permissions allow, or what any other client sees.

| Information | Focused | Balanced (default; “Recommended” is supporting text only) | Complete |
| --- | --- | --- | --- |
| Assistant narration | Final/user-facing response remains complete. Intermediate assistant narration is a short preview (maximum 2 lines or 240 characters), expandable to full exposed text. The trailing message of an active turn remains full and visibly marked as streaming. | Full assistant prose, naturally grouped by message; intermediate progress prose remains visible. | Full prose and every distinct exposed assistant message, including intermediate narration. |
| Reads and searches | Adjacent successful activity may aggregate. Summary counts only when tool type is known; expand to each call. | Individual calls, compact card; collapse completed low-impact details by default. | Every call individually represented; any aggregate is only a navigation summary and is expanded by default. It never replaces or hides individual call cards. |
| Commands | Adjacent calls can aggregate as `Ran N commands`; failures/running calls stay individually visible. Expand for exact displayed command and result. | One compact card per call with status and short result preview. | Every call individually represented; any aggregate is only an expanded-by-default navigation summary, never a substitute for calls. Show all available parameters, progress, bounded result, status, and timing. |
| Edits and diffs | Summary may say `Edited N files` only with reliable file identity/count. Diff collapsed; file list and every available hunk are one deliberate expand away. | Edit card per call/file when IDs permit; show path and added/removed counts if supplied; diff collapsed by default. | Every edit call/file is individually represented; all host-exposed diff lines/hunks are shown in the inspector by default. A user may locally collapse long content; it starts expanded and the edit identity/card remains visible. Never exceed host caps. |
| Tool parameters/results | Hidden from summary except safe bounded preview. Per-call expand reveals all available sanitized parameters/results. | Status plus concise preview; parameters/results expandable. | Every call’s available sanitized parameters/results are expanded and visible by default. A user may locally collapse long payload content; cards stay individually represented and start expanded, subject to explicit host clipping/bounds. |
| Nested agents | Identity/state/task always visible; narration preview max 2 lines or 240 chars; active trailing narration remains full and visibly streaming; child transcript opens on demand. | Agent card with task/state and short latest/final preview; open child transcript. | Rich exposed lifecycle/progress, nesting, model/usage metadata, and full child transcript access. Each child tool call is individual; summaries are expanded navigation only. Do not invent unavailable events. |
| Reasoning / thinking, if exposed | Hidden behind an explicit “Provider-supplied thinking” disclosure; never included in aggregate preview. | Collapsed by default with explicit label. | Visible as a separate labeled block; never blend into user-facing assistant prose. |
| Timestamps / metadata | Turn start/end and current running/approval/failure states remain visible; per-call times and low-value metadata in expansion. | Message times and meaningful call duration/status available on card or focus; secondary metadata in inspector. | All exposed timestamps, IDs, model/provider, stop reason, usage, and call metadata displayed in quiet labeled metadata. |
| Inspector | Overview remains available; tools/agents list condensed, select any entry to inspect. | Overview, Tools, Agents with normal summaries and expandable records. | Inspector exposes all host-projected session/tool/agent diagnostics and details within host bounds. |
| What is never hidden | User messages, final assistant answer, pending approval/action, active/running status, errors, denial, cancellation, connection-loss notice. | Same | Same |

**No loss rule:** collapsed or summarized content remains in the in-memory projection and durable host record. A visible “Show details” / “Expand N activities” control provides access. Export uses the existing host export response and is independent of the setting.

### Assistant narration classification

Use fields already present in the web projection: each `MessageView` has `role`, `event_seq`, `done`, and text/blocks; its enclosing turn has `phase`. Within a turn, order assistant messages by `event_seq`, with stable message ID as a tie-breaker. Apply the rule independently per agent transcript/turn:

1. **Active turn** (`phase === "active"`): the last assistant message with `done === false` is the trailing in-progress message. Render its entire current text without preview truncation, label it “Streaming”, and keep it visible while it grows. Earlier assistant messages are intermediate and may use Focused previews. If no unfinished assistant message exists (for example while tools run), do not infer completion; render existing messages and keep turn status visible.
2. **Completed turn** (`phase === "completed"`): the last assistant message in order is final/user-facing and always fully visible. Earlier assistant messages are intermediate and may use Focused previews. If there is no assistant message or its text is empty, show no fabricated answer; retain completed state and other available turn content.
3. **Failed or cancelled turn**: do not infer a final answer. Render assistant messages in full and keep explicit failure/cancellation outcome and any host error visible. No narration preview may obscure the outcome.
4. **Unknown phase, missing `done`, ambiguous order/identity, or inconsistent state**: fail open by rendering full assistant messages and a visible status/metadata label. Do not collapse based on text content, count alone, or guessed terminal state.

The current projection supplies the needed fields, so client derivation is deterministic; no host schema addition is required. Apply this classification to nested agents only when their projected turn phase/messages are available; otherwise show all exposed narration in full.

## Focused-level activity aggregation

### Eligibility and boundaries

1. Derive the timeline from projected message/tool/agent records and their stable IDs/order metadata. Do not concatenate raw events or run a second reducer.
2. Aggregate only contiguous tool activity belonging to the same session, turn, and agent scope. A group consists of adjacent eligible tool calls in canonical timeline order; preserve original order within the group.
3. Flush the group before any assistant prose/message, user message, approval request or decision, error/failure, turn completion/cancellation, agent/task boundary, unknown activity type, or explicit projection gap/resync boundary. Session sequence numbers may legitimately skip between event frames; a numeric sequence gap alone is not evidence of missing projection data.
4. Never combine across turns, sessions, parent/child agent boundaries, or an item whose ordering is unknown. Unknown order means render individual cards.
5. Pending approvals, failed calls, cancelled calls, and currently running calls are not absorbed into a completed summary. A running call is an individual live row; previously completed adjacent calls may form a group before it.
6. Keep Edit/MultiEdit identity recoverable per call/file. A group may summarize edits only if each underlying call remains individually expandable and every exposed diff remains available in its detail view.

### Summary grammar and honesty

Use one compact line with clauses in first-occurrence order, separated by ` · `:

```text
Read 2 files · Edited 2 files · Ran 1 command
Searched 3 locations · Read 1 file
Used 2 tools
```

- Use singular/plural correctly (`Read 1 file`, `Read 2 files`; `Ran 1 command`, `Ran 2 commands`; `Edited 1 file`).
- Count calls, not inferred effects. Say “Edited N files” only when distinct file identities are supplied; otherwise say “Edited N times” or `Used ToolName`.
- Distinguish Glob/Grep/search from file reads and shell commands. Do not turn a Bash command into a file edit based on command text.
- Do not include paths, raw command text, parameters, or result snippets in a one-line summary unless the summary remains within host-provided safe display data. Details are available by expansion.
- Do not report success for unknown, clipped, running, failed, or cancelled activity. A summary’s status is `N completed` only if all members are completed; eligible groups are otherwise formed only from completed calls.

### Expansion, keyboard, and streaming behavior

- Summary is a real button/disclosure with an accessible label such as “3 completed tool activities; expand to inspect each call”. Enter/Space expands or collapses. Left/Right may collapse/expand when the control uses tree/disclosure semantics; do not hijack normal page navigation keys.
- Expanded calls retain stable `call_id` keys and individual status labels. Focus moves to the first call on expansion only when expansion was initiated from keyboard; collapsing returns focus to the summary button.
- Announce a new completed summary once through a polite live region. Do not announce every token, every progress update, or the full summary again on unrelated repaint.
- Build groups from stable projection identity and canonical order. During streaming, keep an existing group stable while eligible completed calls append; do not move a card because counts changed. Flush at a boundary immediately. A call changing to failed/running/approval state is removed from an aggregate and restored as an individual keyed card without losing focus or expansion context.
- If a host patch causes an authoritative resnapshot, recompute grouping from the snapshot while restoring expansion by group member IDs where the exact membership still matches. Never key groups only by array index or sequence number.

## Preference persistence and scope

- Browser default resolves to Balanced when unset. Save browser default, workspace default, and session override as separate browser UI preferences, not in session log, host config, URL, export, or protocol.
- Precedence is session override > workspace default > browser default (or Balanced if unset). The active-session selector writes only the session override. Its “Use workspace default” action removes only that override. Settings configures the workspace default and has “Use browser default” to remove it; Settings configures the browser default and has “Reset browser default” to return it to unset/Balanced. Show effective level plus source and accessible scope description at each selector.
- Switching sessions resolves target session override, then workspace default, then browser default, then Balanced. Reload restores the same result. Clearing browser site data resets to Balanced. New sessions have no session override and inherit the effective workspace/browser default without writing a session fact.
- Theme and detail level are never encoded in `/s/<session-id>` or query/fragment. Browser Back/Forward changes session navigation only. Copy/share URL opens the same session with that browser’s own local preference.
- Markdown/JSON/JSONL exports are exactly the host’s session export and are independent of the rendered level. No omitted projection content may be inserted into an export by the client.
- Do not synchronize this preference across browser profiles or expose it to Textual. It cannot alter daemon events, reducer state, permissions, model prompts/selection/behavior, tool execution, queue state, security bounds, or what another attached client sees.

## Accessibility and keyboard requirements

- Use semantic landmarks (`header`, `nav`, `main`, `aside`), one `h1`, ordered headings, real buttons/links, and programmatic labels. Session list and inspector tabs follow the appropriate list/tab patterns and expose selected state.
- All functions have mouse and keyboard paths. Focus order follows sidebar → top bar → transcript controls → composer → inspector/overlay without focus traps outside modal dialogs.
- Modal palette/approval/settings dialogs trap focus while open, announce a title, close/resolve predictably, and restore focus to the invoking control. Approval Escape resolves as Deny once, with visible wording and announcement. The detail selector exposes effective value, source, write scope, and clear/override action in its accessible description. Arrow keys move among radio options; selecting in the active-session control writes the session override; Tab reaches “Use workspace default” without changing scope.
- Maintain visible focus, text contrast, non-color status labels, and at least 44×44 px touch targets on narrow/touch layouts; desktop controls are at least 36×36 px. Do not depend on hover for essential controls or metadata.
- `role="log"` is not used to read the entire transcript on every update. Announce user-visible state transitions and newly completed activity in a polite region; keep streamed token updates silent. Use assertive announcements only for an urgent actionable approval where appropriate.
- At 200% zoom and narrow view, all primary tasks remain available without two-dimensional page scrolling: switch session, send/stop, select model/agent, inspect tool/diff and child, decide approval, change detail setting, reconnect.
- Respect reduced motion. Support browser text zoom, OS contrast settings, native text selection, screen-reader names for icon buttons, and high-contrast focus boundaries.
- Diff add/remove information includes `+`/`−` text or equivalent accessible labels, not color alone. Disclosure labels report expanded state and meaningful item counts.

## Implementation notes for this codebase

- Static app remains under `nexus/ui/web/`: `index.html`, `styles/tokens.css`, `styles/app.css`, and native ES modules. No framework, CSS framework, build step, remote font, or remote asset is introduced.
- `nexus/host/facade.py` is the sole UI-facing runtime API; browser code must not read daemon/session files or reach into managers/tools. `nexus/host/web.py` serves the browser and enforces current cookie/CSRF/Origin/response boundaries.
- Render only the versioned host web snapshot and operations through `js/projection.js`. Maintain the snapshot/reconnect/resync contract in `webplan.md`; duplicate or stale sequence frames are ignored and session sequence values are not assumed consecutive. Patch application validates the complete operation list against a detached staged view, then returns a replacement projection only after every operation succeeds. On malformed path/op/value, discard the staged view, leave the last good projection untouched, and resnapshot.
- Detail-level rendering and activity grouping are derived client-side from the **complete host projection**, not raw event stream, invented core state, or lossy host-side filtering. Key messages/tools/agents by durable projected IDs (`message_id`, `call_id`, agent ID, turn ID); retain all projected fields in the client state while adjusting DOM presentation.
- Host bounds and redaction are authoritative. Do not evade current payload limits, clipping, credential redaction, byte-payload display rules, or Markdown sanitization. Never fetch arbitrary filesystem paths referenced by tools. A “Complete” UI means all detail the projection safely exposes, not unbounded data or raw secrets.
- Use DOM text APIs for untrusted fields. Assistant Markdown must follow the existing allowlist/sanitization requirement in `webplan.md`; do not insert raw host strings as HTML. Tool output, paths, errors, model names, labels, and diff content are untrusted display data.
- Preference and override keys stay in browser storage, separate from drafts and authentication, using the exact scope precedence and clear actions above. Do not send preference values in `api.command`, SSE query parameters, or export requests.
- Use CSS classes/data attributes to select presentation level. Recompute view models from current projection without mutating `state.view`; keyed DOM updates preserve open disclosures, focus, and scroll.
- Match current shell IDs and responsive behavior where practical. Remove `.window-lights` in the visual implementation; keep actual browser/desktop chrome to the platform. Current implementation details in `webplan.md` are a checkpoint, not proof that every state/accessibility requirement here is already met.
- Session tool diff cards may show the bounded `ToolCallView.diff` supplied by the host. Repository-wide Git review and worktree/branch controls remain deferred until a shared host contract exists.

## Acceptance criteria

### Functional and visual

- Exactly three user-facing detail option names appear: Focused, Balanced, Complete. Balanced has separate visible supporting text “Recommended”, excluded from its accessible name. A new browser profile resolves to Balanced. Test active-session selection writes only the session key; “Use workspace default” removes that key; Settings “Use browser default” clears the workspace key; “Reset browser default” clears the browser key. Verify effective value/source after each action and after reload/session switch. No host request is made.
- Each level preserves the final answer, user prompts, pending approval, errors, running state, cancellation state, connection notice, and access to every field in the current host projection.
- Focused aggregates only contiguous eligible calls under the stated boundary rules. Expanding reveals each keyed call and does not change projection/export. Failed, running, approved, denied, and agent-boundary activity is never misrepresented as successful aggregate work.
- Complete shows each tool call as its own card, including child-agent tools. An optional navigation summary is expanded by default and never replaces individual cards. Verify every call’s parameters/results, each exposed diff line, clipping label, timestamps, and status; no browser-side file fetch fills gaps.
- Exercise narration classification with (a) active unfinished trailing assistant message receiving another delta, (b) completed turn with two assistant messages, (c) completed turn with no assistant prose, (d) failed turn with error, (e) cancelled turn, and (f) missing/unknown phase. Focused previews only intermediate messages in (a)/(b); active stream and completed final message remain full; failure/cancel outcome is always visible; uncertain cases show full text.
- Verify preference survival and isolation: configure all scopes, reload, switch sessions/workspaces, clear each override, and open the same session in a second browser profile. Confirm URL, host state/events, export, permissions, model behavior, and second client are unchanged.
- At 1440 px, assert sidebar 232–280 px, main minimum 520 px, inspector 320–400 px. At 1024 px inspector is closed or drawer; at 800 px sidebar is rail/drawer and inspector drawer; at 390 px both are overlays. At 200% zoom verify reflow with no horizontal page scroll and usable primary actions. At 959/960, 1279/1280, and 699/700 px verify the exact breakpoint transition and dimensions.
- Feed a malformed patch with a valid first operation and invalid later operation; assert the displayed and stored last-good projection are unchanged and the client requests a fresh snapshot. Then apply a valid patch and assert the full view matches the authoritative snapshot.
- Verify current Markdown constructs remain readable. When allowlisted links are implemented, test accepted safe schemes and rejected `javascript:`/unknown schemes; assert labels and URLs are escaped/sanitized and external links receive safe attributes.
- No background image, simulated desktop chrome, decorative gradients, nonfunctional controls, or excessive blur appears in any theme/state.

### Screenshot and state checklist

Capture reviewed screenshots at 1440×900 (large), 1024×768 (medium), 800×900 (compact), 390×844 (narrow), and browser zoom 200% at a 1280×800 window. Capture light and dark themes and each of the three detail levels. Required content/state set:

1. Fresh workspace with no sessions and first-session action.
2. Empty session with composer, model/agent selectors, prompt suggestions.
3. Long conversation with user prompt, Markdown, code block, and scrolled-away scroll position.
4. Mixed read/search/command/edit activity proving focused aggregation, balanced cards, and complete detail.
5. Expanded aggregate proving stable order and access to each call; failed and running call adjacent to completed activity.
6. Edit/MultiEdit diff, line markers, clipped diff indicator, and inspector tool detail.
7. Nested child agent with parent breadcrumb and live/complete preview.
8. Pending approval, persistent options available/unavailable, and “Answered in another view”.
9. Reconnecting/disconnected banner while transcript and draft remain present; retryable error/model unavailable state.
10. Reduced-motion appearance, visible keyboard focus, open settings/detail radio group, and narrow inspector overlay.
11. Narration classification cases (active stream; completed with multiple assistant messages; completed without prose; failed; cancelled; unknown phase) in Focused and Complete.
12. Complete view with expanded navigation summary plus all individual parent and nested-agent tool cards visible.

### Review gates

- Manually compare light/dark hierarchy and contrast; check no surface depends on a background image or blur.
- Keyboard-only review: Tab/Shift+Tab reaches sidebar, session, inspector, composer, and detail selector; Enter/Space chooses options and expands groups; Escape restores focus from settings/inspector and denies approval once; no shortcut fires while typing or during IME composition. Arrow navigation works in radio groups, tabs, session list, and palette without hijacking composer keys.
- Screen-reader review confirms landmarks, option names (Balanced excludes “Recommended”), current level/source/scope and clear action, group counts/order, individual calls in Complete, tool statuses, diff additions/removals, approval consequences, connection changes, and no token-by-token transcript announcements.
- At 959/960, 1279/1280, and 699/700 px plus 200% zoom, verify the matching layout and no horizontal page scrolling. Reduced-motion check confirms no nonessential transition or smooth scroll.
- Verify browser storage contains only UI preference/layout/draft state; no session content, event projection, credentials, or approval decision is added for this design.
- Run the relevant browser visual/functional checks and `git diff --check` when implementing this specification; implementation review must still preserve host-side security and sanitization tests.
