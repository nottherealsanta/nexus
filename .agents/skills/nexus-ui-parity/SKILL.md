---
name: nexus-ui-parity
description: Land a Nexus UI change in both the TUI and NSH, verify it with shared mock scenarios and screenshots, and get user approval for any client-specific difference. Use for any change to what either client shows or how it behaves, including layout, styling, wording, pickers, modals, sidebars, shortcuts, the timeline or the composer, even if the request names only one client or sounds like a small tweak.
---

# Nexus UI parity

The TUI (`nexus-tui`, Rust) and NSH (`nexus-nsh`, TSX) are one product drawn in two media. A user who moves between them should find the same structure, wording and behaviour, so every UI change lands in both, in the same change. Porting later is how the clients drifted apart.

## 1. Decide the shared change

State the change in terms of the product, not one client's code: what appears, where it sits in the structure, what it says, and how it responds. Then translate that into each medium: cells and rows in the TUI, views and pixels in NSH.

These must match:
- Structure and layout: which regions exist, what they contain, and their order.
- Content: rows, labels, numbers, formats, and limits such as clipping counts.
- Behaviour: states, transitions, confirmations, focus and Esc handling.
- Visual meaning: which roles, colours and glyphs signal what.

Only the medium's mechanics differ: exact sizes, and how a shared design is rendered. A hover style in NSH and a focus style in the TUI can express the same affordance. If that equivalence changes what the user can do or see, treat it as a difference (step 2).

If the change touches commands, keys, `when` rules, settings or colours, edit `interface/manifest.toml` or `interface/theme.toml` rather than either client, then run `python3 interface/check_manifest.py` and `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml`.

Find the counterpart code with `references/client-map.md`.

## 2. Client-specific differences need approval

If the change cannot or should not be the same in both clients, stop and ask the user before implementing the difference. Describe what would differ, why, and the closest shared alternative. This includes differences forced by the medium, and requests that name only one client: ask whether it should apply to both instead of assuming either way.

Without an explicit yes, implement the shared version, or leave the difference out and report it as an open question. Earlier approval covers only the difference it was given for.

Once approved, add a row at the top of the table in `docs/porting-ledger.md`:

```
| <TUI behaviour> | <NSH behaviour>. Client-specific, approved by the user (YYYY-MM-DD) | <the user's reason> |
```

Standing exceptions need no new approval: the iOS and mobile exclusions in `AGENTS.md`, NSH's phone layouts (the TUI has no phone target), the `theme-void.toml` palette overlay, and differences already in the ledger.

## 3. Implement both, then verify both

1. **Tests.** Add or update a scenario that both clients pass when the change affects content or behaviour. Read `references/mock-scenarios.md` first: it has the mock recipes, templates and known limits. Skip this for purely visual changes.
2. **Suites.** Run `cargo test` in `nexus-tui` and the relevant files with `nsh test --target web` in `nexus-nsh`. Report failures you can't fix, along with whether they predate your change.
3. **Screenshots.** Capture the same state in both clients, read the images, and compare them side by side (see the visual section of `references/mock-scenarios.md`). Tests can't tell you whether the two look like one product.

## 4. Record and report

- Update `nexus-tui/README.md` and `nexus-nsh/README.md` wherever they describe the changed behaviour, and any matching page in `docs/`.
- Add a ledger row if the change alters behaviour from old Nexus, and update `PLAN.md` if a step landed.

End with this summary, so a skipped client is obvious:

```
| | TUI | NSH |
| Changed | files | files |
| Tests | result | result |
| Screenshots | compared / not captured | compared / not captured |
Differences: none, or each one with its approval and ledger row
```
