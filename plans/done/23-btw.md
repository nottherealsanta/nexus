# 23. `/btw` rebuilt on the fork

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

**Done (2026-11-11); moved to done/.** Only the paid cache measurement remains, unscheduled until the user approves it. Earlier status (re-checked 2026-11-11): partially done. The implementation is in the tree: Core `tests/test_btw.py` passes
(with the ZMX suites: 123 passed, 1 unrelated ZMX failure), TUI `src/btw.rs` and NSH `src/btw.ts` / `ui/btw.tsx`
exist, and `session.btw` is in the manifest. What keeps it open is only the two unchecked gates at the bottom:
matching TUI/NSH screenshots and the approved real-provider cache measurement. No paid call has been made.

**Implemented (2026-11-09); real-provider cache measurement remains unverified.** `/btw <question>` starts a
hidden `kind: btw` fork and opens a live modal immediately, including while the parent is busy and on mobile.
It copies the frozen system prompt, tool definitions, resolved model/effort, longest settled history and effective
parent cache key. Its marker identifies the read-only side question. No parent tool call, notification, report or
message is created; the parent's context and timeline stay unchanged.

| Decision | Implemented behavior |
| --- | --- |
| **B1. Trigger and UI** | Immediate modal titled with the query; compact live tool rows, `N/10`, streamed Markdown. `i` expands the inherited request prefix; `c` expands the full ordered side context (marker, arguments/results, notices, replies). Esc/close cancels only the side run |
| **B2. Tool budget** | At most **10 outer/nested calls**, persisted and daemon-enforced, including refused attempts. The tenth queues an answer-now notice; further calls are refused. The fork's 20-model-step ceiling also applies |
| **B3. No fork, no subagent** | Daemon refusals for `fork`, `session`, `message_session`, workspace/control/write tools, unannotated or mutating MCP and user extensions. Shell is an exact foreground read allowlist; ordinary command hooks do not run |
| **B4. Mobile** | Same `/btw` modal and context folds; no desktop-only session surface is added |

- [x] **Run storage:** hidden fork session, separate from the source's relational tree so an idle parent does not
  become busy. `fork.parent_id` retains provenance. Parent deletion cleans up side runs; project removal/deletion
  includes them in safety/shutdown checks. Default snapshots/lists/search and client creation events keep them hidden.
- [x] **Modal:** opening, streaming, canonical context folds and close cancellation in both clients. Per-request
  tokens prevent late responses attaching to a newer modal; checkpoint refresh recovers missed stream/idle events.
  Past-answer navigation (R13) remains deferred, not an implementation gate.
- [x] **Running turns:** the in-flight unsettled tool-call suffix is left out. This completes section 9.5's busy-btw item.
- [x] **Core tests:** same prefix/model/effort/cache key, parent unchanged, single-batch and nested 10-call cap,
  write/control/subagent refusals, busy-parent settled prefix and independent cancellation.
- [x] **Docs/ledger:** API start/checkpoint/SSE contract, agents, both client READMEs and capability matrix O5/R13/L11.
- [x] **Visual verification:** matching TUI/NSH desktop and narrow-web modal captures. **11-11:** `tui-btw.png`,
  `nsh-desktop-btw.png`, `nsh-phone-btw.png` in `reports/sections21-23/` (narrow web now opens the left drawer
  first). Title, inherited/context folds, `N/10` progress, tool row and answer match. **Divergence found:** NSH adds
  two button rows ("Expand inherited context", "Expand context") and has no `↑↓ scroll · esc close` footer; the TUI
  has the footer and no buttons. Not fixed here; tracked in [42](../42-btw-modal-parity.md).
- [ ] **Real-provider cache measurement** (unscheduled; needs approval): requires explicit user approval for a paid/model call. Offline prefix
  equality is tested; no paid request has been made for this change.
