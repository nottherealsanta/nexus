# 6. External workers (formerly ZMX) for Claude Code and Antigravity

Design, lessons, milestones and risks: [zmx-design.md](zmx-design.md) (was `ZMX_plan.md` at the repo root).
Summary: a **subagent** runs on the real `claude` (interactive PTY, hooks, transcript) or `agy` (ACP v1 over stdio;
legacy stream-json histories are readable only) CLI, which runs its own loop, tools, permissions and sign-in; Nexus
shows it as a child session. Off by default, always labelled, no Nexus permissions inside it. Claude Code answers no CLI
prompt; Antigravity ACP permission requests go to the existing Nexus approval cards. Sign-in and trust still end as
`needs_attention`; other unsupported ACP prompts are refused.

**Resumed (user, 2026-11-10).** Reference `nexus01` and keep ZMX separate from normal sessions and providers.

## Status (re-audited 2026-11-12)

On 11-13, both parent child cards were changed to show the agent name first and the ZMX vendor label in the metadata line; shared scenario tests cover the wording. Paired screenshots remain to be captured.

## 2026-11-23: renamed to External workers (implemented)

The user-facing name is now **External workers** (decision in [decisions.md](decisions.md)). Claude Code is
`PTY · Claude Code`, Antigravity is `ACP · Antigravity`, and a legacy stream-json Antigravity history shows the neutral
`CLI · Antigravity` rather than falsely claiming ACP. Settings › External workers offers `Agent SDK`/`PTY` and
`off`/`ACP`. The canonical config is `[workers] claude_backend = "cli"` and `[workers] antigravity`; `[zmx]` and the
`"zmx"` value remain read/write aliases, and older clients keep `values.zmx`, `zmx_unavailable` and the `zmx: true`
API flags. Old worker labels and disclosures are normalized in display copies only; historical model-context messages
are untouched. Paired TUI/NSH Settings captures are in `reports/external-workers-rename-2026-11-23/`.
Review follow-up: an old-client write stores the canonical `cli` value, `PATCH /api/config` returns the same
unavailable reasons as `GET`, a new Antigravity child carries its ACP marker from creation (so an early failure is not
shown as legacy CLI), and label normalization works when an old row has no `provider`.

## 2026-11-23: Antigravity ACP migration (implemented; live protocol probe and Nexus E2E passed)

- [x] **Follow-up: no permission prompts by default.** New `allow` children run in ACP `auto`; Nexus rejects or cancels
  any remaining permission request without a card. `ask` still prompts and existing children retain their saved mode.
  Verified offline with fake ACP workers; live Auto behavior was not tested.

**Decision (user):** move Antigravity only from the headless stream-json transport to ACP v1 (`agy acp`); leave Claude
Code's PTY transport unchanged. ACP permission requests use the existing approval cards. A session "allow for session"
grant is limited to the exact tool name and argument payload. Old stream-json Antigravity histories stay readable but
cannot be continued. The worker uses ACP mode `default` (prompting), not `accept-edits`/`auto`, and never bypasses.
Decision recorded in [decisions.md](decisions.md) and the [porting ledger](../docs/porting-ledger.md).

**Implemented (uncommitted work in the tree):** `nexus/workers/acp.py` (ACP v1 stdio JSON-RPC client),
`AntigravityAcpWorker` in `nexus/workers/antigravity.py`, ACP selection and legacy refusal in
`nexus/workers/agent.py`, and `Runtime._approve_external` (`runtime.py`) for approval cards and exact-payload session
grants. Docs updated: `docs/external-workers.md`, this file, the design and the ledger.

**Verified offline (2026-11-23):** 194 tests pass with fake CLIs, the sanitized 1.5.14 ACP fixture and scratch homes:
`test_zmx_acp.py`, `test_zmx_antigravity_acp.py`, `test_zmx_workers.py`, `test_zmx_claude.py`,
`test_zmx_runtime.py` and `test_zmx_reliability.py`. Coverage includes ACP framing/correlation, model selection and
usage, permission fail-closed cases, allow/deny, exact session grants, allow rules not bypassing cards, legacy refusal
with no new message, interrupt, and process records.

**Protocol probe (live, 1.5.14, signed in; not through Nexus):** a scratch probe confirmed `session/new` and
`session/resume` in a fresh process, the advertised modes `default`/`accept-edits`/`auto`/`plan`,
`session/set_model` and `session/set_mode`, and a real `write_file` permission request with `allow_once`/`reject_once`
options whose deny produced a failed tool call. Two small turns ran.

**Live Nexus end-to-end (2026-11-23, passed):** a scratch Core (`$NEXUS10_HOME`, agy 1.5.14) ran an
`agy/gemini-3.8-flash-low` child. The real ACP `write_file` request surfaced on the approval API as
`Antigravity · write_file — File approval-test.txt`; denying it left `approval-test.txt` absent and the failed turn
`needs_attention`. A follow-up resumed the **same ACP session** (`session_id` unchanged) in a fresh process (new pid)
and returned `resumed`. Token usage was recorded on both turns (first 11,363 in / 85 out; follow-up 2,564 in / 2 out
with 9,307 cached). Evidence: `/tmp/nexus-zmx-live.anqGQS/results.json`, `after_deny.json`, `after_followup.json`.

The API E2E did not check the visual clients and did **not** restart the daemon. Separate visual checks then passed in
the TUI and NSH. The **approval/prompt card** was the same for the short `read_file` request and a long `execute`
command, after both clients were changed to wrap a parameter value to three rows
(`reports/acp-approval-parity-2026-11-23/`). The **denial card** now shows the full `Needs attention` error as wrapped
text under the title in both clients, and the shared scenario asserts it
(`reports/zmx-attention-parity-2026-11-23/`). Mobile was not checked; both E2E turns ran in the same daemon process.

**Fixture:** the probe's 1.5.14 shapes are sanitized in
`nexus-core/tests/fixtures/workers/agy-1.5.14-acp/live.json`. It records base ids in `configOptions.model` and exact
per-effort ids in `models.availableModels`; the worker now uses `session/set_model` for the exact id and confirms it
through the following `configOptions` update. It also normalizes the prompt result's token fields per turn. Both are
covered offline and by the live Nexus E2E.

The pre-ACP (stream-json) offline work is done. Commits `306670ac`, `5dee5561` and `5cf811c8` (merged from `plans/zmx`) added subagent sidechains,
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

### ACP items (2026-11-23)

- [x] **ACP transport and approval path.** New Antigravity children select `AntigravityAcpWorker`;
  `session/request_permission` goes to `Runtime._approve_external` and the existing approval cards. Only per-call
  allow/deny is returned; a session grant covers only the exact tool name and argument payload. Fail-closed when no
  usable option or no approval path exists. Claude Code is unchanged. Live-tested through the approval API (deny).
- [x] **Legacy histories.** A stored stream-json or unmarked Antigravity child remains readable; a prompt is refused
  before a message is committed and the worker row says to start a new child.
- [x] **Live ACP protocol probe (1.5.14, signed in).** `session/new`, modes, `session/set_model`, `session/set_mode`,
  a real permission request and deny, and `session/resume` in a fresh process all worked in a scratch directory, with
  two small turns.
- [x] **Nexus end-to-end ACP run (2026-11-23).** Scratch Core, `agy/gemini-3.8-flash-low` child, real `write_file`
  approval surfaced through the API and denied, file absent, `needs_attention` turn, fresh-process resume of the same
  ACP session returning `resumed`, and usage recorded. See the evidence above. The visual check is separate below; a
  daemon restart remains unchecked.
- [x] **Model selection.** 1.5.14 advertises base model ids in `configOptions.model` and exact per-effort ids
  in `models.availableModels`, and accepts the exact id through `session/set_model`; the worker now uses that path and
  confirms the base model/effort from the following `configOptions` update (`test_live_*`); used live with
  `gemini-3.8-flash-low`.
- [x] **Usage.** The prompt result's `inputTokens`/`outputTokens`/`cachedReadTokens`/`thoughtTokens` are
  normalized per turn; `usage_update` stays context used/size and is wire-only. Recorded live on both turns.
- [x] **`nexus doctor` ACP preflight.** It uses `AntigravityAcpWorker.preflight()` and requires agy **1.5.0** or newer,
  reporting `ACP v1 stdio (agy acp)` and `sign-in not verified`; it no longer checks the legacy stream-json flags.
- [x] **ACP fixture and approval-card parity.** A sanitized 1.5.14 ACP fixture is in
  `nexus-core/tests/fixtures/workers/agy-1.5.14-acp/live.json`. TUI and NSH **approval/prompt cards** match for the
  short `read_file` request and for a long `execute` command after both clients were changed to wrap a parameter value
  to three rows; paired captures are in `reports/acp-approval-parity-2026-11-23/`. **Mobile is not checked.**
- [x] **Denial and escalation wording.** An ACP denial says `Denied through Nexus`; legacy stream-json and Claude
  cards keep `Denied by the CLI`. The parent escalation says Nexus denied the ACP request, not that ZMX cannot answer a
  CLI prompt. `interface/scenarios/zmx-acp-denial.json` asserts the card wording; the shared scenario is green in
  both clients as above.
- [x] **Denial-card attention detail.** Both clients wrap the full `Needs attention` error under the title; paired
  captures are in `reports/zmx-attention-parity-2026-11-23/`, and the shared scenario asserts the full text.
- [~] **Inspector.** Inbound ACP lines are wire events; Nexus's outbound ACP requests are not. Rebuilt requests remain
  open.
- [x] **ACP client scenario verification.** The shared `zmx-acp-denial` scenario asserts the full text in both clients;
  TUI `cargo test --test scenarios` passed 11 and the targeted NSH ACP-denial test passed. The full NSH scenario file
  still has the known `Connection reset by peer` flake in unrelated tests.
- [ ] **Daemon restart with an ACP child.** Not run; the live E2E kept one daemon process.

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
- [~] **M0 live spikes (synced 2026-11-16, offline; ACP probe added 2026-11-23).** The boxes in [zmx-design.md](zmx-design.md) section 6 now say what the
  fixtures and the 11-12 live runs answer (Claude 2.1.322 transcript with `api-request-*` entries and `subagents/`; agy
  1.5.0 turns, denial, plan mode and `/usage`; flags and the agy exit grace from the live run). **Still needing a paid
  real run (not approved in this pass):** compare a rebuilt `api-request` with a local capture; `message.id` and parallel
  results; `--append-system-prompt`; the `StopFailure` payload and permission wording under `dontAsk` / `auto`;
  `--mcp-config`; `claude -p` vs interactive quota; hidden requests (`ai-title`); the legacy agy `--mode auto` and its
  soft denial; agy subagents; a real interrupt followed by `--resume`. The ACP probe and Nexus E2E are above. The same list is in
  `reports/pre-release-2026-11/user-checklist.md`.
- [x] **Real end-to-end check (11-12; stream-json, pre-ACP; user approved the live runs and the spend on 11-12).** Scratch `$NEXUS10_HOME`
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
  sessions, parallel tools, quota comparison of `-p` vs interactive. This check used the legacy Antigravity transport;
  it is not evidence for ACP.
- [x] **Auto-trust for Nexus project folders (2026-11-13).** Claude Code trust is pre-recorded for a registered
  project folder or a linked worktree of one (`ClaudeCodeWorker.ensure_trust`); everything else still ends
  `needs_attention`. Offline tests only; not run against the real CLI.
- [x] **Claude's own subagents** read-only inside the calling tool's card (`306670ac` Core, `5dee5561` clients).
- [~] **Inspector:** raw CLI entries show as wire events. Rebuilt requests depend on the unanswered M0 question.
- [x] **M4 reliability** offline: recovery text, transcript tail after a crash, orphan records + `nexus workers clean`,
  race tests, agy usage, `nexus doctor` CLI/version/transport (Antigravity now ACP, minimum 1.5.0).
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

## 2026-11-16 offline follow-ups

- **TUI/NSH differences from the 11-12 captures, fixed.** (1) The effort label: a ZMX child shows its agent's `low` in
  NSH (which reads the full session row) and showed `default` in the TUI (which kept the snapshot row's empty effort); the
  TUI history load now takes `effort` and `effort_source` from the full row too (test
  `the_full_session_row_supplies_the_effort_the_snapshot_row_lacks`). (2) The turn footer shows the catalog name or bare
  model id in both clients when no request recorded a model; the TUI had shown `provider/model`.
- **D5 confirmed:** the `opencode-antigravity-auth` route stays dropped; see zmx-design.md (it signs in with the user's Google
  OAuth and calls Antigravity itself, which section 1 rules out).
