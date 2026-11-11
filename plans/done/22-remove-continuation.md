# 22. Remove the `/continuation` slash command (complete)

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

**Implemented (2026-11-09).** `/continuation` (`session.open_continuation`) and its unused
`session.continued_on` condition are removed from the shared manifest, generated NSH manifest and both clients,
including mobile. Manual `/continue` and `POST …/continue`, `/origin`, and Core's `continued_to` links remain.

- [x] Both clients, shared command regression tests, manifest validation and generated module.
- [x] API/client docs, capability matrix and porting-ledger row.
