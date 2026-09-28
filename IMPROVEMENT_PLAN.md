# Nexus improvement plan: a great terminal coding harness

Research snapshot: 2026-09-25. Primary user: **coding agent operators**. Primary outcome: **a smooth terminal experience**. This is a forward-looking plan, separate from the historical phase ledger in `PLAN.md`.

## Product goal and decision rule

An operator should be able to open a repository, give the agent precise context, see what it is doing, interrupt or answer it, review the complete change, and return to the work later without guessing what happened. The terminal should make those actions fast and legible. A feature earns priority when it shortens that loop, preserves control over workspace changes, or makes a failure diagnosable.

Keep the existing architecture: one provider-neutral loop; daemon-owned sessions; durable events and replay; a pure view reducer; UI access through the host facade; permission checks in the daemon. Build operator workflows through those contracts. Taui is a reference for interaction patterns, not an architecture to port wholesale.

## What exists now

Nexus already has a mature base: provider adapters and conformance tests, a model registry and tiers, context compaction, host-side permissions, append-only session logs, bounded subagents, hot extensions, a daemon, and a Textual chat UI (`README.md`, `ARCHITECTURE.md`). The UI already supports multiline input, a command palette, model and agent selection, session commands, approval prompts, replay/reconnect, a subagent transcript, and tool-level Edit/MultiEdit diff previews. The current worktree includes substantial uncommitted TUI/host changes and new TUI tests, so these need to be treated as in-progress baseline work rather than proposed features.

The taui port defers `/compact`: Nexus currently compacts automatically while
assembling a request, but the runtime has no explicit compaction command for a
client to call. Add a host action and durable compaction event before exposing
manual compaction in the Textual shell.

The largest observed operator gaps are: the composer cannot attach files or images and has no `@path` completion; `/sessions` lists IDs in a notice instead of a searchable picker; the empty session gives little setup guidance; the main timeline does not render provider-supplied reasoning; a tool preview does not summarize the full repository change; there is no first-class worktree lifecycle; and there is no structured question flow for either the main agent or subagents. The current theme also forces a near-black background, while model/context details sit above the composer rather than in a compact row below it. Release automation and task-quality evals are missing or not discoverable in this checkout. Evidence and boundaries appear below.

## Priority order

| Priority | Outcome | Why now | Size / dependency |
| --- | --- | --- | --- |
| P0 | Stabilize and gate the current TUI work | Avoid planning on a moving UI baseline and catch real terminal regressions | Medium; finish current patch first |
| P1 | `@path` completion and a searchable session picker | Removes frequent navigation friction in the terminal | Medium; host-backed path listing for `@` |
| P1 | Useful first-run and empty-session state | Makes setup and the next action obvious | Small; doctor/config state |
| P1 | Composer image attachments | Lets operators provide screenshots and visual evidence in chat | Medium–large; content transport/storage |
| P1 | Reasoning display and below-composer information row | Makes active model behavior and context headroom visible | Medium; reducer and effective model metadata |
| P1 | Terminal background that follows the user's terminal when feasible | Removes the forced black canvas | Small prototype, then theme work; terminal capability dependent |
| P1 | Complete task change review | Lets the operator assess everything the agent changed, including shell writes | Large; best paired with worktrees |
| P1 | Session/task worktrees | Gives parallel agents separate checkouts and a safe discard path | Large; Git/cwd/session plumbing |
| P2 | Text/document attachments | Completes the attachment workflow beyond images | Medium; reuses attachment pipeline |
| P2 | Main-agent and subagent questions | Lets either agent pause for a real choice and resume durably | Medium; event/facade/UI additions |
| P2 | Honest spend and context diagnostics | Makes expensive or shortened turns explainable | Medium; event-derived accounting |
| P3 | Task eval runner | Measures whether behavior changes improve coding outcomes | Medium; can start alongside P1 |
| P3 | Job inspector and optional traces | Helpful for long-running tests and diagnosis | Medium; validate demand first |

Sizes are relative estimates from static inspection, not delivery commitments. The rows below define acceptance before implementation starts.

## Milestone 0 — settle the current terminal baseline

1. Complete the ongoing TUI/host patch and classify its new tests. Pin the PTY keyboard contract (`tests/test_tui_keys.py`), reducer/reconnect behavior, scripted-provider terminal journey, and representative Textual snapshots. The current diff already touches `nexus/ui/tui/app.py`, `timeline.py`, `controller.py`, `widgets.py`, `nexus/view/reduce.py`, and host files. Review that as one coherent change before building another UI layer on it.
2. Add a reproducible developer/release gate: locked dev install; lint; offline unit/integration tests; wheel build and clean-venv smoke; terminal/visual checks in a pinned environment. Run on the declared Python and macOS/Linux support matrix, or narrow the support claim. `pyproject.toml` has pytest and Playwright but no lint/timeout dependency; no CI workflow or release guide was found in this checkout. Keep credential-gated live provider checks separate.
3. Record a clean baseline report with test counts, durations, known environment requirements, and artifact versions. The research run here stopped after 1,096 passes because the sandbox rejected `asyncio.start_unix_server(...).bind()` with `PermissionError`; that says nothing about the daemon in an unrestricted local terminal. Rerun the socket-dependent suite in an environment that permits Unix sockets. Do not bless failures as a new baseline.

**Exit:** clean install and packaged install both launch; PTY Enter/newline, approval, reconnect, session switch, and subagent transcript journeys pass; visual changes are reviewed; offline suite passes where Unix sockets are available. This gate remains required for later milestones.

## Milestone 1 — make everyday terminal navigation quick

### 1A. Workspace path completion

Typing `@` in the composer should offer bounded, keyboard-selectable paths relative to the active workspace. Completion should insert a stable reference; it should not silently read file contents or send a prompt. Use the existing `Read`/`Glob`/`LS` path policy and a host-facade query, not direct filesystem access from `nexus/ui/**`. Decide explicitly whether ignored/hidden files appear and how a worktree changes the root. Taui's `taui/tui/widgets/at_completer.py` and `chat_input.py` are interaction references; Nexus's current `ChatEditor` in `nexus/ui/tui/widgets.py` has no comparable completion flow.

**Accept when:** keyboard-only search/insertion works in a large repo; suggestions are bounded and responsive; a path outside the active workspace or denied by policy is not offered; no file bytes enter the prompt until the operator or agent explicitly requests them; replay shows the literal reference the user sent. Proposed performance target: p95 suggestion update under 200 ms for a 10,000-file local tree, measured in CI or a repeatable benchmark.

### 1B. Searchable session picker

Turn Ctrl+O and `/sessions` into a picker showing title, ID, recency, running/idle state, and current-session marker. Keep `/sessions <id>` for scripts/experts. The current `app.py` implementation renders a newline-joined notice and switches by ID/prefix; the session/fork/replay host operations already exist. Taui's session picker is a UI reference.

**Accept when:** search by title or ID and keyboard switch work; empty/loading/disconnected states are clear; switching preserves each draft or warns before discarding it; a live turn survives view detachment and reappears from its durable event sequence; no duplicate transcript rows appear after reconnect.

### 1C. First-run/empty-session state

Show a small, non-transcript panel when a session has no messages. If provider configuration is missing, point to the exact `nexus init`/`nexus doctor` or model setup action. When ready, show the selected model/agent, a few useful coding prompts, and Ctrl+P help. The present `app.py` composes a timeline and editor, then changes the connection status after bootstrap; the README has setup instructions, but the empty app does not guide the operator.

**Accept when:** unconfigured, ready, disconnected, and reconnecting states have different actionable text; the panel disappears after the first user turn; it does not create a synthetic session event or alter replay; it works at narrow terminal widths.

### 1D. Information below the composer

Put a compact, persistent information row **below the text box**. Show the effective provider and model, selected reasoning effort when the provider exposes one, any configured thinking-token budget as a separate value, and context-window usage. Distinguish the model's full context-window capacity from the current request's smaller input budget after output reservation and safety margin; show `used / available input budget` and `model window` rather than conflating them. Include active session/agent and turn state only where space allows. On narrow terminals, abbreviate labels or wrap to a second line before hiding essential context. The current `ChatInput` places `RootAgentBar` and `context-usage` above `ChatEditor`; `RootAgentBar` already shows provider/model and a reasoning label, but `HostFacade.current_agent` currently returns `reasoning_effort: "not exposed"`, so the UI must not invent an effort from `thinking_budget`.

Use existing model retry, context, usage, and subagent events so the row updates when a fallback actually changes the active model. Put deeper provenance in `/details`: why context was compacted, which prompt parts consumed budget, token usage, and estimated spend. Estimate USD only from known registry rates and actual usage, including cache rates where supported; unknown or stale prices must display “unavailable” rather than `$0`. Taui's `cost.py` shows the utility of a ledger; Nexus should avoid its hard-coded fallback pricing.

**Accept when:** the row sits beneath the composer in idle, streaming, approval, question, and reconnect states; model/provider are the effective values for the current turn; effort says unavailable when it is not exposed; context usage and full model window have distinct labels; switching models or compaction updates values without flicker; narrow-width screenshots remain readable. Model switches, fallback, subagent usage, restart/replay and cache tokens yield identical totals from the same event prefix; spend is labelled an estimate; no prompt or secret-bearing config value leaks through details.

### 1E. Provider-supplied reasoning in the transcript

Render normalized `thinking.delta` / finalized thinking blocks for the main agent as a subdued, collapsible section attached to the correct assistant message. Keep live streaming seekable through replay/reconnect, and offer a show/hide preference so reasoning does not overwhelm normal answers. The reducer already stores text and thinking blocks (`nexus/view/reduce.py`, `view/model.py`); `agent_transcript.py` already renders child thinking when present, while the main `timeline.py` currently renders assistant prose without it. Show only the text or summary the provider actually emits. A reasoning-token count alone is not reasoning text, and encrypted or opaque provider payloads must not be decoded or displayed.

**Accept when:** a provider with normalized reasoning text shows a live, collapsible section for both root and child messages; replay and reconnect reproduce it once in the right order; providers emitting only usage counts show no empty reasoning panel; final answer text remains visually primary; terminal control characters and provider data are safely rendered. Cover full text, summary-only, no-reasoning, interrupted stream, and nested-agent cases.

### 1F. Terminal-native background

Remove the forced near-black canvas from `nexus/ui/tui/theme.py` and `app.tcss`. Prototype whether Textual 8.2.8 can emit the terminal's **default background** for the full screen, and test it in supported terminals with dark, light, and translucent terminal themes. CSS `background: transparent` only blends with Textual's underlying screen color, so it should not be assumed to reveal the terminal background. [Textual's FAQ](https://textual.textualize.io/FAQ/) says terminal translucency is unlikely to work because Textual normally renders true-color backgrounds rather than ANSI default background colors. If the backend cannot truly inherit the terminal background, offer a configurable or detected terminal-matching canvas with readable contrast and avoid claiming transparency. Keep focused surfaces such as the input, selection, and dialogs distinct.

**Accept when:** a supported terminal can use its own default background if the prototype proves this possible; otherwise the default no longer hard-codes black and the user can match the canvas to their terminal. Verify dark/light themes, terminal transparency where supported, readable selection/focus/approval states, and no bright color flash on startup, resize, or exit. Document which terminals actually inherit the background versus use a matched color.

### 1G. Image attachments in the composer

Add a visible attach action plus image paste/drop where the terminal supports it, an attachment chip or thumbnail/filename preview, and remove controls before send. Ship images early so screenshots can accompany bug reports, UI reviews, and visual questions. Nexus already defines `Image` and `Document` in `nexus/model/message.py`, and `config/schema.py` has an attachments budget, but `nexus/context/parts.py` currently registers `_NoOpPart("attachments", 2)`. This needs end-to-end composer, host transport, durable message, context, provider-capability, and replay behavior. Taui's attachment bar and input handling are UI references. Text/document files can follow on the same pipeline in Milestone 3.

**Accept when:** the operator can attach a PNG or JPEG from a path or supported paste, see its name/type/size and remove it before sending; the sent message shows an attachment marker on replay; only deliberately selected bytes are sent; size/type/token caps give clear feedback before send; a vision-capable provider receives the image and an unsupported one follows a visible degradation policy. Test multiple images, image plus text, oversized/invalid image, stale path, provider switch, reconnect, export privacy, and wire-size limits.

## Milestone 2 — make coding changes reviewable and disposable

### 2A. Session and subagent worktrees

Offer an opt-in “work in a branch/worktree” action for a session, with a branch/base commit, active path, and clean/dirty state recorded in session events. Allow selected subagents to work in separate worktrees when parallel writes are expected. The tool workspace, Bash cwd, hooks, path policy, context environment, `@path` completion, and UI labels must all agree on the active checkout. On resume, validate that the path and base still exist and report recovery choices. Taui's `taui/worktree.py` is a useful lifecycle reference.

**Accept when:** when isolated execution is selected, two writing agents receive separate checkouts and Git indexes; creation and resume are idempotent; a failed creation leaves the original workspace untouched; session replay identifies the active checkout; discard refuses or clearly resolves uncommitted work; detached turns continue in their checkout. Worktrees isolate files and Git state; they are **not** an OS sandbox for Bash, network, home directories, or trusted Python extensions.

### 2B. Whole-task change review

Add a session-level change view with file list, additions/deletions, untracked files, and full diff against the worktree base. Include changes made by shell commands and subagents. Provide clear apply/integrate and discard operations with conflict reporting; retain a reviewable result if integration fails. The current dirty-tree `timeline.py` already renders durable per-tool Edit/MultiEdit hunks, so the missing scope is the *aggregate repository result*. Taui's `tui/screens/git_diff.py` is a presentation reference.

**Accept when:** edits from Write, Edit, MultiEdit and Bash appear in the same review; binary/untracked files have useful summaries; the operator can navigate and copy diff text in a narrow terminal; apply reports conflicts without losing either side; discard removes only the owned worktree after checking for unreviewed changes. Never auto-commit or auto-merge merely because a turn finished.

## Milestone 3 — complete file input and agent questions

### 3A. Text and document attachments

Extend the Milestone 1 image-attachment pipeline to text and document files. Keep selection, preview, removal, size limits, durable storage/reference, and provider degradation consistent. Make explicit whether a text file becomes quoted prompt context or a provider-native document block, and show the choice before send. Do not silently ingest a path typed in the prompt.

**Accept when:** text and document attachments reuse the image workflow's selection and retention rules; a large file is bounded before model submission with an explicit truncation or rejection outcome; the operator can see what was attached on replay; unsupported provider formats degrade visibly; the agent can distinguish user prose from attached file content.

### 3B. Questions from the main agent **and every subagent**

Add a model-facing question tool and distinct `question.requested` / `question.resolved` events with single choice, multiple choice, and free text. Make the tool available to the root agent and all default child/grandchild profiles, including read-only research roles, unless an explicit policy denies it. A child's request must route through the parent event relay and root host facade to the operator, carry the originating agent ID/task and unique question ID, and deliver the answer back to the *same* waiting child turn. Show which agent is asking in the question panel, with a jump to its transcript. Allow more than one outstanding question without mixing responses; define ordering and cancel behavior when a parent Task is waiting for a child. Reuse the proven async request/resolution approach used by approvals, but keep question answers separate from permission grants. Taui's `tools/builtins/question.py` and `tui/widgets/questions_panel.py` show the operator affordance.

**Accept when:** root and nested agents can independently ask, wait, and resume; answer IDs cannot be routed to a different agent; origin and answer persist in both the child transcript and root replay view; reconnect restores unanswered questions; simultaneous questions remain distinguishable; cancelling a child or parent resolves its pending questions and cannot deadlock the parent Task; headless mode follows an explicit policy rather than hanging forever. No answer changes a tool permission rule.

## Milestone 4 — measure task quality and diagnose hard cases

Add a small `nexus eval` fixture/report path over the existing `ScriptedProvider` and durable events. Start with coding-operator scenarios: relevant file discovery, image attachment and vision degradation, streamed root/child reasoning display, edit and test, permission denial/recovery, root and nested-agent questions, subagent handoff, context compaction, interrupted/reconnected turn, model fallback, and review/discard. Compare observable outcomes (file result, tool arguments, final response constraints, event invariants, turn count, latency, token/spend estimates) rather than brittle exact prose. Keep model-live evals opt-in and separate from deterministic release gates. Taui's `taui/eval.py` gives a lightweight fixture pattern; Nexus's existing provider-conformance and integration tests remain essential and are not replaced by evals.

**Accept when:** a fixture runs offline from a clean checkout, emits a machine-readable and human-readable diff, has a documented update/review flow, and catches an intentional bad change. Track user-journey measures alongside pass/fail: time to resume a session, time to find/attach a file, number of steps to review/discard, failed or abandoned approvals/questions, and task completion rate on a small fixed benchmark. Use these measures to decide whether later UI complexity earns its keep.

After those basics, add a session-scoped job inspector if operators often run long tests or servers. `BashOutput` and `KillShell` already exist for agents; a human panel would show bounded tail, exit status, and cancel without launching work. Optional trace export should correlate session/turn/agent/tool IDs and timings while omitting prompt/tool content by default. These are later features because the durable event stream and `/details` already cover much of the basic diagnosis.

## Architecture and safety guardrails

- Every new operator-visible state transition must be an event that replay/reconnect can reduce. Add protocol commands/results and facade methods before TUI controls; keep `nexus/ui/**` inside its enforced import boundary.
- Preserve the existing first-match permission engine and path canonicalization. A root or subagent question is never an approval. A worktree is never a security boundary. If host containment is needed later, scope an actual OS-backed execution backend with adversarial integration tests and clear platform support.
- Keep data caps explicit at each layer: composer input, host wire body, persisted event, provider request, tool result, and UI preview. Report truncation/degradation instead of silently losing content.
- Keep provider-specific behavior in adapters/capabilities and use the shared conformance suite. Cost and context display must derive from actual event/request data, not a guessed model default.
- Avoid a full TUI rewrite, generic IDE/file manager, new provider framework, or wholesale Taui port. The current event/facade boundaries are valuable and already tested.

## Research trail and limitations

This plan combines six read-only Luna research passes across architecture, reliability, provider/context accounting, TUI workflow, release contracts, and prioritization, plus direct inspection of Nexus and `../taui`. Concrete comparison points: Taui's `tui/widgets/at_completer.py`, `tui/screens/session_picker.py`, `tui/widgets/attachments_bar.py`, `tools/builtins/question.py`, `worktree.py`, `tui/screens/git_diff.py`, `eval.py`, and `docs/testing.md`. Those are examples of user value; Nexus has stronger existing daemon/facade separation and protocol/event contracts for its own implementation.

This is a static review of an uncommitted worktree, not a production usability study. The attempted full offline pytest run was interrupted after 1,096 passes, 26 failures and 8 setup errors because this sandbox disallows Unix socket binds; three live tests were deselected. At least one quarantine test also failed and was not diagnosed in this environment. A clean unrestricted baseline is a Milestone 0 task, not an assumed fact. Priorities should be revisited after the first terminal journey tests and direct operator feedback.
