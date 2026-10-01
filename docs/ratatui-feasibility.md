# Feasibility: a Rust/Ratatui TUI with Python and maturin

Assessment date: 2026-10-01. Status: **proposal, not an approved migration**.
Repository baseline: `nexus-harness` 0.2.13, Python ≥3.13, Textual 8.2.8.
This report changes documentation only. No Rust prototype or comparative benchmark
has been run; performance projections and effort estimates below are not verified.

## 1. Recommendation

Replacing Nexus's Textual terminal application with a Rust/Ratatui application
is technically feasible while keeping the daemon, providers, tools, sessions,
context assembly, canonical reducer, CLI, and browser application in Python or
their existing languages. Nexus already has the most important prerequisite:
the terminal UI is a host client rather than the owner of agent execution.

The recommended design is a **Rust presentation and input layer, exposed through
PyO3 and packaged with maturin, with a Python host-client adapter**. Rust owns
terminal rendering, editor interaction, focus, hit testing, and temporary screen
state. Python owns host commands, subscriptions, replay, semantic projections,
attachments, and existing Python integrations. This confines Rust to the TUI
and avoids a second implementation of the agent harness.

Proceed first with a measured prototype, not a complete replacement. Ratatui can
plausibly reduce rendering and input-processing overhead, but changing languages
does not establish that the current bottleneck is rendering. Feature parity and
native-wheel distribution are likely to cost more than the initial renderer.
A faster small chat demo is insufficient evidence for migrating Nexus.

| Question | Assessment |
| --- | --- |
| Can all agent execution remain Python? | Yes, using the existing daemon contract. |
| Can Python remain the installed entry point? | Yes; load a native extension only when launching the Rust TUI. |
| Does maturin connect Rust to Python at runtime? | No; PyO3 supplies bindings, maturin builds and packages them. |
| Is Ratatui a drop-in Textual replacement? | No; screen composition and interaction need a substantial rewrite. |
| Will model responses or tools execute faster? | No inherent improvement from changing the client. |
| Are speed and memory gains established? | No; benchmark the actual Nexus workloads. |
| Is a staged migration practical? | Yes; keep Textual selectable until parity and distribution gates pass. |

## 2. Existing boundaries and reusable work

The current architecture is documented in [architecture.md](architecture.md),
[host.md](host.md), [textual.md](textual.md), and [surfaces.md](surfaces.md).
The launch seam is `nexus/ui/tui/run.py:run(client, session, reconnect)`, which
creates `NexusTextualApp` and awaits `run_async()`. The CLI has already created
the host client before this seam is entered.

`nexus/ui/tui/controller.py:TuiController` has no Textual imports. It manages
client commands, stream tasks, selection metadata, and the canonical Python
reducer. It is a useful candidate for extraction or adaptation, although its
lifecycle and callbacks still need review before reuse by a new shell. Its
bootstrap currently obtains a baseline sequence and hydrates the typed view
through event replay. Replacing widgets alone will not remove that startup work.

The authoritative representation remains `ConversationView` from `nexus/view/`.
Its durable contents are reduced from events; native UI caches must not become
another source of truth. Session storage remains SQLite in the Python daemon.
The Rust application must never open the database, call managers directly, or
read workspace files to construct tool diffs.

Python components that can remain in service include:

- `nexus/client/` and the UDS transport for connection and commands.
- `nexus/view/model.py` and `reduce.py` for replay and semantic state.
- Host-backed attachments, settings, permissions, questions, and voice commands.
- Pure presentation helpers such as `ui_support/tool_details.py`, after defining
  a native-friendly representation of their labelled sections and rows.
- Slash-command specifications in `nexus/ui/cli/commands.py`, fuzzy matching and
  prompt-history helpers where their interfaces are sufficiently independent.

The Textual-dependent `ui/tui/` widgets and `ui_support/tui_*.py` screens do not
carry over as native widgets. TCSS must become Rust layout and style definitions.
The browser remains plain HTML/CSS/JS, but its parity obligations remain active.

## 3. What Ratatui supplies, and what Nexus must supply

Ratatui uses immediate-mode rendering: an application draws widgets into a cell
buffer, and the terminal machinery compares buffers to emit changed cells. This
is a useful rendering foundation; it does not automatically virtualize the
transcript or eliminate the cost of constructing each frame.
[Ratatui rendering documentation](https://ratatui.rs/concepts/rendering/under-the-hood/).

Ratatui documents several application patterns rather than prescribing one
complete application architecture. Nexus should choose explicit update/render
separation, with typed actions and an application-owned focus model.
[Ratatui application patterns](https://ratatui.rs/concepts/application-patterns/).

Crossterm is a reasonable initial terminal backend and event source. It exposes
key, mouse, resize, focus, and paste events; some capabilities require enabling
terminal modes. Its event API also has constraints on mixing synchronous reads
and asynchronous streams. Choose one event-reading owner and test actual terminal
behavior rather than assuming modifier support everywhere.
[Crossterm event documentation](https://docs.rs/crossterm/latest/crossterm/event/index.html).

Nexus still needs to implement or select editor, Markdown, diff, completion,
selection, focus, dialogs, autosave feedback, and clipboard behavior. Third-party
Rust widgets may help, but compatibility, maintenance, licenses, Unicode handling,
and large-input behavior must be evaluated individually. No editor or Markdown
crate has been validated for Nexus in this assessment.

## 4. Integration options

| Option | Python responsibility | Rust responsibility | Tradeoff |
| --- | --- | --- | --- |
| A. PyO3 extension plus Python adapter | Existing client, reducer, commands, integrations | Renderer, editor, terminal loop, focus, local screen state | Recommended; best fit for keeping everything else Python, but requires careful threading. |
| B. Native subprocess plus Python bridge | Same responsibilities, communicating over private IPC | Standalone terminal application | Better crash isolation; adds another transport, packaging and process lifecycle. |
| C. Rust client talks directly to daemon | Daemon and harness | TUI plus transport bindings and state interpretation | Viable architecture, but expands Rust beyond presentation and risks duplicated contracts. |
| D. Keep Textual, accelerate selected functions | Existing complete UI | Measured parsing/layout hotspots only | Smaller change; useful comparator if a few functions dominate. |

### Preferred boundary

```text
nexus chat (Python CLI)
  ├── Python adapter: asyncio, Client, canonical reducer, host actions
  │      └── existing UDS connection → Python daemon → Python harness
  └── PyO3 extension, built with maturin
         └── Rust: Ratatui + terminal backend + editor + presentation caches

Python → Rust: versioned presentation updates and command results
Rust → Python: typed user actions and lifecycle notifications
```

Do not expose arbitrary Python callbacks to individual Rust widgets. The bridge
should have a small documented vocabulary: initialize, deliver update, drain
actions, notify error, request shutdown, and close. These are proposed operations,
not existing APIs. Batch transfers across the boundary; avoid one call per cell,
widget, character, or rendered line.

Rust may have a mirror of presentation data, but it should not independently fold
Nexus domain events. Python reduces every event and projects changed entities
into Rust. Transporting JSON internally is acceptable; displaying a raw JSON dump
to users is not. Start with owned serialized bytes if that simplifies the
prototype, measure encoding/copying cost, and replace with typed transfers only
where measurements justify the additional complexity.

### Presentation update contract

A proposed update envelope should include a schema version, session id, session
selection generation, monotonic presentation revision, and durable event cursor
where applicable. The generation distinguishes late responses from an old
session selection; the presentation revision also covers host-query results that
are not represented by a new durable event sequence.

Use a complete initial snapshot, then bounded updates keyed by durable turn,
tool, agent, or context ids. Track separate invalidation for presence, composer,
context, dialogs, and transcript. Include an explicit resync operation for an
unknown revision, overflow, or incompatible bridge schema. The existing browser
projection is useful prior art; reuse it only after auditing field coverage,
clipping, limits, and assumptions. Do not assume browser patches are a ready-made
terminal schema.

Keep complete domain information accessible through labelled detail views.
Rendering only visible rows must not remove offscreen information. Existing
bounded or clipped values retain their disclosure, and large host responses
remain subject to the current size limits.

## 5. Async execution, threading, and terminal ownership

Python already uses asyncio for daemon communication. A native terminal loop
must not block that loop or hold the GIL while waiting for keys or drawing.
PyO3 documents detaching Rust work from Python so other Python work can proceed.
The exact API must match the pinned PyO3 version.
[PyO3 parallelism documentation](https://pyo3.rs/main/parallelism).

A concrete starting design is:

1. Keep Python's asyncio adapter on the main thread so existing signal and client
   lifecycle handling remain coherent.
2. Run the Rust terminal loop on one dedicated thread. That thread exclusively
   owns terminal reads, writes, raw mode, and screen state. Validate this choice
   on every supported OS and terminal before committing to it.
3. Pass owned data through bounded native queues. Rust never accesses a borrowed
   Python object after leaving a PyO3 call. UI mutations happen on the Rust thread.
4. Wake asyncio with a thread-safe notification mechanism when actions arrive.
   An initial bounded polling implementation may be acceptable for a prototype;
   include its latency and idle CPU in measurements.
5. Dispatch host actions asynchronously in Python. Show pending state immediately
   in Rust; commit authoritative changes only after the result or event arrives.
6. On exit, stop accepting actions, restore the terminal, stop client tasks, and
   join the Rust thread with a bounded shutdown protocol.

Avoid holding queue locks while acquiring the GIL or calling Python. Use explicit
request ids for asynchronous results, and cancel or reject stale requests after
session switches. Surface timeouts and disconnected state visibly.

Presentation snapshots may be replaced by newer snapshots after Python has
reduced all events; user actions, approvals, questions, and error notifications
must not be silently dropped. Reserve capacity or use separate queues for
critical traffic. If the adapter cannot keep up, show overload and resync rather
than drawing a permanently stale transcript.

Raw mode, alternate screen, cursor visibility, mouse capture, paste mode, and
keyboard enhancement flags need restoration on normal exit, Python exceptions,
Rust errors, and unwinding panics. Panic-abort and forced termination cannot
promise cleanup. Native crashes can kill the Python client process; the separate
daemon should continue running. Closing the UI must not cancel the active turn.

Only one component may read stdin or render stdout. Python logs must go to the
existing logging path or an explicit Rust log presentation channel. Preserve
non-TTY errors and the existing `nexus run` alternative.

## 6. Performance hypothesis and measurement

The most plausible gains are lower client CPU for layout and cell generation,
more predictable rendering during bursts, a smaller widget-object footprint,
and quicker local editor feedback. Those are engineering hypotheses, not
benchmark results. Python and Rust both remain resident in option A, so total
memory does not become the footprint of a standalone Rust executable.

The unchanged costs include provider latency, tool execution, daemon scheduling,
SQLite persistence, Python replay/reduction, and most connection setup. Wheel
installation speed may actually worsen if a missing wheel triggers compilation.

Nexus already avoids resynchronizing the timeline for presence updates and avoids
reconciling unchanged turns. `tests/test_tui_submit_latency.py` checks these
properties. [textual.md](textual.md) records a historical delay of about 0.6 s
before that optimization; this is not a current baseline or a Rust comparison.
Preserve that optimization in the prototype and profile the current code first.

For illustration only: if rendering is 40% of a client operation and native
rendering is five times faster, total speedup is
`1 / (0.60 + 0.40 / 5) ≈ 1.47×`, not five times faster. If rendering accounts for
10%, the same improvement gives about `1.09×`. These are hypothetical inputs.

### Benchmark plan

Use deterministic scripted providers and identical event fixtures. Measure the
current Textual app, a reasonably optimized Textual baseline, and the native
prototype with the same semantics, transcript size, widths, and visible panels.
Use release-mode Rust builds; record machine, OS, terminal, Python/Rust versions,
fixture hash, sample count, and cold versus warm runs.

| Workload | Measurements | Why it matters |
| --- | --- | --- |
| Cold launch and reconnect | Time to usable composer; time to fully hydrated transcript; import and replay breakdown | Native rendering may leave Python startup unchanged. |
| Editor under event load | p50/p95/p99 input-to-visible latency, including completion and paste | Measures perceived responsiveness. |
| Streaming text and reasoning | Event-to-display latency, CPU, queue depth, allocation rate | Reveals batching and Markdown costs. |
| 10 / 100 / 1,000 turns | Scroll and resize latency, RSS, retained caches | Detects history-dependent work. |
| Large tool result and multi-file diff | Open/scroll latency, peak memory, lost or clipped data | Exercises detail rendering. |
| Parallel tools and child agents | Root and child update latency, session-switch races | Tests real event diversity. |
| Idle and reconnect storms | CPU, wakeups, stale updates, terminal output bytes | Exposes unnecessary redraw and polling. |

Measure both isolated stages and end-to-end PTY behavior. A fast draw call can
still produce slow terminal display or remote SSH output. Buffer comparison does
not make transcript parsing free, and high frame rates can increase output costs.

Suggested prototype targets, to be agreed before implementation: p95 local
input-to-visible latency below 50 ms under the agreed load; a substantial measured
improvement in a currently failing workload, such as ≥30% lower client CPU;
no correctness regression; and no major startup or memory regression. These are
proposed acceptance criteria, not claims about achievable performance. Prefer
ratios plus absolute budgets on specified hardware to flaky universal timing tests.

### Necessary rendering design

Draw on invalidation rather than running a perpetual animation loop. Coalesce
stream updates at a bounded frame cadence, initially testing 30–60 Hz during
activity, with immediate scheduling for interaction and approvals. Idle screens
should sleep until an event or an actual animation deadline.

Virtualize transcript rows, preserve scroll anchors when content grows, and cache
Markdown and wrapped line heights by content revision, width, theme, and expansion
state. Parse only changed blocks where possible; streaming Markdown requires
care around incomplete code fences and lists. Resizes invalidate width-dependent
layout. Bound caches independently of the durable log. Presence changes must not
reparse history. Ratatui alone does not provide these application policies.

## 7. Feature parity and rewrite scope

| Area | Existing behavior to preserve | Work/risk |
| --- | --- | --- |
| Shell layout | Tabs, breadcrumb/status, sidebars, logs, composer rows, dock/overlay breakpoints | Medium; explicit layout and hit regions. |
| Transcript | Markdown, thinking expansion, usage footer, queued messages, tool activity, child pages | High; streaming and virtualized variable-height content. |
| Context | Literal system prompt, AGENTS.md, schemas, skill/MCP switches, measured/estimated tokens, clipping disclosure | High; completeness is a release gate. |
| Editor | Multiline editing, Unicode, paste pills, history, slash/argument/file completion, selection | High; prototype this early. |
| Keys | Shift/Ctrl/Alt+Enter, Ctrl+J, Ctrl+X leader, Escape cancellation, Ctrl+C, focus restoration | High; terminal protocols and legacy fallbacks. |
| Approvals/questions | Docked choices, inert background, daemon arbitration, reconnect | High correctness importance. |
| Settings/setup | Sections, scope, editor, 700 ms autosave, validation, conflicts, reset, provider flows | High; many interaction states. |
| Tool details/diffs | Every parameter/output, labelled sections, durable split diffs, bounded live tails | High; cannot simplify to an opaque summary. |
| Attachments | Host preparation, reference labels, previews, clipboard images, session-switch clearing | Medium/high; retain Python conversion and OS helpers. |
| Voice | Python capture/transcription integration, stop/discard rules, overlay, progress | Medium/high; presentation changes, model/runtime stays Python. |
| Session operations | Archive/trash/restore/export, search, fork, metadata, reconnect | Medium; host calls reusable, screens rewritten. |
| Worktrees/models/usage | Inspection and actions, searchable model picker, favorites, tiers, usage refresh | Medium/high; preserve existing data and controls. |
| Themes/preferences | Dark/light palette, saved panels/context preferences | Medium; migrate TCSS, preserve preference compatibility. |

The editor should test grapheme boundaries, wide characters, combining marks,
emoji, tabs, word movement, selection, undo/redo, and wrapping. Distinguish
bracketed paste from executable key input. Do not let pasted control sequences
trigger actions. Port terminal-content escaping for context, logs, tool output,
and literal prompt text. Raw-mode rendering must not execute untrusted escapes.

Keep the web application's established placement, wording, commands and shortcuts.
A migration can change the TUI implementation without redesigning the product.
If an intentional behavior change becomes necessary, implement and document it
across both surfaces.

## 8. Packaging with maturin

Maturin supports mixed Python/Rust projects and a native extension imported as a
Python submodule. That makes a private module such as `nexus._tui_native` a
reasonable target; no package-directory move is inherently required.
[Maturin project-layout documentation](https://www.maturin.rs/project_layout.html).

The current build backend is setuptools. A single-distribution migration would
replace it with maturin, add Cargo configuration and the PyO3 crate, preserve
`[project.scripts] nexus = "nexus.cli:main"`, and explicitly verify packaged Python
modules and assets. Backend replacement is a packaging change even when all
non-TUI code remains Python.

Illustrative layout, not a tested scaffold:

```text
pyproject.toml                 # Python metadata + maturin backend/settings
nexus/                         # existing Python package
  ui/tui_native/               # Python adapter and native launch seam
rust/tui/
  Cargo.toml                   # cdylib, PyO3, Ratatui, terminal backend
  src/lib.rs                   # extension entry and bridge
  src/app.rs                   # native screen state and actions
  src/render.rs                # layout/rendering
  src/editor.rs                # editor integration
Cargo.lock                     # reproducible application dependency resolution
```

A possible configuration shape is `manifest-path = "rust/tui/Cargo.toml"`,
`python-source = "."`, and `module-name = "nexus._tui_native"` under
`[tool.maturin]`. Validate exact module naming, package inclusion, and Cargo
library naming in the prototype. Pin a compatible tested toolchain and dependency
set rather than copying moving documentation versions into production.

An alternative is a separately published `nexus-tui-native` wheel built with
maturin while `nexus-harness` stays on setuptools. This limits the build-backend
change and can make native installation optional during rollout, but adds release
coordination, compatibility checks, and another distribution. Prefer it if
unsupported native targets must still install the Python CLI/web without Rust.
A Textual fallback alone cannot solve failure to build a mandatory native package:
the fallback is useful only after installation succeeds.

### Wheels and release implications

Native extensions need platform-compatible wheels. Maturin documents Linux
compatibility tags and build/distribution options; PyO3 describes Python-version
compatibility and the optional limited-API (`abi3`) approach.
[Maturin distribution guide](https://www.maturin.rs/distribution.html),
[PyO3 build/distribution guide](https://pyo3.rs/main/building-and-distribution).

Plan and test at least macOS arm64/x86_64 and Linux glibc x86_64/arm64 if those
are the intended product targets. Decide explicitly whether musl and Windows
remain supported for installation and whether the TUI is supported there. A
portable terminal backend does not remove the host transport's Unix-socket
assumptions. Audit `install.sh`, `install.ps1`, and update behavior before changing
the support promise.

Normal users should receive wheels without installing Rust. A source distribution
needs Rust and a linker and may need additional build prerequisites. Document
that path; account for offline installation and Cargo dependency availability.
`abi3` may reduce the Python-version wheel matrix, but does not eliminate OS,
architecture, libc, or free-threaded-build considerations. Its suitability is
not verified; test the chosen PyO3 APIs and supported Python configurations.

The current release workflow builds on Ubuntu with `uv build` and publishes from
that job. A native migration needs a build matrix, collected artifacts, clean
wheel-install smoke tests, and publishing only after all required targets pass.
Preserve release-please version authority and the current trusted-publishing
workflow identity. Do not change Python version files manually for this migration.

Package tests must cover the offline model catalogue and attribution, agent
prompts, web assets, search-server configuration, voice/auth notices, native
extension, and temporary Textual assets. Verify the sdist too. Native linkage,
minimum macOS/glibc versions, wheel tags, and extension import must be checked in
clean environments; a developer's successful import is insufficient.

Lazy-load the extension only for native TUI launch. Importing `nexus`, using
`nexus run`, or serving the browser should not initialize terminal state. Keep
CLI help, doctor, and non-TUI workflows usable independently of the native renderer.

## 9. Testing strategy

Keep Python host, reducer, providers, storage, security, and web tests. Preserve
the behavior represented by Textual tests, but rewrite UI-specific assertions:
Textual Pilot, widget queries, TCSS layout checks, and SVG export cannot directly
test a Ratatui shell.

Ratatui documents backend-driven application testing. Use its test backend for
cell-buffer layout assertions at multiple widths, with deterministic state and
clocks. Add action-state tests for focus, dialogs, selection, and cancellation.
[Ratatui testing documentation](https://ratatui.rs/recipes/testing/).

Add Python/native bridge tests for schema negotiation, queue limits, stale
session results, resync, GIL-safe shutdown, and host error presentation. Replay
identical fixtures through Python reduction and native presentation, checking
that parameters, outputs, approvals, context, and clipping remain accessible.
Include settings failures, conflicting saves, voice cancellation, attachment
labels, and reconnect during a pending permission.

Use PTY integration tests for real key bytes, paste, resize, EOF, suspend/resume
where supported, and terminal restoration. Check local terminals, SSH and tmux,
including a terminal without enhanced keyboard protocols. Native snapshots alone
cannot verify those interactions.

Existing `textual-serve` browser checks do not carry over. A replacement browser
terminal harness would require a PTY plus terminal emulator and suitable input
encoding, or those checks can move to PTY coverage. Keep browser tests for the
actual web app. Do not delete Textual coverage while Textual remains supported.

Existing Python import-layer tests cannot enforce Rust boundaries. Add an
explicit native dependency/bridge policy and checks: no database access,
credentials, direct tools, or manager calls in the presentation crate. Document
new Python modules in [module-map.md](module-map.md) when implementation begins.

## 10. Staged migration and approximate effort

These are planning estimates for one engineer comfortable with both languages,
not a delivery commitment. They include verification and assume reusable Python
helpers. Editor/library problems or terminal compatibility can substantially
increase the range. Time ranges are working weeks and largely sequential.

| Stage | Deliverable | Approximate effort | Exit condition |
| --- | --- | --- | --- |
| 0. Baseline | Profiles, fixtures, parity inventory, support matrix | 1–2 weeks | Rendering bottleneck and acceptance budgets identified. |
| 1. Vertical prototype | Maturin wheel, Python bridge, real scripted host, editor, streaming transcript | 2–4 weeks | End-to-end gains; no GIL stall; install and cleanup demonstrated. |
| 2. Core parity | Virtualized history, context, tools/diffs, approvals, sessions, shortcuts | 4–7 weeks | Core functional journeys and replay coverage pass. |
| 3. Full surface parity | Settings/setup, providers, voice, attachments, worktrees, all pickers | 4–8 weeks | Feature inventory complete; web parity preserved. |
| 4. Release hardening | Wheel matrix, installers/update, PTY tests, docs, fallback | 2–4 weeks | Supported clean installs and terminals pass. |

That is roughly 13–25 engineer-weeks for a supported full replacement under these
assumptions. A narrow prototype can be useful in 2–4 weeks after baseline work;
it is not representative of complete Settings/editor/terminal parity. These
estimates were not derived from implementing a prototype and should be revised
after stage 1.

Introduce an explicitly selectable experimental renderer through the launch
seam, while keeping Textual the default. Backend selection syntax is a future
product decision; no new command or config key is introduced by this report.
Freeze bridge schema versions during each release and make compatibility errors
clear. Preserve saved preference semantics and rollback without altering durable
session data.

Switch the default only after performance, correctness, feature coverage, wheel
availability, and terminal compatibility gates all pass. Retain the old backend
for a defined transition window. Remove Textual and `textual-diff-view` only after
all remaining imports, tests, dev dependencies, docs, fixtures, and packaging
references have been audited. Backend import failure can permit an early fallback;
a crash after terminal startup needs reliable cleanup before any restart attempt.

## 11. Decision gates and unresolved questions

**Proceed to a prototype** if profiling shows a meaningful client bottleneck and
the team accepts maintaining Rust alongside Python. **Proceed to replacement**
only if the realistic prototype improves the agreed workloads and the full parity
and release work is funded. **Keep optimizing Textual** if the bottleneck is replay,
Python projection, host work, or terminal output rather than rendering.

Before implementation, resolve:

- Which workload is currently slow, on which hardware and terminal?
- Is a native wheel required for every existing installation target, or should
  the TUI be an optional separate distribution?
- Can the existing controller be reused cleanly, or should its Python lifecycle
  be extracted into a neutral adapter?
- Which editor and Markdown approach satisfies Nexus's existing interactions?
- What fraction of work crosses the Python/native boundary, and does batching
  keep that overhead below the performance savings?
- Does threaded terminal ownership work reliably on the intended targets?
- How will PTY tests replace the current Textual-specific browser/visual tooling?

The architectural fit is strong. The performance outcome remains unverified.
The valuable design is a native TUI whose boundary preserves Nexus's Python
contracts and complete context presentation; maturin makes shipping that boundary
practical, but feature reconstruction and release engineering determine whether
the migration is worthwhile.
