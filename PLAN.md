# Nexus 0.10: merge to `main` and release 0.10.0

Goal: merge branch `nexus10` into `main` and publish 0.10.0. Current behavior is in [docs/README.md](docs/README.md); historical porting decisions are in the [porting ledger](docs/porting-ledger.md). Old Nexus (`~/repos/nexus`, branch `nexus01`) is reference only.

Preserve the visible, ordered context timeline, announce clipping, keep task tools separate from Nexus control, and do not rewrite context already sent to a provider. State is in `~/.nexus` after `nexus migrate`; never run it against the real home from a session.

## Where things stand (2026-10-30)

- `main` and `origin/main` are old Nexus 0.7.2 (PyPI `nexus-harness`). `nexus01` is pushed (2026-10-30) at that commit plus one backup commit holding old Nexus's 18 uncommitted local changes (`c3f1848`); `~/repos/nexus` is now checked out on `nexus01`. `nexus10` has its own root commit and shares no history with `main` (corrected 2026-10-30; an earlier version of this plan called it a fast-forward). A plain push would need a force-push, so the merge is `git merge --allow-unrelated-histories -s ours origin/main` on `nexus10`: the tree stays exactly `nexus10`'s, old `main` becomes the second parent, and `git push origin nexus10:main` is then a fast-forward. Branch protection on `main` is off (the one ruleset is disabled), so no PR is required. The tree layout is entirely different (`nexus-core/`, `nexus-tui/`, `nexus-nsh/` replace `nexus/` and `rust/`), so `main` stops being old Nexus the moment it moves.
- Release gate O1 to O4 are done (kernel helper, migration backup and recovery, `nexus migrate`, device logins). O5 and O6 are below.
- CI and release workflows are written in `.github/workflows/` (adapted from old Nexus) but have never run. Release-please files: `release-please-config.json`, `.release-please-manifest.json`.
- About 55 files are modified and uncommitted, plus `migrate.py` and three test files. Nothing below can land until they are committed.

### Linux check (Debian 13 x86_64, 2026-10-30, a scratch folder on the build box, since deleted)

| Part | Result |
| --- | --- |
| Core: `uv sync`, `ruff check`, `pytest` | clean; 767 passed, 11 skipped; `nexus doctor` runs |
| TUI release build | builds, **only** with `pkg-config` and `libasound2-dev`; without them `alsa-sys` fails (`rodio` → `cpal`). This is the likely cause of the earlier Linux failures, and old Nexus's wheel build needed the same ALSA step |
| TUI tests | 317 + 11 + 55 pass; 1 fails: `agent_and_model_chosen_before_a_session_exists_go_into_its_creation` (`left: None, right: Some("plan")`). It fails identically on macOS, so it is not a Linux problem |
| NSH (`nsh check`, build) | **not run**: installing `nsh` on the box was not permitted |
| `install.sh` | **not run** (it needs `nsh` through npm) |
| aarch64 Linux, Alpine/musl, older glibc | **not run** |

## Steps to merge and release

### 1. Make the tree green and committed
1. Commit the working tree in logical commits (the `migrate` work, the docs, the TUI/NSH changes). Use Conventional Commit titles; release-please reads them.
2. Fix the failing TUI test above (also red on macOS). CI will be red until then.
3. Run `nexus check` locally (Core, TUI, lints, manifest/theme checks, NSH checks).
4. Fix the NSH mock event stream (it ends and puts the app in reconnecting state) and rerun the browser suite; the recorded run had 13 failures, so do not assume they are unrelated. `nsh test --target web` needs an explicit Playwright Chromium path on this machine. If the fix does not land, record the failing tests as known and keep the suite out of `ci-ok`.

### 2. Platform matrix (O5)
Cells to fill, each with install, `nexus doctor`, daemon start, TUI against the daemon, NSH web, a scripted turn and a Monty kernel run:

| Platform | Status |
| --- | --- |
| macOS arm64 | development machine; run the full list once from a clean `$NEXUS10_HOME` |
| macOS x86_64 | not run |
| Linux x86_64 glibc | Core and TUI done (above); NSH, `install.sh`, a live daemon + TUI session and kernels remaining |
| Linux aarch64 glibc | supported: the release builds a binary on `ubuntu-22.04-arm`; run the same checks as x86_64 on real hardware or a runner before release |
| Alpine/musl | not supported at 0.10.0: `install.sh` rejects it with a clear message (decided; old Nexus did the same for musl x86_64). Revisit after release |

Linux needs, in the installer's checks and in `docs/development.md`: `pkg-config`, ALSA headers (`libasound2-dev` / `alsa-lib-devel` / `alsa-lib-dev`), a C toolchain, `git`, `uv`, Rust, Node 22 and `nsh`. Release TUI binaries are built on Ubuntu 22.04 (glibc 2.35) so older distributions can run them. The Monty kernel launcher uses `/usr/bin/env`; confirm the path on Linux (tests passed here, but the scrubbed-environment worker, forced worker crashes and cold start are not covered).

### 3. Installation (O6)
`install.sh` today builds from a checkout (`uv tool install`, `cargo build --release`, `npm run build`) and requires `nsh` via npm. For 0.10.0, add release-download installation:
- download `nexus-tui-<os>-<arch>`, `nexus-web.tar.gz` and the Core wheel from the GitHub release, verify against `SHA256SUMS`, and install with `uv tool install <wheel>`;
- keep the from-checkout path behind a flag;
- reject unsupported platforms with a diagnostic; do not install `nsh` for a release install (the web bundle is prebuilt);
- add an `install` workflow (clean runner, rerun is an idempotent upgrade, `nexus update`) and a nightly check of the published script; old Nexus's `install.yml` is the model. `nexus update` currently only reports; decide whether 0.10.0 installs updates.

### 4. CI and CD (written, not yet exercised)
Adapted from old Nexus (`~/repos/nexus/.github/workflows`):
- `ci.yml`: `changes` (skips release-only PRs), `lint` (ruff, manifest and theme checks, `cargo fmt --check`, `uv build`), `core` (pytest on macOS and Ubuntu 24.04), `tui` (cargo test + release build on macOS and Ubuntu 22.04 with ALSA headers), `nsh` (check, host tests, build), and `ci-ok`, the one required check.
- `build-artifacts.yml` (reusable): TUI binaries for `ubuntu-22.04`, `ubuntu-22.04-arm`, `macos-15`, `macos-15-intel`; the NSH web bundle; the Core wheel and sdist.
- `release.yml`: release-please opens a release PR on every push to `main`; merging it tags `v0.10.0`, builds, and attaches the artifacts, `install.sh` and `SHA256SUMS` to the GitHub release. **No PyPI publish**: old Nexus published `nexus-harness`, and 0.10 is not packaged that way.
- `pr-title.yml`: copied unchanged.

Before relying on them:
1. Push the branch and let `ci.yml` run on a PR into a scratch branch; fix whatever the runners find (the NSH job and the Ubuntu jobs have never run on GitHub).
2. Set the repository's required check to `ci-ok`, and confirm the `RELEASE_APP_ID` variable and `RELEASE_APP_PRIVATE_KEY` secret exist for the release-please app token (the old workflow used them).
3. `nsh` is pinned to `v0.3.0` (decided). The workflows install it with `curl https://nsh.dev/install.sh`; `install.sh` still says `npm install -g nsh`, so make the from-checkout path use the same source and version.
4. Release-please: `release-as: 0.10.0` is set so the first release is 0.10.0 whatever the commit history says; remove it afterwards. The manifest starts at `0.7.2` (old Nexus's last release); the changelog will include every commit since `v0.7.2` unless `bootstrap-sha` is set. Verify the `extra-files` updaters against a dry run, and bump the three versions (`0.10.0.dev0`, `0.10.0-dev.0`, `nsh.json` `0.10.0`) consistently.
5. Dry-run the release with `workflow_dispatch` on a pre-release tag before the real one.

### 5. Verification with real providers and clients (blocking)
- Real OpenAI and Ollama calls. Codex and the device-flow providers (Copilot, Grok, OpenCode) are done.
- **Claude through the Agent SDK is in scope for 0.10.0** (decided). See 5b below.
- Real daemon with both clients: model memory (no default model), sign-in from each client's Providers page, the remaining turn-summary checks in [TURN_SUMMARY_PLAN.md](TURN_SUMMARY_PLAN.md), steer/queue ordering, and OSC 7501 in a supporting terminal.
- A fresh-machine run of `nexus migrate` against a copy of a real old `~/.nexus` (never the real home from an agent session).

### 5b. Claude Agent SDK (blocking)

Landed 2026-10-30 (uncommitted): provider `claude` in `nexus-core/nexus/llmhub/claude.py` and `_claude_worker.py`, sign-in in `auth/claude.py`, `claude-agent-sdk==0.2.162` in `pyproject.toml`, 17 tests in `tests/test_claude.py` (full suite: 795 passed), docs in `docs/llm-providers.md` and `docs/auth.md`, a ledger row. Design and limits are in [docs/llm-providers.md](docs/llm-providers.md#claude-agent-sdk). A real request through the provider with `claude-haiku-5-5` ran a tool turn and a final-answer turn correctly (both cost about $0.01). The SDK is used as a plain client; Nexus owns the loop.

Findings to act on:
- Hidden context, found and measured (details in [docs/llm-providers.md](docs/llm-providers.md#claude-agent-sdk)): the CLI made a second hidden request per query to title the session, with the whole history in it. Fixed in the worker with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. It still adds, and cannot be told not to: two system lines, a `StructuredOutput` tool, the account's **email address** in the first user message, and an `# Environment` message (cwd, platform, shell, OS version, date). The worker now runs in an empty `$NEXUS10_HOME/claude-sdk`, which cut a real turn from 1,766 to 1,259 input tokens. These are listed in the wire request (`sdk_adds`) but not yet in the clients' starting-context view: decide whether to show them there, and whether sending the email is acceptable (it goes to Anthropic, the account's own provider). The 420 tokens of fixed overhead is unexplained.
- The CLI logs `unrecognized_model` for `claude-haiku-5-5` but serves it. models.dev lists the model, so the catalog is fine; confirm the context window the clients show.
- Size, resolved: Nexus does not bundle Claude Code. The user installs it; without it only the Claude provider errors ("Claude Code is not installed"). The SDK's platform wheels carry a 223 MB copy, so `pyproject.toml` sets `[tool.uv] no-binary-package = ["claude-agent-sdk"]` and the SDK installs from source (560 KB). `uv tool install` ignores that table: `install.sh` now passes `--no-binary-package claude-agent-sdk` on the from-checkout path (the release-download path must do the same), and CI/release `uv sync`/`uv build` need a build-capable runner. Check that the pin holds in `uv.lock`, in CI, and in a `uv tool install` of the built wheel.

Remaining:
1. Say in Settings/docs which Claude Code version is needed (2.1.248 or newer) and show "Claude Code is not installed" on the Providers page when `cli_path()` is empty. Hidden additions vary with the user's CLI version (about 175 more tokens on 2.1.314 than on 2.1.285), so record the CLI version in the wire request.
2. Run both clients against a real daemon: the Providers page shows "Claude (Agent SDK)", connect opens the URL, a session with a Claude model streams one buffered reply, a tool turn and an interrupt. Both clients post the provider's first method (`cli`); the pending row shows the message with the URL.
3. Show the limits in the timeline: a "buffered, no thinking" note for a Claude model and a clear failure for images (the error text exists; check how each client renders it).
4. Plan-limit usage through the CLI (`-p /usage`, a local command that makes no model request) in `provider_usage.py` and `/api/usage`.
5. Interrupts: confirm cancelling a turn kills the worker's process group and the CLI child.
6. Mark the provider `verified` after a multi-tool turn with a real model; add wire fixtures from that run.

### 6. Merge
1. `nexus01` (pushed) and the `v0.7.2` tag are the old line. Old Nexus users on PyPI keep `nexus-harness 0.7.2`; say so in the release notes and README, since `main` no longer contains that code.
2. Merge with the `ours` strategy as above (no rebase: the histories are unrelated), push to `main` directly or through a PR, and wait for `ci-ok`. Do not squash the 116 commits. The push to `main` also starts `release.yml`, whose release-please job opens the 0.10.0 release PR (the app secrets are set); nothing is published until that PR is merged.
3. Update `AGENTS.md`, `README`, `docs/development.md` and `docs/porting-ledger.md`: `nexus10` and `nexus01` references become `main`; old Nexus is `nexus01`.
4. Merge the release-please PR; confirm the release has the binaries, the web bundle, the wheel, `install.sh` and `SHA256SUMS`; run the installer from the published release on macOS and Linux.
5. Remove `nexus-design-mockup/`, drop `release-as` from the config, and bump to 0.10.1-dev.

## Not blocking 0.10.0

Everything below ships in 0.10.x unless it turns out to affect the steps above.

### OSC 7501 program status

The TUI emits per-session OSC 7501 records; see [docs/development.md](docs/development.md#terminal-program-status). Verify the behavior in a supporting terminal, including reconnect, suspension and multiple simultaneous sessions.

### Turn summaries

Follow [TURN_SUMMARY_PLAN.md](TURN_SUMMARY_PLAN.md) for the remaining real-daemon checks, documentation and step 9 (live partials and ephemeral deltas). Preserve durable `input.*` events and the ordering of delivered steers; a steer is not a new turn. The TUI benchmark is in [docs/turn-summary-benchmark.md](docs/turn-summary-benchmark.md).

### Settings: Providers and Agents pages

The pages are in both clients ([config.md](docs/config.md#settings-layout)). Remaining: sign in to a real device-flow provider from each client's Providers page; NSH browser coverage for key entry, the device-code row and the Agents page edits once the mock stream is fixed (see Browser tests); PTY screenshots of both pages at 80×24 and 160×48. Agent cards show the file path but cannot open it: the built-in editor reads project files only, and global and built-in agents live elsewhere.

### Steer, queue and prompt history

- Add NSH browser coverage for Enter/Tab and the pending-input stack after fixing the mock stream (see Browser tests). The composer already steers and queues, and the timeline shows the TUI's `steer` / `queued` tags.
- Add Ctrl+R history search in both clients; verify cross-client per-project recall after restarting both clients, including multiline drafts and images.

### Extensions, skills, MCP and hooks

- Show agent and user-tool errors in client pickers and Settings; add a visible changed-file notice for a running tool, process memory limits and validation of project-only names in `[tools] disabled`. Exercise a user tool in a real session and run the reload stress test.
- Add default-on settings for skills and exercise skills with a real model. Decide whether and how `allowed-tools` should be enforced without changing a cached prompt or granting permissions.
- Add MCP resources and prompts and Settings screens for adding/editing servers; exercise browser-use (the pinned `uvx` package, `nexus-browser/`) in a real model session, including server failure; its screenshots and LLM-backed tools are unexercised. MCP output must remain bounded, fenced as untrusted and visible as received.
- Support editing hooks in Settings, render hook rows in both timelines, and fire `SessionEnd` on archive. Test `bash(...)` matchers on compound commands and `PostToolUse` after denied approval; verify a blocking hook with a real model.

### Providers

- Verify real OpenAI and Ollama calls.
- **J4 Anthropic:** add the Messages dialect using the Anthropic SDK as a plain client (Nexus owns the loop); preserve signed thinking blocks for lossless replay and handle cache-control breakpoints and tool-result media.
- **J5 Gemini:** choose a dialect that preserves replayable reasoning.
- **J6 Claude (Agent SDK) is now step 5b; Antigravity:** assess its fit with the one-timeline rule before porting. Use the Claude Agent SDK and the approach in https://github.com/NoeFabris/opencode-antigravity-auth rather than ZMX.
- Each provider needs wire fixtures, provider/auth documentation and a verified real call. Group OpenAI-compatible providers where possible.

### Context and sessions

- Session switching is slow (2026-10-30). Fixed: Core re-parsed the 6.4 MB models.dev cache on every context, draft-context and image-input call (~300 ms on the event loop each), and NSH's new-session view asked for the draft context on every model bump, so a streaming turn kept Core at 100% CPU. The NSH composer also re-read prompt history on every bump (thousands of requests per turn), and the `@` listing asked again while a request was in flight; both are fixed. Still open: after a long streamed turn, NSH's main thread stays busy for 5-7 s rendering it (present in the committed code too), so clicks in that window wait. The TUI now caches the last 8 session views and keeps them current from the stream; select loads history in the background with no resync, and git/worktree reads no longer block a switch (`--bench`: a switch 260 → 10 ms, back to a cached view 0.8 ms to first frame). NSH caches session views the same way. Both clients now run context reads one at a time per session and coalesce the rest, and the TUI no longer reads git and worktrees inline at launch (first open ~115 → ~50 ms). Core still rebuilds the whole request body for each `GET …/context` (warm 5–12 ms for a 1.4 MB body, 30–130 ms on a session's first read) and sends all of it to clients that mostly show `used`/`window`; a Core-side cache was not added because its invalidation would have to track everything `agent.request()` reads, and a stale one would show context the model does not have. Next: a lighter context endpoint (budget only) if the cost shows up again.
- Model memory (no default model) is in core, TUI and NSH; check it in a real daemon with both clients: a draft with no model asks for one (Core's draft-context request returns 400 "no model selected", so the starting-context preview is empty until one is chosen: consider a provider-independent estimate), a remembered model whose provider is signed out is shown and not replaced, and a session stored before the change that holds no model asks for one. The TUI test `agent_and_model_chosen_before_a_session_exists_go_into_its_creation` already failed before this change.

- Allow `/btw` during a running turn in both clients; add NSH browser coverage for opening the original session from a handoff.
- Return per-section starting-context detail text from Core for session drafts.
- Decide whether to persist session grants; expose grant listing and clearing in clients. Add a Settings section for provider limits and verify `/api/usage` with a real account.
- Add editor find and syntax highlighting; test NSH save and stale-file paths in the browser harness. Make NSH tool-result images zoomable.

### Worktrees

Port worktree review and integration as control commands: review against the base, merge by fast-forward or merge commit with confirmation, and remove the worktree and branch after merge. Add commands in both clients; agents get them only when granted. Consult old `nexus/agents/worktree_review.py`, `worktree_integrate.py`, `nexus/worktree_cleanup.py` and `tests/test_worktree_merge_permission.py`. Decide cleanup on archive/delete and how to leave a worktree.

### Interface

- Dictation: verify both clients' loading, start and stop cues and microphone behavior
  with a real model and audio device.

Agent pickers now filter the shared catalog by root/draft or child eligibility; verify
child-session picker navigation in the NSH browser suite once its mock stream is fixed.

#### TUI

- Verify Cmd+arrow composer shortcuts in a terminal that forwards Super-modified keys.
- Add `path:line` and OSC 8 link hit regions; choose a syntax-highlighting library for the file view.
- Fix split-diff continuation alignment (`nexus-tui/src/diff.rs`) and provide line numbers for edit/patch diffs where input lacks them.
- Add PTY tests at 80×24 and 160×48 for anchor, follow and jump-to-latest; produce a fixed-fixture review gallery (`interface/review.md`).
- Timeline mouse selection copies visible screen cells on release; consider selection across scrolling and copying underlying unwrapped text, and verify OSC 52 in real terminals.
- Add failed child count, enabled/disabled tools and kernels to session info.
- Check OSC 777 notifications in kitty (possibly OSC 99) and track focus; debounce completion requests if large workspaces make search slow.
- Compare with old Nexus in a real terminal and review screenshots.

#### NSH and shared

- Finish design parity for the new-session view, files, narrow layouts and states; improve sidebar search handling and live shimmer; sidebar rows show project or branch where available. User and assistant timeline prose now uses bundled Monaspace Argon; verify native rendering and visual parity. Pin NSH, build the macOS desktop bundle and complete files, Git and worktree UI; then remove `nexus-design-mockup/`.
- Subagent navigation now follows the TUI: root tabs exclude children, a viewed child keeps its root's tab, and the subagent strip under the header goes back to the parent and selects the root's children with running, waiting, failed or completed state (`child-navigation-parity.test.tsx`). Remaining: complete the parent card with model/effort, elapsed time and completed token count; distinguish background completion from the start result. Parent cards already show status, agent, session name and background label, and their tool results can be expanded.
- Add Mermaid image rendering in kitty and iTerm, and NSH Mermaid using a pinned host-served bundle (no CDN).
- Review both clients side by side in light and dark, establish large-session performance budgets and test keyboard-only paths. Decide focus-ring colour.

### Browser tests

Fix the NSH mock event stream ending and forcing the app into reconnecting state; then rerun the full browser suite. The recorded run had 13 failures, including `handoff.test.tsx`, `App.test.tsx`, `i6.test.tsx` and `i8.test.tsx`; the record is inconsistent about the clean baseline, so do not assume all are unrelated. `nsh test --target web` needs an explicit Playwright Chromium path on this machine. Add steer numbering, new-session skills/MCP rows, usage modal and the other browser coverage listed above.

### Reliability and safety gaps

- ~~Run Monty kernels in a scrubbed helper process before release.~~ Done: workers start through a launcher with an empty environment and working directory (`kernels.write_launcher`).
- Monty follow-ups: an OS-level sandbox if wanted; checkpoint recovery, Linux (`/usr/bin/env` path), forced worker crashes and cold-start latency.
- Decide how to supervise and report background bash jobs across daemon crashes; do not kill a process based only on a stored PID.
- Consider durable pending directory moves and worktree-in-use checks for external processes; archive transitions currently appear only in the root log.
- Decide whether archive needs bulk/project operations or timestamps; it must not implicitly delete worktrees.
- Address export's long-lived SQLite read snapshot for very large sessions; consider general artifacts and export clipping.
- Improve cross-provider conversion beyond text and tool calls; foreign reasoning is currently dropped. Copilot Responses encrypted reasoning replay is missing.
- Determine whether sandboxing or restricted command execution is needed to enforce read-only agents and read-before-edit beyond the file tools, including races with external writers.
- Add an API view and pruning for file observations; verify `wait: false` child sessions with a real provider.
- Revisit Responses `instructions` if the Codex backend changes; it currently accepts it although the Codex CLI sends a developer message.

## Decisions still open

- Whether the Anthropic Messages and Gemini providers also ship in 0.10.0 (the Agent SDK provider does).
- Whether `nexus update` installs updates or only reports.
- Whether a parent card can steer a child session.
- Output caps (lines vs. bytes) and whether they are configurable.
