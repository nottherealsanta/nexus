# ZMX `Needs attention` detail — TUI / NSH parity (2026-11-23)

## Result

The ACP denial scenario (`interface/scenarios/zmx-acp-denial.json`) exposed a real
difference in the ZMX context card: at 160x50 the TUI put the worker's `error` on
the `Needs attention` header as a one-line meta and clipped it
(`...the CLI may ha…`), while NSH showed the full error. Both clients now render
the attention detail as the section's **full wrapped text under its title**, with
no one-line meta and no row cap. This is the same product rule in both clients;
no client-specific difference and no user decision were needed.

Structure in both:

```
Needs attention
Antigravity's permission request was declined; the CLI may have continued
without the tool. Nexus approved nothing.
```

The TUI's other context rows (`Run`, `Settings`, `Not applied`, `Denied through
Nexus`) keep their existing one-line meta; only the attention detail is a note.
The card gains the note's rows; no fixed-height layout constrains it.

## Changed

| Client | Files |
| --- | --- |
| TUI | `nexus-tui/src/paint.rs`: `CtxBody::Note`; `note_lines` wraps the full text at the context width; empty section meta is no longer emitted. `nexus-tui/src/timeline.rs`: the `needs_attention` section carries `CtxBody::Note(error)` with an empty meta. `nexus-tui/tests/scenarios.rs`: removed the difference-encoding clipped test and the temporary helpers. `nexus-tui/README.md` wording |
| NSH | `nexus-nsh/src/ui/newsession.tsx`: `CtxBody` gains `{ kind: "note"; text }`; `zmxSections` puts the attention error in that body instead of the meta; `Section` renders it as a wrapped, selectable muted paragraph (`ctx-<key>-note`). `nexus-nsh/scenarios.test.tsx`: removed the NSH-only full-text test and the temporary shot test. `nexus-nsh/README.md` wording |
| Shared | `interface/scenarios/zmx-acp-denial.json`: `expect` now includes `Nexus approved nothing`, asserted by both clients. The `absent` entry (`Denied by the CLI`) is unchanged |

No generated files were regenerated. No runtime/Core file was touched.

## Tests

| Command | Result |
| --- | --- |
| `cd nexus-tui && cargo test --lib ctx_tests` | 9 passed, including `an_attention_note_wraps_fully_under_its_title` (the wrapped note text equals the full error and every row fits the card width) |
| `cd nexus-tui && cargo test --test scenarios` | 11 passed |
| `cd nexus-tui && cargo test --test timeline a_zmx_child_card` | 1 passed |
| `cd nexus-nsh && node scripts/nsh-web.mjs test --target web scenarios.test.tsx` | 6 passed, 5 failed only with the known `open("./App.tsx"): the browser is gone: Connection reset by peer` (activity group, subagent done notice, zmx denied action, zmx needs attention, zmx recovered); the ACP denial test passed |
| `... scenarios.test.tsx --filter "zmx ACP denial"` | 1 passed |
| `... scenarios.test.tsx --filter "zmx denied action"` | 1 passed |
| `... scenarios.test.tsx --filter "zmx needs attention"` | 1 passed |
| `... scenarios.test.tsx --filter "zmx recovered"` | 1 passed |

The difference-encoding tests are gone: TUI
`zmx_acp_denial_attention_text_is_clipped_at_the_standard_tui_width` and NSH
`zmx ACP denial shows the full Nexus outcome in NSH` no longer exist; both clients
run the shared `zmx ACP denial says Nexus denied it` scenario with the shared
`expect` list.

## Screenshots

| | TUI | NSH |
| --- | --- | --- |
| After | `tui-zmx-acp-denial-160x50-dark.png` / `.svg` (160x50 cells, dark) | `nsh-zmx-acp-denial-1280x900-light.png` (1280x900) and `nsh-zmx-acp-denial-1024x768-light.png` (1024x768), light |
| Before | the test frame read `Needs attention  …the CLI may ha…` | `nsh-zmx-acp-denial-before-1280x900-light.png` (full one-line error) |

All after images were read. The TUI shows the full error on two wrapped rows
under the title; NSH shows the same title and the full error beneath it (one
line at 1280, two wrapped lines at 1024, as the TUI's narrower card does). The
capture media differ (TUI test frame rendered to SVG/PNG; NSH headless scenario
screenshot), as do wrap points and window sizes; the content, order and
structure match. The TUI frame comes from the same shared scenario loaded by
`tests/scenarios.rs`, not from a live Core.

The NSH before/after pair shows the intentional structure change: the error
moves from the clipped one-line meta beside `Needs attention` to a full
paragraph under it. The text was already visible at 1280 width before; the
change is what makes the same detail survive narrower widths and match the TUI.

## Open items

- `docs/external-workers.md` and `docs/porting-ledger.md` still say the denial
  card's `Needs attention` detail "is not yet at parity / fix in progress".
  Those lines are part of the concurrent ACP work and were left untouched here;
  they should be updated to say the detail wraps in both clients.
- The full NSH scenario file keeps failing intermittently with the harness
  `browser is gone` reset; the affected ZMX tests pass when run individually,
  and the same flake predates this change.
