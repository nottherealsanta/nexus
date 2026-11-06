# Section 14.0 rendering spikes (2026-11-06)

Disposable code lives in the session scratchpad (`.../scratchpad/spikes`: `a_pty.py`, `a_shell.py`, `a_alt.py`,
`rspike/` (Rust), `node/x.mjs`, `b_cdp.py`, `b_fps.py`, `b_bu2.py`); not committed. Evidence files sit beside this
report. Machine: macOS arm64, Chrome for Testing 156 (from `~/Library/Caches/ms-playwright/chromium-1248`), all network
localhost, profile in scratch, processes killed afterwards. The disk hit 100% mid-run (not from these spikes alone);
one planned item (building a throwaway NSH app, see C) was dropped for that reason and is marked "not proven".

## A. Terminal (PTY + VT parsing)

**Proven**
- PTY: `pty.fork` + `TIOCSWINSZ`/SIGWINCH, `less` and `vim -u NONE -N` under `zsh -f -i`, keys sent, 3.6 KB raw
  captured (`a_pty.out`, `a_shell.out`). Alt screen entered (`ESC[?1049h`) and left; resize 24x80 -> 30x100 worked.
- Split chunks: pyte fed 1-byte and 7-byte chunks gives an identical screen to one whole feed (`a_pty.out`).
  A 3-byte UTF-8 char (`世`) typed into vim across two PTY writes landed intact. CJK wide and accented text render.
- **pyte 0.8.2 gaps (important):**
  1. It **crashes** on vim's private SGR (`TypeError: select_graphic_rendition() got an unexpected keyword
     argument 'private'`). Needs a subclass that ignores `private=True` (`a_shell.out` traceback was the first run).
  2. It **does not implement the alternate screen** (DECSET 47/1047/1049): after `:q!` the pre-vim shell
     content is not restored (`MARKER restored by pyte: False`). A ~15 line subclass that swaps the buffer on those
     modes fixes it (`pyte_altscreen_subclass.py.txt`, `a_alt.out`: marker and `after-vim` both present).
  Also no bracketed-paste/mouse mode tracking worth relying on; scrollback only via `HistoryScreen`. pyte is MIT,
  but last released 2023 (0.8.2): low-maintenance. Fine as a Core checkpoint with the subclass; not as the only parser.
- Rust: `vt100` 0.15.2 + `tui-term` 0.2.0 on `ratatui` 0.29 (the version `nexus-tui` pins) rendered the captured vim
  alt screen into a ratatui `Buffer` (TestBackend) with 3-byte chunks; `alternate_screen()==true`, 48 coloured
  cells, wide chars correct (`rust_alt.out`; the extra blank after `世`/`界` is ratatui's continuation cell).
- xterm.js: `@xterm/headless` 6.0.0 + `@xterm/addon-serialize` 0.14.0 on the same bytes: alternate buffer, normal buffer
  restored after leave, `serialize()` gives a 1.1 KB replayable ANSI checkpoint (`xterm_headless.out`).

**Library choices**
| Layer | Pick | Licence / state | Notes |
| --- | --- | --- | --- |
| Core checkpoint | Keep a bounded raw-byte ring plus a pyte (patched) screen; or ship the ring only and let clients parse | pyte MIT, stale | Raw ring + replay avoids parser disagreement; checkpoint = `serialize()`-like ANSI or cell grid |
| Rust TUI | `vt100` (MIT; 0.18.1 on 2026-10-19, very active) | MIT | **`tui-term` 0.3.x needs ratatui 0.30; only 0.2.0 matches our pin.** Prefer vt100 0.18 + a ~50 line own cell->Buffer mapper over tui-term 0.2 + vt100 0.15 (old) |
| Alternatives | `alacritty_terminal` 0.26 (Apache-2.0, heavier, grid/scrollback/selection built in); `wezterm-term` (MIT, git-only, not on crates.io) | | Use alacritty_terminal if scrollback + selection must come free |
| NSH web / macOS | `@xterm/xterm` 6.0.0 (MIT) in a WebView, `@xterm/headless` for tests | MIT, active | See C: needs a WebView host |
| PTY (Rust, if ever client-side) | `portable-pty` 0.9 (MIT) | | Core owns PTYs; not needed in clients |

## B. Browser

**Proven** (`b_cdp.out`, `b_fps.out`, `b_bu.out`, `fixture.png`)
- Headless Chrome 156, fresh `--user-data-dir`, `--remote-debugging-port=9333` on 127.0.0.1, fixture served by
  `python -m http.server` on 127.0.0.1:8765 (form, 2500px scroll, link, button).
- CDP via Playwright `connect_over_cdp` + raw CDP session. `Page.startScreencast` (JPEG q60, 1024x768) frames acked.
  Input via `Input.dispatchMouseEvent`/`dispatchKeyEvent` only: wheel dy=600 -> `scrollY=600`; click + type
  `Nexus é世` -> input value correct; click button -> `#out='hello Nexus é世'`; click link -> URL `page2.html`.
- Frame behaviour: screencast is damage-driven. Static page: 1 frame in 2 s (idle is free). Interaction bursts: 2-6
  frames per action. Sustained animation: 59 fps at 1024w q60 = 313 KiB/s (5.4 KB/frame); 640w q40 = 60 fps,
  150 KiB/s; `everyNthFrame=2` -> 30 fps, 75 KiB/s (mostly-flat test page: real pages will be 5-20x larger).
  Frame metadata has `scrollOffsetX/Y, pageScaleFactor, deviceWidth/Height, timestamp`: enough for a frame revision /
  stale-click guard. `captureScreenshot` jpeg q40 11 KB, q70 13 KB, png 20 KB.
- **browser-use MCP 0.13.14 can attach to an existing CDP endpoint.** Not by flag: the MCP server reads the default
  profile from `$BROWSER_USE_CONFIG_DIR/config.json` (`browser_profile.<id>.cdp_url`). The file must contain `llm`
  and `agent` keys and every profile needs an `id`, or browser-use silently rewrites it to defaults (it did on my first
  try). With `cdp_url` set it listed all 16 tools and all tabs of our Chromium (stable 4-char ids = last 4 of CDP
  targetId). **It does not target our tab by default**: its current tab was `about:blank`; only after
  `browser_switch_tab(<id>)` did `browser_scroll` hit our page (title `y=500`, Playwright confirmed `scrollY=500`
  on the same tab). Side effect: it retitled an unrelated blank tab "Starting agent ...".
  Consequence: the adapter must call `browser_switch_tab` before each automation burst (4-char suffixes can collide),
  and Core must own the browser, writing the per-workspace config.json. Not exercised: LLM-backed tools.

**Showing frames**
- TUI: `nexus-tui` already depends on `ratatui-image` 8.1.1 and probes the protocol at startup
  (`main.rs:522`, `images.rs`), so Kitty graphics on Ghostty is the existing path. Kitty needs PNG (f=100) or raw RGB,
  not JPEG: decode CDP JPEG and re-encode (png/image crates are already deps), or ask CDP for PNG (~2-4x bigger).
  Not tested inside Ghostty (agent cannot drive it); cost of decode+PNG+kitty transmit per frame on real pages is
  **not measured**. Cap the stream at 10-15 fps with idle suspension.
- Non-graphics fallback: halfblocks of a 1024x768 page in ~120x40 cells is unreadable. A usable fallback is a text
  view (browser-use `browser_get_state` DOM text + element list, or CDP accessibility tree) with keyboard "focus
  element / activate / type" actions. That loses fidelity: **needs user approval and a ledger row** (section 14.0).
- NSH web: `<Image src="data:...">` renders an `<img>`; `Canvas.drawImage` also works (web.md). NSH macOS: native
  `Image` (CoreGraphics) via data: URI; every distinct data URI is decoded and cached in the image budget
  (`images.md`), so 10+ fps of data URIs churns memory: use a stable `blob:`/HTTP frame URL or a Canvas and test it.

## C. NSH renderer findings (sources: `~/.nsh/source/docs/dev`, NSH 0.4.0 (3789a45), `nexus-nsh/*`)

NSH is a retained-view framework, not a DOM app: TSX -> engine -> per-platform renderer. Targets used here:
**web** (HTML/CSS, `nsh package --target web`), **macOS** (AppKit, CoreAnimation, CoreText), **iOS**, and **tui**
(ratatui cell grid). Both web and macOS exist for Nexus (`nexus-nsh/package.json`: `build`, `macos`, `ios`, `tui`).
- Arbitrary DOM or xterm.js cannot be placed in TSX. The primitives are `View/Text/Input/Image/Canvas/Shader/Video/
  WebView` (`elements.md`) plus Rust `nativeComponent` (`embedding.md`, `components.md`: native apps register Swift/
  Java/Rust views; "the web has no registered components and ignores them"). `nexus-nsh/nsh.json` already has
  `"rust": "native"` and `native/src/lib.rs` (used for mic capture and Mermaid), so a Rust crate exists.
- `WebView` (elements.md:21, web.md:751): web = sandboxed `<iframe>` (srcdoc without allow-same-origin, or a URL),
  macOS/iOS = WKWebView; props `html`/`src`, `onMessage`/`postMessage`, `injectJavaScript`, `onDownload`, and a manifest
  `webview` capability with host allow-list and trusted `input`/`sendInput` (sandbox.md:38). So **xterm.js can be hosted
  in a WebView on web and macOS** with a message bridge (bytes from Core -> webview; keystrokes back). Same for a
  Core-hosted page URL. In the **tui target WebView/Image/Shader/Video are left out** (tui.md:1111: blank box;
  Canvas is braille dots, strokes 1 dot wide). The NSH TUI backend therefore needs the grid as cells.
- Cell-grid alternative without WebView (works in all targets, incl. tui): render rows as `Text` runs in Monaspace
  Argon (the Nexus font) with per-run colour/attrs; or `Canvas.fillText` per cell on web/macOS (canvas.md:
  `fillText/measureText/drawImage`; text in tui canvases lands on whole cells). Needs a parsed screen from Core or a
  JS/Rust parser in NSH (`@xterm/headless` runs in the NSH JS engine only if it has no browser-only globals; **not
  tested**; `nexus-nsh/native` could instead host `vt100`). Wide chars, cursor and selection are our job.
- Input is the real gap: NSH keyboard is a match-and-consume `keymap` table (elements.md:279) plus `Input.onKeyPress`
  (text, Backspace, Enter). There is no documented raw key stream for arbitrary Ctrl/Alt/Esc/arrow sequences. In a
  WebView xterm.js handles keys itself; for the grid renderer a native component or a full keymap enumeration is needed.
  Also the tui target maps command keys to Ctrl (tui.md:26-36), which collides with terminal Ctrl-chords.
- Not proven: I tried a throwaway NSH app (grid Text rows + Canvas.fillText + data-URI Image, `nsh package --target
  web`); `nsh check` flagged my `mount(<App/>)` (must be `mount(App)`), the web package build was still compiling
  when disk space ran out, so I stopped it. Treat all NSH claims above as documentation-derived.

## Recommendations (layer by layer)
1. Core PTY: own the PTY (`pty`/`os.openpty`, process group, SIGWINCH). Keep a bounded raw ring; checkpoint = pyte
   (patched for private SGR + 1049) or just ring replay. Test with the split-byte and vim cases above.
2. TUI terminal: `vt100` 0.18 + own Buffer mapper (avoid tui-term pin conflict), alacritty_terminal if scrollback/
   selection effort matters.
3. NSH terminal: xterm.js in `WebView` on web + macOS (+iOS if mobile approved); cell-grid `Text` renderer for the NSH
   tui target; decide raw-key plan first.
4. Browser: Core-owned Chromium, CDP screencast, one stable target id; automation via browser-use with
   `cdp_url` config plus `browser_switch_tab` guard; frame stream capped ~10-15 fps, idle suspended.
5. TUI browser: ratatui-image/Kitty on Ghostty; text/AX view fallback elsewhere.
6. NSH browser: `Image` fed by frames on web/macOS; NSH tui target gets the TUI fallback behaviour. Do not use a
   WebView pointed at the site as "the browser": it is a second page, breaking same-page identity.

## Risks
- pyte staleness and bugs; parser divergence between Core (pyte), TUI (vt100), NSH (xterm.js) on edge sequences.
- NSH raw keyboard capture and Ctrl-key remapping in its tui target; macOS WKWebView focus vs NSH keymap.
- NSH image budget with streaming frames; per-frame Kitty PNG re-encode cost (not measured on real pages).
- browser-use config file rewrite behaviour, 4-char tab ids, tab-0 default, unrelated tab retitling.
- Chromium packaging: ~600 MB per install under playwright cache; no download UX yet; headless_shell vs full.
- Screencast showed no throttling under unacked frames test (I always acked); backpressure policy untested.
- Remote Core: `localhost` is Core's; frame bandwidth 75-300+ KiB/s per open browser.

## Decisions the user must make
1. **Mobile**: include iOS terminal/browser (WKWebView xterm.js is possible; iOS has no PTY locally but Core does) or defer?
2. **TUI/NSH-tui fallback**: approve a text/AX browser view (lower fidelity) for non-graphics terminals, and for the
   NSH tui target which cannot show images/WebView? A ledger row is required.
3. **Access model**: who may see/type in a terminal or browser (single controller, viewers), whether the agent may type
   raw keys, and whether browser-use shares the user-visible page (needs switch-tab ownership) or a separate one.
4. **Terminal parser strategy**: Core-parsed cell grid sent to clients (consistent, more bandwidth/Core code) vs raw
   bytes parsed per client (xterm.js/vt100/NSH grid).
5. **Chromium distribution**: auto-download vs user-installed vs bundled; profile lifetime and clearing.
6. **NSH key capture**: accept an upstream NSH/native-component change for raw keys, or limit NSH tui to a subset.
