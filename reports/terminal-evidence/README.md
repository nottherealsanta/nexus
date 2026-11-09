# Terminal evidence and current dock screenshots

Actual production TUI/NSH components, dark theme and matching selected text
(`selected output`, terminal `t1` / `Build`, workspace `/tmp`, unknown sequence).
No product UI was mocked. TUI uses the ordinary seeded-session test renderer;
NSH uses its actual Composer and confirmation modal in a new-session fixture.
Backgrounds therefore differ; the evidence payload and actions are the same.

| State | TUI | NSH |
| --- | --- | --- |
| Exact-payload preview | `tui-preview.png` | `nsh-preview.png` |
| Short/narrow preview, confirmation reachable | `tui-preview-narrow.png` (80×24) | `nsh-preview-narrow.png` (640×480) |
| Confirmed draft, not sent | `tui-draft.png` | `nsh-draft.png` |
| Shared terminal-dock fixture, including Attach action | `tui-dock.png` | `nsh-dock.png` |

Read and compared preview, narrow preview, draft and toolbar images. Both show
provenance, the same untrusted-data warning, byte counts, JSON string, Cancel and
Add to draft. TUI's selected confirmation action and NSH's primary action express
the same operation. At short heights NSH caps the scroll body to remaining modal
space so confirmation cannot be clipped; TUI keeps buttons below its scroll body.
Evidence appends to the existing draft in both clients, never replacing selected
text or adding a blank separator to an empty draft.

`nsh-real-core-preview.png` is an additional real-Core/browser capture in light
mode: a running PTY produced 500 lines and `NEXUS_TERMINAL_SMOKE`; the actual
xterm selection was sent through the WebView capture bridge, previewed, cancelled,
then captured again and added to the draft. The smoke asserts the draft contains
the marker/warning and that no prompt/input message was sent automatically. It
also verifies Ctrl+C, rename, acquisition/release, confirmed Close and mobile
exclusion. This is live PTY evidence, not merely a fixture illustration.

Reproduction:

- TUI: `NEXUS_TERMINAL_EVIDENCE_SHOTS=../reports/terminal-evidence cargo test --test terminal_evidence`.
  SVGs are rendered through Chromium with zero body margin, using their intrinsic
  8.4×18 pixel cell geometry.
- NSH: `node scripts/nsh-web.mjs test terminal-evidence-ui.test.tsx --target web --browser <cached Chromium>`.
- Dock fixture: TUI `terminal_dock_matches_the_shared_scenario` with
  `NEXUS_TERMINAL_DOCK_SHOTS`; NSH `terminal-panel.test.tsx`.
- Live smoke: `NEXUS_NSH_URL=<scratch host> NEXUS_TERMINAL_SESSION=<scratch session>
  NEXUS_TERMINAL_EVIDENCE_SCREENSHOT=<path> node e2e/terminal-core-smoke.mjs`.

See [the implementation contract](../terminal-evidence-design-2026-11-08.md) and
[the terminal work report](../terminal-work-2026-11-08.md) for checks and remaining
platform/native/remote release gates. Existing SVG-vs-screen icon styling and
composer send/voice buttons are standing approved presentation differences; no
new client-specific terminal behavior was introduced.
