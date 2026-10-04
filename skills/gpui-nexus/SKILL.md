---
name: gpui-nexus
description: Build and refine the Nexus Rust GPUI desktop client with verified GPUI lifecycle, async, focus, layout, and native visual review. Use for rust/desktop changes, native desktop design, or GPUI debugging.
---

# Nexus GPUI

Read `docs/desktop.md` and the relevant host contract documentation first. The task is a daemon-backed conversation workspace: sessions at left, readable work in the center, evidence and session details at right. Every parameter and result remains available through labelled disclosures.

## Version and source discipline

This client pins **GPUI 0.2.2**, not GPUI Kit. Reference material is adapted from the established Longbridge GPUI Kit skills; see [SOURCE.md](SOURCE.md) and licenses. Read the installed GPUI source and nearest implementation before choosing an API. Kit examples are conceptual references; do not add Kit or copy newer APIs blindly.

For UI changes read [Design Guides](references/design-guides.md): design thesis, task, relevant visual/interaction sections, and final checklist. For a full redesign read the whole guide. Nexus intentionally owns semantic tokens in `theme.rs` and custom controls, so Kit-specific theme/component mandates map to the existing implementation. User direction overrides visual defaults: `#0B0B0B`, compact function-first layout, restrained angled corners, semantic cyan selection, green success, amber input, red errors. Preserve a usable light alternative.

## Implementation workflow

1. Identify the state owner and keyboard route. Host data and workflows belong to the daemon and shared Python bridge; Rust is presentation only. Keep generation guards, bounded channels/caches, and complete labelled content.
2. Read [contexts](references/context.md) and [async](references/async.md) for lifecycle work; [focus](references/focus-handle.md) for input/overlays; [elements](references/element-best-practices.md) for painting. Entity updates stay on the foreground. Do not re-enter the same entity through a handle inside its update/deferred callback. Keep subscriptions and tasks alive intentionally.
3. Use stable element IDs, region-owned scrolling, intrinsic shrink minima, semantic colors, one icon family, and modest control geometry. Arrow cursor for commands; pointing hand for external links. Keep selection/focus/disabled/error states visible. Never add fictional status indicators.
4. Preserve native Unicode/IME, clipboard, selection, undo, focus restoration, Escape dismissal, and keyboard command parity. Check long content and small windows; hidden panes must remain reachable.
5. Build and test `cargo test --manifest-path rust/desktop/Cargo.toml --locked`. Use installed GPUI test helpers, not newer Kit-only query APIs from [testing references](references/test.md). Run focused Python launch/layering/docs tests when those contracts change.
6. Apply the sibling `native-app-review` skill. Launch the actual compiled application through the Python bridge. Capture and inspect real native screenshots for empty, conversation, tool/detail, settings, picker, question, permission, attachment, and agent states. Refine defects, and record limitations honestly. A picture of an app or a browser mockup is not verification.
7. Update `docs/desktop.md`, module map for new modules, and decisions for lasting choices. Report tested behavior and actual gaps.
