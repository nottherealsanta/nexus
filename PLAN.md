# Nexus 0.10: remaining work

This plan lists outstanding work only. Current behavior is in [docs/README.md](docs/README.md); historical porting decisions are in the [porting ledger](docs/porting-ledger.md). Old Nexus (`~/repos/nexus`, branch `nexus01`) is reference only; port tests with code and record deliberate behavior changes in the ledger.

The previous full plan, including completed work and original porting specifications, is available as `git show f2f23df:PLAN.md`.

Preserve the visible, ordered context timeline, announce clipping, keep task tools separate from Nexus control, and do not rewrite context already sent to a provider. Use `~/.nexus10`, not `~/.nexus`, until the release migration.

## OSC 7501 program status

The TUI emits per-session OSC 7501 records; see [docs/development.md](docs/development.md#terminal-program-status). Verify the behavior in a supporting terminal such as Rex, including reconnect, suspension and multiple simultaneous sessions.

## Turn summaries

Follow [TURN_SUMMARY_PLAN.md](TURN_SUMMARY_PLAN.md) for the remaining real-daemon checks, documentation and step 9 (live partials and ephemeral deltas). Preserve durable `input.*` events and the ordering of delivered steers; a steer is not a new turn. The TUI benchmark is in [docs/turn-summary-benchmark.md](docs/turn-summary-benchmark.md).

## Settings: Providers and Agents pages

The pages are in both clients ([config.md](docs/config.md#settings-layout)). Remaining: sign in to a real device-flow provider from each client's Providers page; NSH browser coverage for key entry, the device-code row and the Agents page edits once the mock stream is fixed (see Browser tests); PTY screenshots of both pages at 80×24 and 160×48. Agent cards show the file path but cannot open it: the built-in editor reads project files only, and global and built-in agents live elsewhere.

## Steer, queue and prompt history

- Add NSH browser coverage for Enter/Tab and the pending-input stack after fixing the mock stream (see Browser tests). The composer already steers and queues, and the timeline shows the TUI's `steer` / `queued` tags.
- Add Ctrl+R history search in both clients; verify cross-client per-project recall after restarting both clients, including multiline drafts and images.

## Extensions, skills, MCP and hooks

- Show agent and user-tool errors in client pickers and Settings; add a visible changed-file notice for a running tool, process memory limits and validation of project-only names in `[tools] disabled`. Exercise a user tool in a real session and run the reload stress test.
- Add default-on settings for skills and exercise skills with a real model. Decide whether and how `allowed-tools` should be enforced without changing a cached prompt or granting permissions.
- Add MCP resources and prompts and Settings screens for adding/editing servers; exercise browser-use in a real model session, including server failure. MCP output must remain bounded, fenced as untrusted and visible as received.
- Support editing hooks in Settings, render hook rows in both timelines, and fire `SessionEnd` on archive. Test `bash(...)` matchers on compound commands and `PostToolUse` after denied approval; verify a blocking hook with a real model.

## Providers

- Verify real OpenAI and Ollama calls.
- **J4 Anthropic:** add the Messages dialect using the Anthropic SDK as a plain client (Nexus owns the loop); preserve signed thinking blocks for lossless replay and handle cache-control breakpoints and tool-result media.
- **J5 Gemini:** choose a dialect that preserves replayable reasoning.
- **J6 Claude and Antigravity:** assess their fit with the one-timeline rule before porting. Use the Claude Agent SDK and the approach in https://github.com/NoeFabris/opencode-antigravity-auth rather than ZMX.
- Each provider needs wire fixtures, provider/auth documentation and a verified real call. Group OpenAI-compatible providers where possible.

## Context and sessions

- Session switching is slow (2026-10-30). Fixed: Core re-parsed the 6.4 MB models.dev cache on every context, draft-context and image-input call (~300 ms on the event loop each), and NSH's new-session view asked for the draft context on every model bump, so a streaming turn kept Core at 100% CPU. The NSH composer also re-read prompt history on every bump (thousands of requests per turn), and the `@` listing asked again while a request was in flight; both are fixed. Still open: after a long streamed turn, NSH's main thread stays busy for 5-7 s rendering it (present in the committed code too), so clicks in that window wait. The TUI now caches the last 8 session views and keeps them current from the stream; select loads history in the background with no resync, and git/worktree reads no longer block a switch (`--bench`: a switch 260 → 10 ms, back to a cached view 0.8 ms to first frame). NSH caches session views the same way. Both clients now run context reads one at a time per session and coalesce the rest, and the TUI no longer reads git and worktrees inline at launch (first open ~115 → ~50 ms). Core still rebuilds the whole request body for each `GET …/context` (warm 5–12 ms for a 1.4 MB body, 30–130 ms on a session's first read) and sends all of it to clients that mostly show `used`/`window`; a Core-side cache was not added because its invalidation would have to track everything `agent.request()` reads, and a stale one would show context the model does not have. Next: a lighter context endpoint (budget only) if the cost shows up again.
- Model memory (no default model) is in core, TUI and NSH; check it in a real daemon with both clients: a draft with no model asks for one (Core's draft-context request returns 400 "no model selected", so the starting-context preview is empty until one is chosen: consider a provider-independent estimate), a remembered model whose provider is signed out is shown and not replaced, and a session stored before the change that holds no model asks for one. The TUI test `agent_and_model_chosen_before_a_session_exists_go_into_its_creation` already failed before this change.

- Allow `/btw` during a running turn in both clients; add NSH browser coverage for opening the original session from a handoff.
- Return per-section starting-context detail text from Core for session drafts.
- Decide whether to persist session grants; expose grant listing and clearing in clients. Add a Settings section for provider limits and verify `/api/usage` with a real account.
- Add editor find and syntax highlighting; test NSH save and stale-file paths in the browser harness. Make NSH tool-result images zoomable.

## Worktrees

Port worktree review and integration as control commands: review against the base, merge by fast-forward or merge commit with confirmation, and remove the worktree and branch after merge. Add commands in both clients; agents get them only when granted. Consult old `nexus/agents/worktree_review.py`, `worktree_integrate.py`, `nexus/worktree_cleanup.py` and `tests/test_worktree_merge_permission.py`. Decide cleanup on archive/delete and how to leave a worktree.

## Interface

Agent pickers now filter the shared catalog by root/draft or child eligibility; verify
child-session picker navigation in the NSH browser suite once its mock stream is fixed.

### TUI

- Verify Cmd+arrow composer shortcuts in a terminal that forwards Super-modified keys.
- Add `path:line` and OSC 8 link hit regions; choose a syntax-highlighting library for the file view.
- Fix split-diff continuation alignment (`nexus-tui/src/diff.rs`) and provide line numbers for edit/patch diffs where input lacks them.
- Add PTY tests at 80×24 and 160×48 for anchor, follow and jump-to-latest; produce a fixed-fixture review gallery (`interface/review.md`).
- Timeline mouse selection copies visible screen cells on release; consider selection across scrolling and copying underlying unwrapped text, and verify OSC 52 in real terminals.
- Add failed child count, enabled/disabled tools and kernels to session info.
- Check OSC 777 notifications in kitty (possibly OSC 99) and track focus; debounce completion requests if large workspaces make search slow.
- Compare with old Nexus in a real terminal and review screenshots.

### NSH and shared

- Finish design parity for the new-session view, files, narrow layouts and states; improve sidebar search handling and live shimmer; sidebar rows show project or branch where available. User and assistant timeline prose now uses bundled Monaspace Argon; verify native rendering and visual parity. Pin NSH, build the macOS desktop bundle and complete files, Git and worktree UI; then remove `nexus-design-mockup/`.
- Subagent navigation now follows the TUI: root tabs exclude children, a viewed child keeps its root's tab, and the subagent strip under the header goes back to the parent and selects the root's children with running, waiting, failed or completed state (`child-navigation-parity.test.tsx`). Remaining: complete the parent card with model/effort, elapsed time and completed token count; distinguish background completion from the start result. Parent cards already show status, agent, session name and background label, and their tool results can be expanded.
- Add Mermaid image rendering in kitty and iTerm, and NSH Mermaid using a pinned host-served bundle (no CDN).
- Review both clients side by side in light and dark, establish large-session performance budgets and test keyboard-only paths. Decide focus-ring colour.

## Browser tests

Fix the NSH mock event stream ending and forcing the app into reconnecting state; then rerun the full browser suite. The recorded run had 13 failures, including `handoff.test.tsx`, `App.test.tsx`, `i6.test.tsx` and `i8.test.tsx`; the record is inconsistent about the clean baseline, so do not assume all are unrelated. `nsh test --target web` needs an explicit Playwright Chromium path on this machine. Add steer numbering, new-session skills/MCP rows, usage modal and the other browser coverage listed above.

## Reliability and safety gaps

- Run Monty kernels in a scrubbed helper process before release; investigate checkpoint recovery, Linux, forced worker crashes and cold-start latency.
- Decide how to supervise and report background bash jobs across daemon crashes; do not kill a process based only on a stored PID.
- Consider durable pending directory moves and worktree-in-use checks for external processes; archive transitions currently appear only in the root log.
- Decide whether archive needs bulk/project operations or timestamps; it must not implicitly delete worktrees.
- Address export's long-lived SQLite read snapshot for very large sessions; consider general artifacts and export clipping.
- Improve cross-provider conversion beyond text and tool calls; foreign reasoning is currently dropped. Copilot Responses encrypted reasoning replay is missing.
- Determine whether sandboxing or restricted command execution is needed to enforce read-only agents and read-before-edit beyond the file tools, including races with external writers.
- Add an API view and pruning for file observations; verify `wait: false` child sessions with a real provider.
- Revisit Responses `instructions` if the Codex backend changes; it currently accepts it although the Codex CLI sends a developer message.

## Release gate (before 0.10.0)

- **O1:** isolate kernels in a scrubbed helper process.
- **O2:** back up before migrations and recover from a failed migration.
- **O3:** default to `~/.nexus`, with a one-time confirmed migration of `~/.nexus10`; move old `~/.nexus` aside, never delete it.
- **O4:** verify `nexus login copilot|grok|opencode` end to end and add tests.
- **O5:** define and run the macOS/Linux platform matrix.
- **O6:** pin `nexus-search` and browser-use images and versions; test the Apple container path. Add release-download installation support.

## Decisions still open

- Whether a parent card can steer a child session.
- Output caps (lines vs. bytes) and whether they are configurable.
