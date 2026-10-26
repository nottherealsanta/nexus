# TODO

Each row quotes the original TODO text unchanged and links to its plan in `plans/planning/` or
completed record in `plans/done/`. State reflects implementation and verification evidence;
tick only when plan acceptance criteria are met. Implemented work with remaining acceptance
gaps stays unchecked and in planning. Focus: the native Ratatui client (`nexus chat`); desktop
(GPUI) is paused and the web client is to be deprecated (`AGENTS.md`).

| # | Original item (verbatim) | Plan | State |
| --- | --- | --- | --- |
| 1 | "auto move to new session while talking to manager" | [10 · Manager screen and auto-navigation](plans/done/10-manager-screen-and-auto-navigation.md) §4.4 | Implemented (2026-10-26); offline tests pass; not verified against real Codex or GPT Live |
| 2 | "Manager your screen needs to look different - open with ctrl + x and "." . this also opens when gpt-live is initiated. but manager can be talked to by composer itself as well." | [10 · Manager screen and auto-navigation](plans/done/10-manager-screen-and-auto-navigation.md) §4.1–4.3 | Implemented (2026-10-26); offline tests pass; no screenshot review |
| 3 | "worktree merge should not ask for permission to merge" | [11 · Worktree landing and auto cleanup](plans/planning/11-worktree-landing-without-prompts-and-auto-cleanup.md) §4.1–4.3 | Canonical landing already exempt (`nexus/tools/permissions.py:1513`); remaining prompts to be measured |
| 4 | "Auto delete worktree when done with it" | [11 · Worktree landing and auto cleanup](plans/planning/11-worktree-landing-without-prompts-and-auto-cleanup.md) §4.4 | Implemented; offline verified, complete automated acceptance pending |
| 5 | "subagent screen/modal should look like the root agent except that there is back button which is the first two cols of TUI ( with a <-  in the center )" | [12 · Subagent page with back rail](plans/planning/12-subagent-page-root-layout-with-back-rail.md) | Implemented; offline/PTY verified, pixel/contrast review open |
| 6 | ""Notice" needs to be better. We need to classify that there are two things that will be on the screen. One is we are showing the user one-to-one mapping of the context of the agent. Second one, things like notice should appear in a box, and there should be an X button to close them." | [13 · Context rows vs dismissible notices](plans/planning/13-context-rows-vs-dismissible-notices.md) | Implemented; offline/PTY verified, human visual review open |
| 7 | "Collapsed tools need to render better. For example, the shell tool, when uncollapsed, the first two lines should show the input, the 4 lines (scrolling) output. When clicked on input it should show the full input in a modal just in case if it's too big if it's more than say four lines." | [14 · Shell tool preview and input modal](plans/done/14-shell-tool-preview-and-input-modal.md) | Implemented; native PTY acceptance verified (sampled progress) |
| 8 | "when a session is selected from the sessions modal, the modal should close.  /new" | [15 · Sessions selector closes](plans/done/15-sessions-selector-closes-on-open-and-new.md) | Implemented; native restart against temporary daemon and preference reload verified |
| 9 | "![alt text](image.png) better way to render this" (the image is the "Add project" folder picker) | [16 · Add project picker redesign](plans/planning/16-add-project-folder-picker-redesign.md) | Implemented; offline/PTY verified, pixel before/after review open |
| 10 | "a toggle to show tabs as vertical on the left sidebar(new)" | [17 · Vertical tabs](plans/planning/17-vertical-tabs-left-rail.md) | Implemented; offline/PTY verified, pixel/contrast review open |
| 11 | "new messages takes a second to show up ( remove the lag ) (same for steer to interupt promotion )" | [18 · Message and interrupt latency](plans/planning/18-message-and-interrupt-latency.md) | Open; worktree p95 135.465 ms exceeds 100 ms target; interrupt-frame correlation missing |

## Checklist

- [ ] 1. Auto-move to a session the Manager launches (plan 10 §4.4)
- [ ] 2. Manager screen, `Ctrl+X .`, opens on GPT Live, composer talks to it (plan 10 §4.1–4.3)
- [ ] 3. Worktree landing without a permission prompt (plan 11 §4.1–4.3)
- [ ] 4. Automatic checkout removal after landing (plan 11 §4.4)
- [ ] 5. Subagent page with root chrome and a two-column back rail (plan 12)
- [ ] 6. Context rows vs boxed, closable notices (plan 13)
- [x] 7. Shell tool: 2 input rows, 4-row output window, full-input modal (plan 14)
- [x] 8. Sessions selector closes on open and on `/new` (plan 15)
- [ ] 9. Add project picker redesign (plan 16)
- [ ] 10. Vertical tabs toggle (plan 17)
- [ ] 11. Message and interrupt latency (plan 18)

## Suggested order

1. Quick, independent fixes: 8 (plan 15), the `Ctrl+X .` shortcut and auto-move gaps from plan 10.
2. Measurement before change: 11 (plan 18 §4.1) and 3 (plan 11 §4.1).
3. Transcript presentation: 6 (plan 13), then 7 (plan 14).
4. Layout work that touches `render::regions`: 5 (plan 12), then 10 (plan 17); do them in
   sequence, not in parallel, because both change the left columns.
5. Larger design items once the user confirms the open questions: the Manager screen (plan 10
   §4.1/§4.3), the picker (plan 16), cleanup (plan 11 §4.4).

## Decisions needed from the user

Each plan lists its open questions; the ones that block work:

- Plan 10: the Manager screen design; keep `Ctrl+X A` beside `Ctrl+X .`; Live switching the screen
  reverses a documented decision.
- Plan 11: whether `/worktrees` Integrate also skips its confirm; moving the session back to the
  main checkout after landing.
- Plan 12: keep the header `←` from plan 09 or only the rail.
- Plan 14: wheel scrolling inside the output window.
