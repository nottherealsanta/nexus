# One Rust client for desktop, web and mobile on Native Shell (NSH)

Status: **in progress (2026-10-26).** NSH replaces both the old browser client
and the GPUI desktop client, which are removed. `rust/app` is the one client for
web and macOS; `nexus web` opens it. Current state and next steps are in
[Progress](#progress-2026-10-26) below; user-facing docs are
[docs/nsh-client.md](../../docs/nsh-client.md). Sections 1–8 are the original
research proposal; where they disagree with Progress, Progress wins.

## Progress (2026-10-26)

Decisions made by the user:
- The old browser client (`nexus/ui/web/`) and the GPUI desktop client
  (`rust/desktop/`, `nexus/ui/desktop/`) are **removed**, not kept behind a flag.
  `nexus web` serves the NSH bundle at `/w/<key>/app/`; `/` and `/s/...` redirect there.
- Goal: **full parity with the Ratatui TUI** on web and macOS from one crate.
- **Nexus core stays separate from the clients.** Clients use only the
  `/v1/web/*` HTTP API. `nexus/host/nsh_static.py` is the only Python module
  that knows about the bundle; `tests/test_client_separation.py` enforces this
  and that `rust/app` never reads Nexus files or the database directly.
- Verification: web with Playwright only. macOS shares the same root and must
  behave the same; it is compile-checked, not run.

Done (verified on web with Playwright unless noted):
- NSH 0.2.0 toolchain; `scripts/nsh-app.sh package|run web|macos`.
- Launch: `nexus web` prints/opens `/app/?ticket=…`; the daemon redeems the
  one-use ticket, sets the cookie and 303-redirects (NSH 0.2.0 apps cannot read
  the URL fragment and have no `web.host` page hook). Route-scoped CSP with
  inline-script hashes and `'wasm-unsafe-eval'`.
- Live updates: Rust `fetch` cannot stream, so the client long-polls
  `GET /v1/web/session-poll` (client-neutral route; per-client feed in
  `nexus/host/web_poll.py`, `from_seq` acks, bounded buffer, resync on
  overflow). SSE routes are unchanged for other clients.
- `rust/app` foundation (WP1): `api.rs`, `platform/` (only place for `cfg`),
  `ops.rs` patch applier with captured-frame fixtures, `store.rs` live loop with
  backoff and stale-result guards, `present/`, `components/`.
- UI: sessions sidebar, new session, timeline (messages, labelled tool rows,
  turn errors, announced clipping), composer send.
- macOS: native launch via `NEXUS_URL`/`NEXUS_TICKET`, manual cookie + CSRF.
  **Not verified.**
- Packaging: wheel includes `nexus/ui/nsh/`; `native-wheels.yml` builds the
  bundle. **Not verified in CI yet.**

Next: work packages WP2–WP8 from
[nsh-tui-parity-inventory.md](nsh-tui-parity-inventory.md) §4, run in parallel,
each in its own git worktree from the committed foundation, merged one at a time
with a Playwright check after each:

| WP | Scope | Status |
| --- | --- | --- |
| WP1 | Transport, store, ops applier, long-poll | done |
| WP2 | Chrome and sidebars (top bar, toasts/notifications, projects/sessions/files/git, details sidebar, breakpoints) | next |
| WP3 | Timeline presentation port (markdown, tool groups, diffs, code mode, subagents, notices, clocks, streaming) | next |
| WP4 | Composer and slash commands, keymap, queue/steer/stop, `@`/`/` completion, attachments, pickers | next |
| WP5 | Approvals and questions, session ops, worktrees, `/merge` | next |
| WP6 | Context header and inspectors (tools, skills, MCP, usage, message/agent pages) | next |
| WP7 | Settings and onboarding (all areas) | next |
| WP8 | Dictation, `/speak`, manager, dev mode; Live (WebRTC) likely deferred | after WP2–WP7 |

Known limits: `/restart`, `/reload` and daemon shutdown are not possible over
`/v1/web/*`; the view has no `slot_wait` or manager show events (poll
`SessionActivity` / `ManagerState`); local TUI state (prefs, history, drafts)
must be kept by the client itself.

## 1. What NSH is

[Native Shell](https://nsh.dev) (`longbridge/nsh`, Apache-2.0, ~700 stars,
CLI around 0.1.x, no tagged releases seen) is a Rust-first UI framework. One app
model and one toolchain (`nsh` CLI) target:

| Target | Backend | Minimum |
| --- | --- | --- |
| iOS / Mac Catalyst | UIKit + CoreText | iOS 15 |
| Android | Android Views (`libnsh.so`, `nsh.aar`) | API 28 |
| macOS | AppKit | 14 |
| Windows | Win32 + Direct2D/DirectWrite | 10 1809 |
| Linux | GTK 4 + Pango | GTK 4.14 |
| GPUI | `AppView` entity inside a GPUI / GPUI Kit window | Metal / DX / Vulkan |
| Web | Rust engine in `nsh.wasm`, DOM ops applied by `nsh.js` | modern browsers |

Key facts for Nexus:

- **Pure-Rust apps are first class.** `#[component]` functions, signals
  (`signal`, `get/set/update`), typed style methods, Taffy flex/grid layout,
  Tailwind-style classes. A Rust app ships no JS VM on any target, including the
  web (`nsh.wasm` ≈ 3.3 MB, ≈ 0.75–1.06 MB compressed for the full gallery).
- **One engine thread per app**; the main thread applies one mount batch per
  commit, so a busy engine delays updates but never freezes scrolling.
- **`nsh/base`** gives unstyled behavior components: Menu, ContextMenu, Dialog,
  AlertDialog, Popover, Sheet, Tabs, NavStack, Resizable panels, Dock, Toast,
  Tooltip, List (virtualized), Table/DataTable, **DiffView**, **Editor**,
  TextView, SelectionArea, KeyboardAvoidingView, SafeAreaView. We supply the look.
- **Async:** `spawn(async { .. })` updates signals; `fetch` works everywhere
  (NSURLSession / HttpURLConnection / browser). `background` / `blocking` run on
  native threads but **are not available on the web.**
- **Rust `fetch` returns the whole body only.** Streaming (`ReadableStream`,
  incremental `text/event-stream`) and `WebSocket` are documented for **TSX**
  only. On the web, Rust reaches browser APIs through `wasm-bindgen`/`web-sys`.
  → This is the single biggest technical risk for Nexus (live SSE `ops`).
- **Embedding:** `AppView` (macOS/Windows/Linux/GPUI), `NshAppView` in
  `NativeShell.xcframework` (iOS), `NshView` in `nsh.aar` (Android),
  `createPlayer` (web). Hosts talk to the app over a host channel
  (`set_params`, `send`, `handle` → `call.resolve/reject`).
- **Sandbox/capabilities** in `nsh.json` (`network.hosts`, `network.http`), and
  Ed25519-signed `.nshpkg` modules for runtime TSX delivery (not needed at first).
- **Testing:** `nsh_testing::Headless` runs the real runtime under `cargo test`
  (`wait_for("text")`, `press("label")`).
- **Docs for agents:** `https://nsh.dev/llms.txt`, every page as `.md`.

Stated weaknesses (vendor-authored): ~3× GPUI Fast CPU under dense updates
(20k quotes/s); small ecosystem; per-platform look differs by design; GPUI
backend does not draw blur/backdrop filters, rounds only backgrounds when
clipping, video only on Linux; web: native components render empty, mouse drag
does not scroll, partial UAX #14 line breaking, no hyphenation.

## 2. Where Nexus is today

| Surface | Code | Talks to | Status |
| --- | --- | --- | --- |
| Ratatui TUI | `rust/tui`, `rust/widgets`, `nexus/ui/ratatui/` | daemon via Python bridge | primary |
| GPUI desktop | `rust/desktop` (gpui `=0.2.2`) | Python bridge, private JSONL wire schema 3 (`nexus/ui/desktop/wire.py`) | **removed** (2026-10-26) |
| Browser | `nexus/ui/web/`, `nexus/host/web.py` (`/v1/web/*`, ticket → cookie + CSRF, SSE) | daemon HTTP/SSE | **removed** (2026-10-26), replaced by NSH |
| iOS | `ios/` SwiftUI + `NexusKit` (`GatewayClient`, `SSEDecoder`, `ViewStore`) | opt-in gateway (`/v1/pair`, `/v1/me`, `/v1/hosts/...`), bearer token, snapshot + SSE `ops` | runtime unverified |
| Android | — | — | none |

Important property: the gateway **forwards the host's own snapshot, events and
command protocol unchanged**; only auth differs from `/v1/web/*`. So a single
client core can speak both: cookie/CSRF on loopback, bearer token via gateway.

## 3. Goal and non-goals

Goal: one Rust codebase (`rust/app`) built with NSH that becomes
- the **mobile** client (Android new; iOS as a parity candidate to the SwiftUI app),
- the **web** client (the successor to the deprecated `nexus/ui/web/`),
- a **desktop** client (native AppKit/Win32/GTK, or embedded in GPUI later),

all speaking the existing host contract (AGENTS rule 4). No new public API
except what Phase 1 proves is missing.

**Order (decided 2026-10-26): desktop and web first, with the same layout.**
Mobile (Phase 4) waits until desktop + web reach M5 in §5A. §5A is the working
spec for that track.

Non-goals for this plan: touching `rust/desktop/` (removed; recreating it needs explicit
approval), adding features to `nexus/ui/web/`, runtime-delivered TSX modules,
voice/live calls (phase 6+), changing core/manager layers.

**Decisions the user must make before Phase 2** (recommendations in bold):
1. Is NSH-web allowed to replace `nexus web`? **Yes, behind `nexus web --nsh`
   until parity, then swap.** (Working on NSH web is approved; the swap is not.)
2. iOS: replace SwiftUI or keep both? **Keep SwiftUI until the NSH iOS build
   matches its feature list; do not maintain both long-term.**
3. Desktop: NSH native backends or GPUI embedding? **Native AppKit first** (no
   GPUI version coupling with the removed client); revisit GPUI only if desktop
   work resumes.
4. Presentation helpers (`ui_support/tool_details.py`, `context.py`,
   `timeline.py`, `prompts.py`): port to Rust with golden parity tests (as the
   web does in JS) **or** add a host command returning the projected rows.
   **Port to Rust with Python-generated golden fixtures** — keeps the host
   contract unchanged and lets the client render offline replays.

## 4. Architecture

```
rust/
  client-core/        pure Rust, no NSH dependency, no I/O runtime
    model.rs          serde types for snapshot, ops frames, commands, results
    ops.rs            snapshot + ops applier (Rust port of ViewStore.swift logic)
    sse.rs            incremental SSE decoder (bounded line/event sizes)
    api.rs            request builders + response parsing for /v1/web/* and gateway
    auth.rs           AuthMode::{LoopbackTicket{cookie,csrf}, Gateway{token}}
    present/          ports of tool_details / context / timeline / prompts
    tests/            golden fixtures generated from Python
  app/                NSH app crate (nsh.json, "root": "App")
    transport/        Transport trait + per-target impls (see 4.1)
    store.rs          signals fed by client-core (sessions, view, drafts)
    screens/          Pair, Workspaces, Sessions, Conversation, Details, Settings
    components/       Message, ToolCall, ContextHeader, ApprovalCard, Composer,
                      Markdown (pulldown-cmark → Text spans), Diff (nsh DiffView)
    theme.json        shadcn-style cssVars; mirrors design.md tokens
    platform/         secure storage, open-url, notifications (nativeModule)
```

`client-core` is testable with plain `cargo test` and reusable by the GPUI
desktop if it ever resumes (it could replace part of the Python bridge's job).

### 4.1 Live events (the risk)

Need: a long-lived `text/event-stream` read incrementally on every target.

| Target | Plan A | Fallback |
| --- | --- | --- |
| Web (wasm) | `web-sys` `fetch` + `ReadableStream` reader (or `EventSource` when cookies suffice — same-origin) pushing into signals via `spawn_local`-style task | TSX shim module doing `fetch` streaming, calling into Rust via `#[module]` |
| macOS/Win/Linux/iOS/Android | `background` thread with a small blocking HTTP client (`ureq` + rustls, or platform stack via `nativeModule`) feeding a channel drained by an engine `spawn` task | Same TSX shim (TSX `fetch` streams on native too), or host-channel `send` from a Swift/Kotlin host |
| Any | — | Add a WebSocket mirror of the SSE stream on host/gateway (contract change → last resort) |

Phase 0 must prove: (a) a value produced on a `background` thread reaches a
signal without blocking the engine, (b) wasm streaming works with
`wasm32-unknown-unknown` + NSH's glue, (c) iOS/Android backgrounding closes the
stream and foreground resnapshots (same rules as the iOS client).

### 4.2 Auth and connection modes

- **Local desktop / local web:** the launcher mints a one-use web launch ticket
  exactly as `nexus web` does today; the app redeems it at
  `/v1/web/ticket/redeem`. Web: same origin, so the browser handles cookies.
  Native: store the cookie in memory, send `Cookie` + CSRF headers manually
  (NSH allows `Cookie` on native fetch). Loopback only; nothing new is exposed.
- **Remote (mobile, remote desktop):** gateway pairing (`/v1/pair`, one-use
  code, scopes `read,prompt,approve,voice,live`) → bearer token. Token goes to
  Keychain (iOS) / Keystore (Android) / OS keychain (desktop) through a
  `nativeModule`; never `nsh/storage` (not documented as secure). Refuse
  redirects and URLs with credentials/query/fragment, as `ios/` does.
- **Web served by Nexus:** the NSH web bundle (`nsh package --target web`) is
  served by `nexus/host/web.py` under `/w/<project-key>/app/` from the same
  origin, so no CORS. CSP must add `'wasm-unsafe-eval'` for that route only;
  `connect-src 'self'`. Update `docs/security.md`.

### 4.3 Presenting context (AGENTS: "show everything")

Every tool call, parameter, output, context block and error renders as labelled
rows from the `present/` port; clipping is announced. Golden tests: a Python
script (`scripts/gen_client_fixtures.py`) runs the real `ui_support` helpers over
`tests/fixtures/` sessions and writes expected rows as JSON; `client-core` tests
assert equality. CI fails on drift, so a change to `tool_details.py` forces the
Rust port to follow.

## 5. Phases

### Phase 0 — spike and go/no-go (≈1–2 weeks, throwaway branch)

Build a minimal NSH Rust app in `scratch/nsh-spike/` (not merged) that:
1. Lists sessions from a running daemon over `/v1/web/*` (macOS target).
2. Streams one session's SSE `ops` live while a turn runs (`nexus run`), on
   macOS **and** web **and** iOS simulator **and** Android emulator.
3. Renders a 2,000-block transcript in `List` with Markdown + a diff; measure
   scroll FPS and CPU during token streaming (compare with TUI).
4. Composer: multi-line `Input`/`Editor`, IME, paste, Ctrl-key shortcuts on
   desktop/web, keyboard avoidance on mobile.
5. Stores a token via a `nativeModule` in Keychain.
6. Records: NSH git rev used, toolchain friction (wasm LLVM on macOS, Xcode,
   Android NDK), binary sizes, cold-start time.

Go criteria: all of 1–5 work on ≥3 of 4 targets with Plan A or a documented
fallback; streaming stays smooth at realistic token rates; no blocker needing
NSH upstream changes we can't wait for. Write findings to `docs/decisions.md`.
No-go → keep SwiftUI iOS, consider Android via Kotlin on the same gateway, stop.

### Phase 1 — `rust/client-core`

- Serde models for snapshot, `ops` frames, commands used by the iOS client
  (`SessionOpen`, `SessionEnqueue`, stop, approvals) and session list.
- Ops applier with stale-revision rejection and resync → resnapshot.
- SSE decoder: bounded (match `MAX_FRAME_BYTES` 16 MiB / host SSE limits).
- `present/` ports + golden fixture generator + tests.
- Replay test: snapshot + recorded ops == final snapshot from Python
  (`nexus/view/reduce.py`), mirroring the "replay reproduces live state" rule.
- Add `rust/client-core` to CI (`cargo test`, `cargo clippy`).

### Phase 2 — `rust/app` shell (macOS first)

Feature list = current iOS client, which is the smallest complete remote set:
pair/connect, workspaces, session search/list, new session, conversation
(snapshot + live), composer with per-session drafts, stop (keeps queue),
one-time approve/deny, structured details for every block, follow-latest
toggle, reconnect banner. Layout follows `docs/surfaces.md`: top bar, sessions
sidebar, context header, timeline, composer, details sidebar; responsive
breakpoints (`md:`) collapse sidebars into `NavStack` pages / `Sheet`s on phone.
Headless tests with `nsh_testing` for each screen.

### Phase 3 — web target

- `nsh package --target web` in a build script; output copied into the Python
  package data (like current static assets) with content hashes.
- `nexus web --nsh` (flag) serves it; same ticket flow. Playwright check added to
  `tests/playwright_web_check.py` peer (`tests/playwright_nsh_check.py`).
- Cache headers per NSH docs (`no-cache` index, immutable hashed assets),
  `application/wasm` MIME.

### Phase 4 — mobile

- Android: `nsh package --target android` → `.apk`; gateway pairing; Keystore
  module; background closes stream. Emulator test via `nsh test` + manual device.
- iOS: `nsh package --target ios`; compare against `ios/` feature list; decide
  (per §3.2) whether to replace SwiftUI. Option: embed `NshAppView` in the
  existing SwiftUI shell to migrate screen by screen.
- Both: safe areas, keyboard avoidance, share-sheet for exports later.

### Phase 5 — desktop packaging

- `nsh package --target macos|windows|linux`; `nexus app` (or `nexus desktop
  --nsh`) launcher that mints the ticket and passes the URL as a launch param.
- Signing/notarization folded into `docs/release.md`; separate from the wheel.
- GPUI embedding (`AppView`) only if desktop GPUI work resumes and the GPUI
  version NSH pins is compatible with ours (`=0.2.2`) — check
  `nsh-platform-gpui`'s manifest; mismatch likely.

### Phase 6 — parity and retirement (each step needs user approval)

- Port remaining TUI features in priority order: questions/answers, attachments,
  slash commands (from `ui/cli/commands.py` metadata via host), settings,
  provider sign-in, subagent pages, manager screen, usage meter.
- Voice/live: needs microphone via native module; out of scope until above.
- Retire `nexus/ui/web/` once NSH web passes the existing Playwright checks.

## 5A. Desktop + web track (current focus)

Phases 0–3 and 5 above are done together for two targets only: **macOS
(AppKit backend) and web (wasm)**. Windows/Linux builds are kept compiling in CI
once M5 is reached but are not verified before then. Phase 0 runs only items
1–4 and 6 on macOS + web (Keychain item 5 moves to M5).

### Rule: one layout, two targets

Desktop and web render **the same component tree from the same Rust root**
(`rust/app`, `"root": "App"`). Layout depends on the window size, never on the
platform. The web player is created with `idiom: "desktop"`, so NSH base
components behave the same as on macOS.

Allowed platform differences (and only these, kept in `rust/app/src/platform/`):

| Area | macOS | Web |
| --- | --- | --- |
| Title bar | `set_title_bar(TitleBarStyle::Hidden, Some(px(42.0)))`; top bar has `.window_drag(true)`; left inset from `title_bar()` for traffic lights | no inset; browser chrome above |
| App menu | `nsh/desktop` menus (About, Settings, Quit, Edit menu for copy/paste) | none; same actions in the top bar `+` menu |
| Transport | §4.1 native impl | §4.1 wasm impl |
| Auth | ticket passed by launcher, cookie + CSRF sent manually | same-origin ticket redeem, browser cookie |
| Open link / file | system open via `handle_open_url` | `window.open`, `<a download>` |
| Clipboard | native | needs HTTPS/loopback; may prompt |
| History | none | `history: false` (no back-button navigation) |

Enforcement: `#[cfg(...)]` and `platform.select` are forbidden outside
`transport/` and `platform/`; a `cargo test` in `rust/app` greps the source
tree and fails otherwise.

### Layout spec (from `docs/surfaces.md` and the TUI)

```
┌ top bar 42px ─────────────────────────────────────────────────────────────┐
│ ▌ title · workspace · branch          status · context meter   +   ▐     │
├──────────────┬───────────────────────────────────────────┬────────────────┤
│ sessions     │ context header (opens every conversation) │ details        │
│ sidebar      │───────────────────────────────────────────│ sidebar        │
│ search       │ timeline (virtualized List)               │ tool call /    │
│ list         │   messages, tool calls, approvals, notices│ context block /│
│ new session  │                                           │ session info   │
│              │───────────────────────────────────────────│                │
│ 260px, 200–  │ composer rows (input, model, queue, stop) │ 360px, 280–560 │
│ 400 resizable│                                           │ resizable      │
└──────────────┴───────────────────────────────────────────┴────────────────┘
```

- Built with `ResizablePanelGroup`; panel sizes persist per viewer (native
  storage / `localStorage` via `nsh/storage`), not in the host.
- Breakpoints on window width, identical on both targets:
  - **≥ 1280:** three columns as drawn.
  - **900–1279:** details sidebar becomes a right `Sheet` opened by selecting a
    block or `Ctrl-D`; sessions sidebar stays.
  - **< 900:** sessions sidebar becomes a left `Sheet` (`Ctrl-S`); timeline
    takes the full width. Minimum supported window 640×480.
- Timeline max text width 820px, centered in its column.
- Typography: sans-serif interface text, Monaspace Argon for code, bundled as
  assets on both targets (no system-font fallback differences). Theme tokens in
  `theme.json` (light + dark), derived from `design.md`; stroke icons as SVG
  assets.
- Keyboard: one keymap (`nsh/keymap`) with the TUI's Control-key shortcuts
  and the chat commands from `ui/cli/commands.py`; `Cmd` aliases on macOS
  only where the browser does not reserve the key.

### Shared components (`rust/app/src/components/`)

TopBar, SessionsSidebar (search, grouped list, new), ContextHeader,
Timeline (virtualized `List`), MessageBlock, Markdown (pulldown-cmark → `Text`
spans, code blocks in `TextView` with copy button), ToolCallRow (heading +
summary + batch glyph from the `present/` port), ApprovalCard (four choices,
unavailable ones disabled), NoticeRow, Composer (multi-line `Editor`, Enter
send / Shift-Enter newline, drafts per session), DetailsPanel (labelled
sections: Overview, Parameters, Code, Progress, Summary, Result, Error,
Context, Metrics, Diff via `DiffView`), ContextMeter, ReconnectBanner.
Every component has a `nsh_testing` headless test fed by fixtures.

### Milestones (each ends with both targets green)

| M | Delivers | Acceptance |
| --- | --- | --- |
| M1 | Layout shell on **fixture data** (snapshot JSON bundled as an asset; no daemon) | All regions render on macOS and web at 1440×900, 1100×800, 800×600; resize and breakpoints behave identically; headless tests pass |
| M2 | Live read-only: ticket auth, session list, open session, snapshot + SSE `ops`, reconnect → resnapshot | A turn started in `nexus chat` streams into both clients; closing the client does not stop the turn |
| M3 | Write path: new session, send (`SessionEnqueue`), stop (keeps queue), one-time approve/deny, drafts | Same actions work from both; failed sends keep the draft; no automatic retries |
| M4 | Details sidebar complete via `present/` port; golden fixtures green; clipping announced | Every tool/context field visible as labelled rows, matched against Python output |
| M5 | Packaging and launchers: `nexus web --nsh`, `nexus app` (macOS `.app`), Playwright + native screenshot checks | Fresh clone builds both via one script; checks run in CI jobs separate from the Python suite |

### Layout parity check

- **Headless:** `nsh_testing` dumps the layout tree (element role, label,
  frame) at the three sizes; one snapshot file per size is shared by both
  targets because the root is the same. (Verify in Phase 0 that
  `nsh_testing` exposes frames; if not, assert region presence and order.)
- **Visual:** `tests/playwright_nsh_check.py` screenshots the web build;
  the `native-app-review` skill screenshots the macOS build at the same sizes
  with the same fixture; images go to `artifacts/nsh/` and are reviewed side
  by side. Differences outside the allowed table above are bugs.

### Launch and dev loop

- `scripts/nsh-app.sh run macos|web` and `scripts/nsh-app.sh package
  macos|web` wrap `nsh run` / `nsh package` with the pinned NSH rev.
- `--fixture PATH` (dev feature) loads a recorded snapshot instead of
  connecting, for layout work without a daemon.
- Desktop launch: `nexus app` mints the same one-use ticket as `nexus web`
  and passes base URL + ticket to the app (launch argument or env; mechanism
  verified in Phase 0). Web: `nexus web --nsh` opens
  `/w/<project-key>/app/#ticket=…`.
- The Python side adds only the launcher command and the static route; no
  host protocol change is expected for M1–M5.

## 6. Docs, tests and repo rules to honor

- New `docs/nsh-client.md`; rows in `docs/README.md`, `docs/surfaces.md` (new
  surface), `docs/module-map.md` (new Python launcher/route modules),
  `docs/security.md` (wasm CSP, token storage, ticket on native),
  `docs/release.md` (app packaging), `docs/decisions.md` (why NSH, spike result).
- Layering: Python additions limited to the launcher (`nexus/ui/...`) and static
  route in `nexus/host/web.py`; no core changes. `tests/test_ui_layering.py`
  must stay green.
- Pin NSH to a git rev in `Cargo.toml`; vendor nothing. Re-evaluate on each
  bump using `nsh-compat` notes.
- Conventional commits; separate PRs per phase.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| Rust-side streaming not supported | Phase 0 Plan A/B/C in §4.1; WebSocket mirror as last resort |
| NSH is young (0.1.x, single vendor) | Pin rev; keep `client-core` NSH-free so a UI swap costs only `rust/app` |
| CPU under dense token streaming | Batch ops per frame in the store; measure in Phase 0 |
| Markdown/code rendering quality (no built-in Markdown) | Own pulldown-cmark → spans component; use `TextView`/`Editor` for code |
| Duplicate presentation logic drifts | Golden fixtures generated from Python, CI-enforced |
| Web CSP / cookie / CORS | Same-origin serving; route-scoped CSP change reviewed in `security.md` |
| Toolchain weight (wasm LLVM, Xcode, NDK) in CI | Build app targets in separate CI jobs, not the Python test job |
| Two iOS clients | Decide in Phase 4; do not ship both |

## 8. First concrete tasks

1. Clone `longbridge/nsh`, install the CLI, run `examples/counter` on macOS,
   web, iOS sim, Android emulator; note the rev.
2. Read `nsh.dev/docs/{reactivity,lists,forms-and-keyboard,native-components,
   sandbox-and-capabilities}.md` and `nsh-platform-gpui/Cargo.toml`.
3. Spike §5 Phase 0 items 1–2 (session list + live SSE) on macOS and web.
4. Write the spike report and the go/no-go into `docs/decisions.md`; ask the
   user for the §3 decisions.

Sources: https://nsh.dev, https://nsh.dev/llms.txt, docs pages architecture,
web, networking, gpui, rust, comparison; https://github.com/longbridge/nsh.
