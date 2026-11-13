# 6. ZMX mode for Claude Code and Antigravity

Design, lessons, milestones and risks: [zmx-design.md](zmx-design.md) (was `ZMX_plan.md` at the repo root).
Summary: a **subagent** runs on the real `claude` (interactive PTY, hooks, transcript) or `agy` (stream-json) CLI,
which runs its own loop, tools, permissions and sign-in; Nexus shows it as a child session. Off by default, always
labelled, no Nexus permissions inside it, never answers a CLI prompt (it ends as `needs_attention`).

**Resumed (user, 2026-11-10).** Reference `nexus01` and keep ZMX separate from normal sessions and providers.

## Status (re-audited 2026-11-12)

On 11-13, both parent child cards were changed to show the agent name first and the ZMX vendor label in the metadata line; shared scenario tests cover the wording. Paired screenshots remain to be captured.

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

**Approval (user, 2026-11-12):** the ZMX live runs, including the real end-to-end check and its small spend, are approved.

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
- [x] **Real end-to-end check (11-12; user approved the live runs and the spend on 11-12).** Scratch `$NEXUS10_HOME`
  (`[zmx] claude_backend="zmx", antigravity=true`), scratch workspace under a directory Claude Code already trusts,
  parent `orchestrate` on `claude/claude-haiku-5-5`, children by project agents pinned with `model:`
  (`agy/gemini-3.8-flash-low`, `claude-code/claude-haiku-4-5`), CLIs Claude Code 2.1.322 and agy 1.5.6, each child
  one `read` of `a.txt`. Evidence: `reports/zmx-2026-11-12/live/`.
  Result: the Claude Code child ran `Read`, answered `hello-zmx`, the parent got it. The agy child ran `read_file`,
  answered `hello-zmx`; in run 2 it was reported **failed** ("did not close its process/stream after the terminal
  result") although the answer was there, and passed in run 3. Cause: agy 1.5.6 needs 0.6 to 2.2 s to exit after stdin
  closes (measured directly), against a 3 s grace; `EXIT_GRACE_SECONDS` is now 10 s. After `nexus daemon stop/start`
  the three children read correctly in `GET /api/session/{id}/message` (user, tool_call, tool_result, text, with the
  `worker` card) and `GET …/inspect` (v2, 200). Findings: (1) the `session` tool has no model parameter, so a child's
  ZMX model comes from its agent's `model:` or `claude_backend="zmx"` mapping (the first attempt, with the built-in
  `quick` agent, mapped to `claude-code/claude-haiku-5-5`); (2) a new directory ends `needs_attention` ("Workspace trust
  required") as designed, until the user runs `claude` there once; the first run proved this and I did not approve trust.
  Spend: about six short Haiku/Flash turns plus one direct agy probe. Not run: the Settings/clients against these
  sessions, parallel tools, quota comparison of `-p` vs interactive.
- [x] **Claude's own subagents** read-only inside the calling tool's card (`306670ac` Core, `5dee5561` clients).
- [~] **Inspector:** raw CLI entries show as wire events. Rebuilt requests depend on the unanswered M0 question.
- [x] **M4 reliability** offline: recovery text, transcript tail after a crash, orphan records + `nexus workers clean`,
  race tests, agy usage, `nexus doctor` CLI/version/flags.
- [~] **Vendor terms (read 2026-11-12; no legal conclusion).** The text neither clearly permits nor forbids this, so
  the Settings note keeps saying "Not verified" and the switch stays off by default. What the pages say:
  - Anthropic, [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance): OAuth
    "is intended exclusively for purchasers of Claude Free, Pro, Max, Ultra, Team, and Enterprise subscription plans and
    is designed to support ordinary use of Claude Code and other native Anthropic applications"; developers building
    products, "including those using the Agent SDK, should use API key authentication"; Anthropic "does not permit
    third-party developers ... to route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their
    users", nor to "collect, store, or intermediate Claude.ai credentials or session tokens". It does not restrict "an
    end user from signing in to the unmodified Claude Code binary with their own Claude subscription". Limits "assume
    ordinary, individual usage of Claude Code and the Agent SDK". Enforcement "without prior notice".
  - Anthropic, [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (effective 2025-10-08), section 3: no
    use "to access the Services through automated or non-human means, whether through a bot, script, or otherwise",
    except via an API key "or where we otherwise explicitly permit it". It does not mention Claude Code or OAuth.
  - Google, [Antigravity terms](https://antigravity.google/terms) (no date shown), section 6: no use "in connection with
    products not provided by us"; "Using third party software, tools, or services to access the Service (e.g. using
    OpenClaw with Antigravity OAuth) is a breach", possible suspension. Headless use of `agy` is not addressed. Google
    staff replies on [discuss.ai.google.dev](https://discuss.ai.google.dev/t/is-orchestrating-the-official-antigravity-cli-from-a-local-multi-agent-coding-application-allowed/181088)
    (several similar threads) disagree with each other and are not terms.
  - Read against ZMX: Nexus runs the unmodified CLI under the user's own sign-in and reads no credentials, which is
    what the Anthropic page carves out for end users; whether a local program driving it counts as "automated" under the
    Consumer Terms, and whether `agy` driven by Nexus is "third party software to access the Service", is not answered by
    the text. Open: ask Anthropic and Google in writing, or keep the disclosure. Secondary reports (not checked against
    an official page) mention a separate Agent SDK/`claude -p` credit from 2026-06-15.
- [x] Windows: the registry refuses non-POSIX and both clients show `zmx_unavailable` on the ZMX rows (`5dee5561`).
