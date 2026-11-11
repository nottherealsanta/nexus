# 6. ZMX mode for Claude Code and Antigravity

Design, lessons, milestones and risks: [zmx-design.md](zmx-design.md) (was `ZMX_plan.md` at the repo root).
Summary: a **subagent** runs on the real `claude` (interactive PTY, hooks, transcript) or `agy` (stream-json) CLI,
which runs its own loop, tools, permissions and sign-in; Nexus shows it as a child session. Off by default, always
labelled, no Nexus permissions inside it, never answers a CLI prompt (it ends as `needs_attention`).

**Resumed (user, 2026-11-10).** Reference `nexus01` and keep ZMX separate from normal sessions and providers.

## Status (re-audited 2026-11-11)

The previous plan text ("transport groundwork only, no callers") is out of date. Commit `a5e844ef` ("WIP: local
changes before pull") went well past the transport slice. What the code has now:

| Area | State | Where |
| --- | --- | --- |
| agy transport | Done, fake-CLI tested | `nexus/workers/antigravity.py`, `base.py` |
| Claude Code transport (PTY, hook script, transcript reader, attention detection, `--resume` with policy fingerprint) | Implemented, fake-CLI tested | `nexus/workers/claude_code.py`, `claude_transcript.py` |
| `ExternalAgent` → normal loop events, resume via `message_session` | Implemented | `nexus/workers/agent.py`, `runtime.py` (`_agent()` ~2292) |
| Registry: ids `claude-code` / `agy`, labels `ZMX · Claude Code` / `ZMX · Antigravity`, user-only enable check, CLI discovery (`NEXUS10_AGY_CLI` override), `claude/…` → `claude-code/…` child mapping | Implemented | `nexus/workers/registry.py` |
| Config keys `[zmx] claude_backend = "sdk"\|"zmx"`, `antigravity = bool` (off by default) | Implemented | `config.py` |
| Root refusal, guards that change nothing, normal children unchanged, `session.worker` event + `worker` snapshot facts | Implemented, tested | `runtime.py` ~533/1243, `tests/test_zmx_runtime.py` |
| Catalog / provider rows; models carry `zmx: true` and are hidden from root pickers | Implemented in Core, TUI, NSH | `server.py` ~719, `hub.py`, `catalog.py`, TUI `app.rs` ~4149, NSH `commands.ts` ~868 |
| Settings › ZMX section: `Agent SDK` / `ZMX` and `off` / `ZMX` with the agreed hint (decisions Z1/Z2 taken as their defaults) | Implemented in both clients and the manifest | `interface/manifest.toml` ~430, TUI `app.rs`/`render.rs`, NSH `settings.tsx` |
| Child label and starting-context disclosure (`context incomplete`, `CLI inherited`, `Not applied`) | Implemented in both clients | `interface/scenarios/zmx-child.json`, TUI `timeline.rs`, `tests/scenarios.rs`, NSH `newsession.tsx` `zmxSections` |

Tests on 2026-11-11: `test_zmx_claude.py`, `test_zmx_runtime.py`, `test_zmx_workers.py`, `test_btw.py` give
**123 passed, 1 failed**. The failure is `test_zmx_workers.py::test_no_import_or_registry_coupling`, which still
asserts the old "no production caller" isolation and is now intentionally wrong (`server.py` imports
`workers.registry`). No live CLI or model call has been made. No screenshots.

## Remaining

- [~] **Fix the stale isolation test.** Replace it with the real invariant: importing `nexus.workers` and starting
  the daemon with ZMX off probes no CLI, reads no credentials and registers no provider.
  **11-11:** `test_no_import_or_registry_coupling` now allows `server.py` (its gated `workers.registry` import) and
  checks that importing `nexus.workers` loads neither vendor module; `tests/test_zmx_workers.py` 77/77 pass. Still
  missing: the daemon-start-with-ZMX-off check (no CLI probe, no credential read, no provider registered).
- [ ] **Docs are stale.** `docs/external-workers.md` says "no provider, configuration switch, runtime routing, API or
  client UI"; rewrite it for the current state. Then `llm-providers.md`, `auth.md`, `models.md`, `config.md` (`[zmx]`),
  `daemon.md`, `api.md` (`session.worker`, `worker` snapshot, `zmx: true` on models), `stream-events.md`, both client
  READMEs. All have 0 ZMX mentions today.
- [ ] **Timeline-rule exception (D2):** record it in AGENTS.md and the porting ledger. M1 has effectively landed, so
  this is now due.
- [ ] **Decisions Z1/Z2:** the code follows the defaults (subagents only; per-vendor control). Ask the user to confirm,
  then move them from Open to Made in [decisions.md](decisions.md).
- [ ] **M0 live spikes** (user approval needed; real, small runs): Claude transcript request shape, `--resume`,
  `--append-system-prompt`, `StopFailure`, `--mcp-config`; agy 1.4.12 stream-json turn, `--conversation`,
  `--mode auto`, soft denial, interrupt, `--print-timeout 0s`. Record each answer and a sanitized fixture. Both
  parsers are currently built on the `nexus01` documented shapes, not fresh captures.
- [ ] **Real end-to-end check (approval):** `orchestrate` launches an agy child and a Claude Code (Haiku) child in a
  scratch directory, each runs a tool, the parent gets the answer, and the session reads correctly in `GET /message`
  and the inspector after a daemon restart (M1/M2 done-when).
- [ ] **Claude's own subagents** shown read-only inside the calling tool's card. Not verified as implemented.
- [ ] **M3 clients:** mock scenarios for parallel tools, a denial, `needs_attention`, an interrupt and a recovered
  turn (only the plain completed child exists in `zmx-child.json`); the inspector shows raw CLI entries; matching
  TUI/NSH screenshots.
- [ ] **M4 reliability:** recovery text, reading the rest of a transcript after a crash, orphan records and offer,
  race tests (cancel during `Stop`, exit before flush, duplicate hook, transcript rewrite), agy usage in the usage
  panel, `nexus doctor` reports CLI/version/flags.
- [ ] **Vendor terms:** check Anthropic's and Google's current terms for driving a subscription CLI this way before
  release. The Settings note says "Not verified" until then. Keep it off by default.
- [ ] Windows: external providers marked unavailable (the registry refuses non-POSIX); confirm the clients show why.
