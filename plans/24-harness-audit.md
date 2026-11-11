# 24. Harness workflow audit follow-up

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

User approved implementing the cross-project tool/prompt audit, with bounded real
Copilot GPT-6 Luna checks if needed (2026-11-10). Existing session-status/btw/UI and
ZMX work is preserved; no personal daemon restart or deployment is part of this change.

- [x] Fresh Monty checkout per cell with trusted in-memory heap restoration; no replay,
  suspension exhaustion regression, saved-handle permissions and failed-transfer cleanup.
- [x] Common direct/nested hooks, modified-input approval and visible notices; completed
  results survive post-hook expiry, with honest uncertain-effects cancellation messages.
- [x] Session-private default kernels, explicit named-agent sharing, no fork borrowing;
  cancellation and variable isolation tested. Ordinary forks: 40 total tool attempts,
  180-second tool deadline, 20 model steps; btw's persistent 10-call cap stays separate.
- [x] Fork job/background restrictions and explicit frozen read-only extension effects;
  foreground shell default 120 seconds, maximum 600; longer work uses background jobs.
- [x] Corrected restricted-Python/read-coverage/delegation guidance, selective evidence,
  partial-failure examples, paused objectives and bounded validation; docs and ledger.
- [x] Focused/expanded integration: 221 passed. Approved real Copilot GPT-6 Luna smoke:
  direct/code/fork all correct; nested hook block and unchanged parent state verified.
  Tiny-cell snapshot overhead measured (~7 ms warm median); no broad speed claim.
- [x] Final whole-Core rerun recorded: **1400 passed, 1 skipped, 4 failures** confined
  to concurrent ZMX worker expectations/launch events; those files are left untouched.
  This is not an all-green whole-tree gate. [Exact report](../reports/harness-audit-2026-11-10.md).
- [ ] Observe larger-heap checkpoint overhead and longer real workflows before changing
  the current eight-kernel resource cap or claiming general performance gains.
