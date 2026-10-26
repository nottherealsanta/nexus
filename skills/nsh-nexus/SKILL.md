---
name: nsh-nexus
description: Build the Nexus desktop and web client on Native Shell (NSH, nsh.dev) — one Rust app crate rendered natively on macOS and as wasm in the browser with the same layout. Use for rust/app or rust/client-core work, NSH API questions, NSH layout/parity checks, or NSH build and packaging problems.
---

# Nexus on Native Shell (NSH)

The plan is [plans/planning/nsh-cross-platform-clients.md](../../plans/planning/nsh-cross-platform-clients.md).
Read §4 (architecture), §5A (desktop + web track: layout spec, allowed
platform differences, milestones) before any change. This skill does not
copy NSH docs: it says **which page to open** and what Nexus adds on top.
Every page is Markdown at its URL (`.md`); the index is
<https://nsh.dev/llms.txt>. Fetch the page when you need it; do not answer
NSH API questions from memory or from React/RN/GPUI analogy.

## Nexus rules (these override NSH defaults)

1. **Host contract only.** The app speaks the existing `/v1/web/*` (loopback,
   ticket → cookie + CSRF) and gateway (bearer token) protocols: snapshot, then
   SSE `ops`. No session files, no new host commands without updating
   `nexus/host/protocol.py`, `facade.py` and `docs/host.md` (AGENTS rule 4).
2. **Two crates.** `rust/client-core` has no NSH dependency (models, ops
   applier, SSE decoder, API, `present/` ports) and is tested with plain
   `cargo test`. `rust/app` is the NSH app; it holds UI, signals and transport.
3. **Same layout on desktop and web.** One root (`"root": "App"`); layout
   depends on window width, never on the platform. The web player uses
   `idiom: "desktop"`. `#[cfg(...)]` / `platform.select` only in
   `rust/app/src/transport/` and `rust/app/src/platform/`. The allowed
   differences are the table in plan §5A — anything else is a bug.
4. **Show everything.** Tool calls, parameters, results, context and errors
   render as labelled rows from the `present/` port of `ui_support/`; clipping
   is announced; golden fixtures from Python must match.
5. **Bounded and untrusted.** Host text is untrusted; cap SSE line/event sizes,
   queues and caches. Never retry a write automatically. Compare session and
   revision ids before applying late results.
6. **Removed/deprecated areas.** The GPUI desktop client (`rust/desktop/`) was
   removed; do not recreate it. The old `nexus/ui/web/` was removed (2026-10-26); `nexus web` serves this client.

## Where to read in the NSH docs

| Task | Page |
| --- | --- |
| Install CLI, wasm target, macOS prerequisites | [installation](https://nsh.dev/docs/installation.md) |
| `nsh run/dev/test/check/package` flags, env vars | [cli](https://nsh.dev/docs/cli.md) |
| Rust app structure, `#[component]`, `spawn`, `nsh.json`, theme | [rust](https://nsh.dev/docs/rust.md), [project structure](https://nsh.dev/docs/project-structure.md) |
| Signals, `computed`, `effect`, `resource`, `batch`, context, cleanup | [reactivity](https://nsh.dev/docs/reactivity.md) |
| **Rules and common mistakes (read before writing UI code)** | [coding guides](https://nsh.dev/docs/coding-guides.md) |
| Flex/grid, sizing, responsive by window size | [layout](https://nsh.dev/docs/layout.md), [tailwind](https://nsh.dev/docs/tailwind.md) |
| Typed style methods, colors, dark mode | [styling](https://nsh.dev/docs/styling.md), [style reference](https://nsh.dev/modules/style-reference.md) |
| Design tokens and visual guidance | [design guides](https://nsh.dev/docs/design-guides.md) |
| Text, fonts (Monaspace Argon / sans assets) | [text](https://nsh.dev/docs/text.md), [fonts](https://nsh.dev/docs/fonts.md), [images and assets](https://nsh.dev/docs/images-and-assets.md) |
| Timeline (`virtual_list`, `follow_end`, keys) | [lists](https://nsh.dev/docs/lists.md) |
| Scroll ownership, nested scroll | [scrolling](https://nsh.dev/docs/scrolling.md) |
| Composer, focus, IME, keyboard | [forms and keyboard](https://nsh.dev/docs/forms-and-keyboard.md) |
| Shortcuts (`on_key`, `mod+k`, `When::NotEditing`, browser-reserved keys) | [nsh/keymap](https://nsh.dev/modules/nsh-keymap.md) |
| Sidebars, sheets, dialogs, menus, tabs, DiffView, Editor | [elements](https://nsh.dev/elements.md) (open the element's own page) |
| Title bar, app menus | [nsh/desktop](https://nsh.dev/modules/nsh-desktop.md), [macos](https://nsh.dev/docs/macos.md) |
| `fetch`, streaming, WebSocket, network capabilities | [networking](https://nsh.dev/docs/networking.md) |
| Web build, `createPlayer`, CSP, CORS/cookies, web limits | [web](https://nsh.dev/docs/web.md) |
| Per-platform props and `cfg` | [platform-specific](https://nsh.dev/docs/platform-specific.md) |
| Native modules (Keychain etc.) | [native components](https://nsh.dev/docs/native-components.md) |
| Capabilities in `nsh.json`, VM limits | [sandbox and capabilities](https://nsh.dev/docs/sandbox-and-capabilities.md) |
| `nsh_testing::Headless`, `StubNetwork`, manual clock, traces | [testing](https://nsh.dev/docs/testing-and-trace.md) |
| Dev server, hot reload, debugging | [dev server](https://nsh.dev/docs/dev-server-and-debugging.md) |
| Packaging `.app` and static web site | [packaging](https://nsh.dev/docs/packaging.md) |
| Performance, traces, measuring | [performance](https://nsh.dev/docs/performance.md) |
| Engine/main thread model | [architecture](https://nsh.dev/docs/architecture.md) |
| API signatures | [modules](https://nsh.dev/modules.md), generated `.nsh/types`, and the pinned `longbridge/nsh` source |

## Traps that matter for Nexus (details on the linked page)

- **Rust `fetch` returns the whole body** — it cannot read SSE. Live `ops`
  use the transport in plan §4.1 (wasm: `web-sys` streams; native:
  `background` thread). `background`/`blocking` do not exist on the web.
  → [networking](https://nsh.dev/docs/networking.md), [rust](https://nsh.dev/docs/rust.md)
- **Components run once.** `.get()` in a body reads once; read inside a
  closure binding. No effects that copy signal → signal; use `computed`.
  Batch multi-field ops updates with `batch`. → [reactivity](https://nsh.dev/docs/reactivity.md)
- **Timeline cells are recycled.** No signals inside rows; per-block state
  (expanded, selected) lives in data keyed by block id; read `at.index()` in
  bindings; stable `.keys(..)`; `follow_end` for "follow latest".
  → [lists](https://nsh.dev/docs/lists.md)
- **Main thread never waits.** Decisions like close-on-escape are props, not
  callbacks returning bool. → [coding guides](https://nsh.dev/docs/coding-guides.md)
- **Theme tokens only.** No raw hex or arbitrary `px` in app code; add roles
  to `theme.json`. → [coding guides](https://nsh.dev/docs/coding-guides.md#styling-and-theme)
- **Web differences:** cookies only to the page origin (serve the bundle from
  the host's origin), mouse drag does not scroll, native components render
  empty, `connect-src` CSP from capabilities, browser-reserved shortcuts never
  arrive. → [web](https://nsh.dev/docs/web.md)
- **No frame query API in Rust tests.** Check layout through trace `Frame`
  items (`app.observe` + `print_batch`). → [testing](https://nsh.dev/docs/testing-and-trace.md)

## Workflow

1. Read the plan section, then the NSH page(s) for the task. Check the pinned
   NSH rev in `rust/app/Cargo.toml` and read that source for signatures.
2. Put protocol, parsing and presentation logic in `client-core` with tests
   first; keep `rust/app` bodies declarative.
3. State the owners (state, layout, scroll, focus) and the test that would
   fail, per the NSH agent rules in [coding guides](https://nsh.dev/docs/coding-guides.md#rules-for-coding-agents).
4. Test: `cargo test` in both crates (headless with `StubNetwork` and
   `Clock::manual`), `nsh check`, then run both targets:
   `nsh run --target macos` and `nsh run --target web` at 1440×900,
   1100×800 and 800×600 (`--window-size`).
5. Parity: screenshot web with Playwright and macOS with the
   [native-app-review](../native-app-review/SKILL.md) skill on the same fixture;
   save to `artifacts/nsh/`; differences outside plan §5A's table are bugs.
6. Update `docs/` (surfaces, security, module map, decisions) and the plan's
   milestone status. Report what was verified and what was not.
