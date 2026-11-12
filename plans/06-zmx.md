# 6. ZMX mode for Claude Code and Antigravity

Design, lessons, milestones and risks: [zmx-design.md](zmx-design.md) (was `ZMX_plan.md` at the repo root).
Summary: a **subagent** runs on the real `claude` (interactive PTY, hooks, transcript) or `agy` (stream-json) CLI,
which runs its own loop, tools, permissions and sign-in; Nexus shows it as a child session. Off by default, always
labelled, no Nexus permissions inside it, never answers a CLI prompt (it ends as `needs_attention`).

**Resumed (user, 2026-11-10).** Reference `nexus01` and keep ZMX separate from normal sessions and providers.

## Status (re-audited 2026-11-12)

All offline work is done. Commits `306670ac`, `5dee5561` and `5cf811c8` (merged from `plans/zmx`) added subagent sidechains,
worker records and orphans, recovery text, `nexus doctor` rows, agy usage, sanitized fixtures recorded from real runs
(Claude Code 2.1.322, agy 1.5.0), race tests, client scenarios for every M3 state, and resume after an interrupt. On
11-12: the daemon-start-with-ZMX-off test, the `/api/state` fix below, paired captures, and docs.

**Bug found by the captures (11-12, fixed):** `/api/state` session rows had no `worker`, so after a client start or
reconnect a ZMX child lost its `ZMX · …` label and showed Nexus's starting context (system prompt, Tools [7]) instead
of the ZMX card. Mock-driven scenario tests could not see this. `Store.state_snapshot` now joins the frozen `worker`
onto each row (`test_child_runs_stores_standard_records_and_resumes` asserts it).

Tests on 11-12: `test_zmx_workers.py`, `test_zmx_claude.py`, `test_zmx_runtime.py`, `test_zmx_reliability.py`
**128 passed**. `check_manifest.py` and `check_theme.py` clean. No live CLI or model call made on 11-12.

## Remaining

- [x] **Isolation test (11-12).** `test_daemon_with_zmx_off_probes_no_cli_and_registers_no_provider`: building the app
  and serving `/api/provider`, `/api/model` and `/api/config` with ZMX off never looks for or spawns a CLI and lists no
  ZMX provider or model (verified to fail with ZMX switched on). ZMX reads no credentials at all; sign-in is the CLI's.
- [x] **Docs (11-12).** `external-workers.md` rewritten; ZMX sections or rows in `config.md` (`[zmx]`), `api.md`
  (`worker`, `session.worker`, `zmx: true`, `zmx_unavailable`), `models.md`, `auth.md`, `llm-providers.md`, `daemon.md`
  (crash recovery), `docs/README.md`, AGENTS.md's table and both client READMEs. `stream-events.md` needs nothing new:
  ZMX emits the ordinary loop events.
- [x] **D2 recorded (11-12)** in AGENTS.md (the one-timeline rule's second exception) and the porting ledger.
- [x] **M3 paired captures (11-12).** `reports/zmx-2026-11-12/`: parent with five ZMX child cards and each child
  (parallel tools, denial, needs attention, interrupt, recovered), TUI 170×50 and NSH 1440×900 dark, from one real Core
  seeded by `reports/zmx-2026-11-11/visual_core.py` (now with the parent's `session` calls, so NSH can open the
  children from their cards). ZMX content agrees. Two small, general differences seen, not ZMX-specific: the TUI shows
  effort `default` where NSH shows the agent's `low`, and the TUI turn footer shows `provider/model` where NSH shows the
  bare model.
- [x] **Decisions Z1/Z2** confirmed by the user on 11-12 (subagents only; per-vendor control); in [decisions.md](decisions.md) Made.
- [~] **M0 live spikes.** Recorded so far (fixtures in `nexus-core/tests/fixtures/workers/`): a Claude 2.1.322
  transcript and its `subagents/agent-*.jsonl` + `.meta.json` link (`toolUseId`); agy 1.5.0 two-turn stream with
  `--conversation`, a denial, `--mode plan` ending as a plan, and `/usage`. Still unanswered: api-request rebuild,
  `--append-system-prompt`, `StopFailure` payload, `--mcp-config`, `-p` vs interactive quota, hidden requests.
- [ ] **Real end-to-end check (approval):** `orchestrate` launches an agy child and a Claude Code (Haiku) child in a
  scratch directory, each runs a tool, the parent gets the answer, and the session reads correctly in `GET /message`
  and the inspector after a daemon restart (M1/M2 done-when).
- [x] **Claude's own subagents** read-only inside the calling tool's card (`306670ac` Core, `5dee5561` clients).
- [~] **Inspector:** raw CLI entries show as wire events. Rebuilt requests depend on the unanswered M0 question.
- [x] **M4 reliability** offline: recovery text, transcript tail after a crash, orphan records + `nexus workers clean`,
  race tests, agy usage, `nexus doctor` CLI/version/flags.
- [ ] **Vendor terms:** check Anthropic's and Google's current terms for driving a subscription CLI this way before
  release. The Settings note says "Not verified" until then. Keep it off by default.
- [x] Windows: the registry refuses non-POSIX and both clients show `zmx_unavailable` on the ZMX rows (`5dee5561`).
