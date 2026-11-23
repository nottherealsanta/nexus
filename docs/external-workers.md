# External workers: Claude Code (PTY) and Antigravity (ACP) as subagents

An **external worker** runs a **child session** on the real `claude` CLI (Claude Code, in an interactive PTY) or the real `agy` CLI
(Antigravity, ACP v1 over stdio: `agy acp`). The CLI runs its own loop, tools, permissions, settings, MCP servers,
plugins and sign-in. Nexus starts it and records what it can observe as ordinary turns and messages. In both clients,
the parent card shows the agent name on the first line and `PTY · Claude Code` or `ACP · Antigravity` in the metadata
below. The child's heading retains the worker's label. A legacy Antigravity stream-json history shows the neutral
`CLI · Antigravity` instead, because it never used ACP. The feature was called ZMX before 2026-11-23.

It is **off by default** and **subagents only** (never a root session or the Manager). Claude Code keeps the original
contract: Nexus answers no CLI prompt, and trust, sign-in and permission prompts end the turn as `needs_attention`.
Antigravity is the one exception: its ACP permission requests go to the **existing Nexus approval cards**, and Nexus
returns the user's allow-once or deny for that request (see [Approvals](#approvals-antigravity-acp)). Nexus still does
not answer sign-in or trust. An authentication failure ends the turn as `needs_attention` with the instruction the user
must run; other unsupported ACP client requests are refused. Design, milestones and open questions:
[plans/zmx-design.md](../plans/zmx-design.md), status in
[plans/06-zmx.md](../plans/06-zmx.md).

**Vendor terms are not verified** (text read 11-12, see [plans/06-zmx.md](../plans/06-zmx.md)). This plan has not established that driving a subscription CLI this way is permitted
by Anthropic's or Google's terms. The Settings note says so; the switch stays off until the user turns it on.

## Switching it on

User-level `config.toml` only (a project file cannot turn it on; see [config.md](config.md#workers-settings)):

```toml
[workers]
claude_backend = "cli"   # "sdk" (default): `claude/…` is the Agent SDK provider; "cli": children run the CLI
antigravity = true        # default false
```

`[zmx]` with `claude_backend = "zmx"` is still read as an alias, and older clients may keep writing it. Settings ›
External workers shows the same two rows (`Agent SDK` / `PTY`, `off` / `ACP`) with the disclosure line, in both
clients. On a non-POSIX machine the rows say why external workers are unavailable (`workers_unavailable` on
`GET /api/config`; `zmx_unavailable` remains for older clients).

While a switch is off: no CLI is probed or started, no credentials are read, no `claude-code`/`agy` provider or model is
listed, and a `claude-code/…` or `agy/…` ref is refused with the setting to change. Existing external worker children stay readable.
`tests/test_zmx_runtime.py::test_daemon_with_zmx_off_probes_no_cli_and_registers_no_provider` holds this.

The ACP **protocol** was checked live against a signed-in `agy` **1.5.14** in a scratch directory: two small turns,
`session/new`, the four mode ids, `session/set_model`, `session/set_mode`, a real `write_file` permission request with a
deny, and `session/resume` in a fresh process.

The **full Nexus path** then passed on 2026-11-23 in a scratch Core (`$NEXUS10_HOME`, agy 1.5.14): an
`agy/gemini-3.8-flash-low` child session ran through Nexus, its actual ACP `write_file` request appeared on the approval
API as `Antigravity · write_file — File approval-test.txt`, and a deny left `approval-test.txt` absent. The failed turn
ended `needs_attention`; a follow-up resumed the same ACP session (`session_id` unchanged) in a fresh process (new pid)
and returned `resumed`. Token usage was recorded for both turns. Evidence:
`/tmp/nexus-zmx-live.anqGQS/results.json`, `after_deny.json` and `after_followup.json`.

The API E2E is **not** a daemon-restart test: both turns ran in the same daemon process. Separate visual checks then
passed in the TUI and NSH. The **approval/prompt card** matched for a short `read_file` request and for a long `execute`
command after both clients were changed to wrap a parameter value to three rows (`reports/acp-approval-parity-2026-11-23/`).
The **denial card** now puts the full `Needs attention` error in wrapped text under the title in both clients, and the
shared scenario asserts the full text (`reports/zmx-attention-parity-2026-11-23/`). Mobile was not checked; it and the
daemon restart stay open in [plans/06-zmx.md](../plans/06-zmx.md).

## Providers, models and routing

| Provider id | Label | CLI | Transport | Models |
| --- | --- | --- | --- | --- |
| `claude-code` | `PTY · Claude Code` | `claude` (found as the `claude` provider finds it) | interactive PTY | models.dev `anthropic` ids |
| `agy` | `ACP · Antigravity` | `$NEXUS10_AGY_CLI`, then `PATH`, then `~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin` | ACP v1 over stdio (`agy acp`) | `agy models` (about 6 s; warmed at Core start, cached 60 s, a stale list is served while it refreshes) |

Both have dialect `external-cli`. `GET /api/provider` lists them only when switched on, with `zmx: true` and no sign-in
methods (sign-in is the CLI's own: `claude auth login`, or `agy acp --login`). `GET /api/model` marks their models
`zmx: true`; clients list those in subagent pools, and in Change model only while a subagent is viewed, never for a root session or a draft.

A child gets an external-worker model explicitly (an agent's `model:`, an `[agents.models]` pool, or `POST
/api/session` with a `parent`), or by mapping: with `claude_backend = "cli"` (or the old `"zmx"`), a child whose model
is `claude/<id>` runs as `claude-code/<id>` (`workers.registry.child_ref`). Roots, the Manager and read-only forks
refuse external-worker models.

## How a turn runs

`Runtime._agent()` returns `workers.agent.ExternalAgent` for an external-worker model instead of the Nexus loop. It yields the same
loop events, so turns, requests, tool rows, usage, messages, the event log, export, the inspector and `via Manager` all
work unchanged. One CLI process per Nexus turn; the next turn resumes the CLI's own session.

| | Claude Code | Antigravity |
| --- | --- | --- |
| Transport | Interactive PTY. Only cursor-position replies are ever written to it | ACP v1 JSON-RPC 2.0 over stdio; `agy acp`; one process per Nexus turn |
| argv | `--model M --session-id S` (first turn) or `--resume S`, `--permission-mode`, `--settings <hooks>`, `--disallowedTools AskUserQuestion`, `--effort` when set, `-- <prompt>` | `agy acp`; the prompt, model and mode travel as ACP requests, not flags |
| Completion | The `Stop` hook (an isolated `python -I` script) after the transcript is read to the end | `session/prompt` returns `stopReason == "end_turn"` and a non-empty answer |
| Events from | The CLI's own transcript (`~/.claude/projects/…/<session>.jsonl`), which must resolve under the CLI's config directory | `session/update` notifications and the prompt result |
| Text | Arrives per block (the transcript is written per block), not per token | `agent_message_chunk` text deltas stream live |
| Permission requests | None are answered; a prompt ends the turn as `needs_attention` | `session/request_permission` goes to the existing Nexus approval card; only per-call allow/deny is returned |
| Nexus `ask` / `allow` session permission | `ask` → `dontAsk`; `allow` → `auto` (the CLI's classifier) | Not mapped. Every new child starts in ACP mode `default`; `accept-edits` and `auto` are not selected |
| Bypass | Never | Never: `always-proceed` is refused and Nexus never passes `agy acp --dangerously-skip-permissions` |

### Antigravity ACP setup

The worker is fail-closed: it sends no prompt until all of these hold. Any failure here ends the turn before the model
sees the assignment.

1. `initialize` negotiates ACP protocol **v1**; any other version fails.
2. `session/new {cwd, mcpServers: []}` creates the child, or `session/resume` continues one. Resume is used only when
   the stored row has transport `acp-v1`, a session id and a matching policy fingerprint, and the agent advertises
   `session/resume`; otherwise the child is refused, not continued as a new conversation.
3. **Model.** The requested id must be exact. If it is one of the advertised `model` config values, the worker sets it
   with `session/set_config_option` and requires the returned current value to match. Otherwise, if it is in the
   advertised `models.availableModels` set, the worker uses the agy `session/set_model` extension (`modelId`, not part of
   public ACP v1) and waits for the following `config_option_update` to confirm the base model and, for a `-low`/
   `-medium`/`-high` id, the effort. An older agent with no model surface at all may accept `session/set_model` and
   confirm the exact id in its result. Anything else fails before the prompt. The 1.5.14 probe showed base ids in the
   `model` config option but exact per-effort ids in `models.availableModels`, and `session/set_model` accepted
   `gemini-3.8-flash-low`. The live Nexus run used that exact id through this path; it is also covered by the sanitized
   fixture.
4. **Mode.** The agent must advertise session modes that include `default`, and `session/set_mode` must not report a
   different current mode. `default` is chosen for every new child because it is expected to prompt; `accept-edits` and
   `auto` could approve workspace edits without the user seeing them. The 1.5.14 probe advertised `default`,
   `accept-edits`, `auto` and `plan`, with `default` described as "Ask before edits and commands".
5. **Effort.** When Nexus resolved an effort (and the model id is not already suffixed `-low`, `-medium` or `-high`), the
   agent must advertise exactly one `thought_level` option that allows it; it is set with
   `session/set_config_option`, and a different reported value fails.
6. `session/prompt` sends one text block. `prompt` has no images; images are refused before launch. The first turn
   prefixes the Nexus agent instructions with `[Nexus assignment instructions]`; later turns send the user text alone.

The policy fingerprint covers the transport, model, mode, effort, resolved directory, empty MCP server list and the
absent system prompt. A resume with a different fingerprint is refused with "start a new child session".

### Legacy Antigravity transport

Older Antigravity children ran the headless stream-json CLI (`--input-format stream-json --output-format stream-json`,
`init`/`step_update`/`result`, `--conversation` for follow-ups). Their messages, tool rows and worker card remain readable,
including after a restart. They **cannot be continued**: a prompt fails before anything is committed, the worker row
becomes `failed` with "created by the legacy transport", and the user is told to start a new child. A row with no
transport marker and existing history is treated the same way; a brand-new Antigravity child uses ACP.

Claude Code is unchanged in this migration: it still uses the interactive PTY, hooks and transcript.

### Approvals (Antigravity ACP)

An ACP `session/request_permission` request becomes an ordinary Nexus approval. It uses the same card, keyboard
shortcuts, mobile card and `via Manager` path as a Nexus tool call; there is no separate external-worker approval UI. The card's
tool line is `Antigravity · <tool name> — <CLI title>` (for example `Antigravity · read_file — Read a.txt`), and its
`call_id` is the ACP tool-call id (`agy:…`) so the card joins the tool row in the timeline. The signed-in 1.5.14 probe
confirmed the protocol side: a real `write_file` request carried `allow_once` (`allow`) and `reject_once` (`deny`)
options, and selecting deny produced a failed tool call. The full Nexus path was then exercised live through the
approval API (denied, file absent); TUI/NSH card visuals were verified separately.

- **Only per-call options are used.** Nexus selects the option whose kind is `allow_once` or `reject_once` (agy 1.5.14
  names them `allow` and `deny`). It never selects `allow_always`, `reject_always` or any other persistent option, and
  it never changes the CLI's own settings. If the request offers no per-call allow option, no card is shown: the worker
  selects `reject_once` when available, otherwise cancels the request. A request that offers only persistent options is
  cancelled with a note.
- **The externally visible answer is only that option id.** Nexus does not send rewritten tool arguments back to the
  CLI. What the card displays is a bounded, sanitized copy of the CLI's claimed payload for the user to judge, not the
  payload Nexus executes or approves.
- **Nexus rules deliberately do not apply.** `AUTO_APPROVED` (`read`, `grep`, …), `tools.permission` rules, an agent's
  permission rules and the session's `allow` policy cannot decide an ACP request. A child on an `allow` session still
  shows the card, so the CLI's claimed tool name cannot borrow a Nexus tool's pre-approval.
- **Allow once** selects the per-call allow option for this request only.
- **Allow for session** selects the per-call allow option now and stores a runtime grant for the exact tool name and JSON argument
  payload (same keys and values; the key is a hash of the canonical pair). A later request with the same name and payload is allowed without another card; any
  different argument (a different path, command, or even a different string value) prompts again. The grant is
  per-session, is not persisted, is lost when the daemon restarts, and is cleared by clearing session grants. A deny
  never grants anything.
- **Deny** selects the per-call reject option for this request (or cancels if none exists). The CLI may continue
  without that tool; the turn ends `failed` with "declined" and the worker row ends `needs_attention` with the denied
  action recorded. A deny note stays in Nexus's `permission.replied` record; ACP receives only the reject option.
  **Nexus approved nothing.** The parent's escalation says Nexus denied the ACP request; the older "the CLI prompt
  cannot be answered by Nexus" wording is for Claude and legacy rows only.
- **Fail closed.** If approval cannot be obtained — no callback, a callback error or exception, a non-boolean answer, an
  interrupted wait, or a prompt that ends while a permission is unanswered — the request is rejected or cancelled. It is
  never allowed by default. Interrupting the turn cancels the pending card and sends `session/cancel` before the process
  group is stopped.

**Trust and security limits.** The approval card is a gate only when the ACP agent asks. The CLI's own settings, allow
rules, MCP servers and plugins can pre-approve an action or stop it asking at all; a buggy or malicious CLI can also
mislabel the tool, send a payload that differs from what it executes, or skip `session/request_permission` entirely.
Nexus cannot verify or enforce what happens inside the CLI, so this is **not a sandbox**: the process has the user's
account, the workspace's files and its own permissions. The ACP permission payload is untrusted input and is displayed as
data. Nexus advertises no ACP filesystem or terminal client capabilities and sends `mcpServers: []`, so the CLI does not
receive Nexus's MCP servers or client-side tools. There is no wall-clock deadline, so the CLI can continue spending
subscription quota until the turn ends or the user interrupts it.

Every inbound line becomes a `WireEvent` (`context_complete: false`) before it is interpreted, so the inspector's wire
lane shows the CLI's entries. Nexus's own outbound ACP requests are not wire events, and the inspector does not yet
reconstruct them. Tool calls keep the CLI's real name and arguments (that is what its model saw); `raw.display` carries a
Core mapping (`read` → `read`, `execute` → `run_command`, the Claude names as before, anything else `generic`) so both
clients pick the same card. Claude Code's own subagents (`<session>/subagents/agent-*.jsonl`) are read read-only and
attached to the calling tool's result (linked by the sidechain's `toolUseId`) as `raw.sidechain`; both clients show them
inside that tool's card. Nexus trees stay one level deep.

Usage is what the CLI reported; when it reports none, usage is unknown (`null`), never zero. No cost is estimated.
For ACP, the prompt result's token fields are normalized per turn (`inputTokens` → input, `outputTokens` → output,
`cachedReadTokens` → cached read, `thoughtTokens` → reasoning); `usage_update` is a context used/size notification and
stays wire-only. A turn whose result has no usage fields stays unknown. Antigravity quota still comes from
`agy --print /usage --output-format json` in the usage panel, only while switched on.

## What the session shows (the D2 exception)

A CLI adds its own system prompt, tool definitions, memory files and reminders that Nexus cannot see. For external worker children
only, the user accepted (decision D2, 2026-11-04) that the timeline cannot show the model's full context. Instead of
Nexus's starting context, the child's timeline opens with a **worker card**:

```
ACP · Antigravity   context incomplete
Needs attention     <instruction, when the last turn needed the user>
Denied through Nexus <n> actions, when Nexus denied an ACP request
Run                 model … · mode/permission … · cwd …
Settings            CLI inherited
Not applied         Nexus permission rules, disabled tools, step limits
```

Its facts come from the session's `worker` object (below). Everything Nexus *does* observe (the prompt, the CLI's
replies, tool calls and results, recovery notices) is shown in order as usual. This is recorded in AGENTS.md and the
[porting ledger](porting-ledger.md). Antigravity approval cards do not change this exception: they show the user the
CLI's claimed request, not the CLI's hidden system prompt, tools or memory. A legacy stream-json or Claude card keeps
`Denied by the CLI`; an ACP denial says `Denied through Nexus`, because the user made that decision. Both clients wrap
the full `Needs attention` error under its title.

## The `worker` object

Frozen in the session's snapshot, updated per turn, and sent as `worker` on session JSON, on `/api/state` session rows,
on `session.updated`, and as its own `session.worker` event (see [api.md](api.md)). `null` for ordinary sessions.

| Field | Meaning |
| --- | --- |
| `provider`, `label`, `model`, `directory`, `permission_mode`, `effort` | What the CLI was started with. Antigravity ACP children use `permission_mode: "default"`; legacy stream-json rows keep the mode they were started with |
| `status` | `not_started`, `running`, then the CLI's outcome: `completed`, `needs_attention` or `failed` (an interrupted turn stays `running` in the record; the turn row says `interrupted`) |
| `error` | The instruction or failure text (for `needs_attention`: the exact command to run) |
| `transport` | `acp-v1` for new Antigravity children, `stream-json-v1` for legacy rows; Claude rows may omit it. A missing marker with existing Antigravity history is treated as legacy |
| `session_id`, `transcript_path`, `policy_fingerprint` | The CLI's session/conversation id, (Claude) transcript path, and the policy the worker started with. Claude Code and ACP resume require the same policy fingerprint; ACP also requires a session id |
| `context_complete` | Always `false` |
| `disclosure`, `not_applied`, `settings`, `mcp` | The authority statement shown on the card |
| `usage` | Last reported usage or `null`; ACP maps the prompt result's token fields, and stays `null` when the result has none |
| `denied_actions`, `notes`, `diagnostics` | What the CLI or the approval path refused; notes about blocks and wire-only records (ACP thought chunks, unknown updates, context usage); and sanitized stderr |

## Refused on an external worker child

Each returns 409 (or a tool error) with a reason and changes nothing (`test_guards_change_nothing`): switching backend,
model, agent or permission policy (start a new child); changing directory; one-shots and `/btw`; handoff; forking;
continuing without a user message; prompts with images. A prompt on a legacy or unknown-transport Antigravity child also
fails before a message is committed. Steering while busy queues the input for the next turn.

## Recovery and orphans

`$NEXUS10_HOME/workers/records/<session>.json` (directory `0700`, ids validated) records the pid, process start time,
provider, transport, CLI session id and (Claude) transcript of a running turn; it is removed when the process stops
normally. After a daemon crash:

- The turn reconciles as usual, but the recovery text says the CLI ran the tool and **may still be running**; nothing
  is retried.
- For Claude Code, the rest of the transcript is read once and added as labelled recovered output.
- The CLI session id is kept, so the next prompt resumes the CLI session. For Antigravity ACP there is no transcript to
  finish from, and resume needs the `session/resume` capability and the same policy fingerprint; otherwise the next
  prompt is refused and the user starts a new child.
- A process that outlived the daemon is never killed automatically. `nexus doctor` lists it, and `nexus workers clean`
  stops it only if both the pid and its start time still match.

`nexus doctor` reports each switch (`off (no CLI probed)` while off) and, when on, the CLI path, version and required
transport. Its Antigravity row uses `AntigravityAcpWorker.preflight()` and requires **agy 1.5.0 or newer** (`agy acp`
first appeared there); an older CLI fails with an upgrade instruction. A successful row says `ACP v1 stdio (agy acp)`
and `sign-in not verified (run agy acp --login)`.

## Workspace trust

This section is Claude Code only. Antigravity ACP has no equivalent Nexus-written trust state; Nexus never writes
Antigravity configuration. A signed-out or otherwise blocked `agy acp` ends the turn with the CLI's message, or with
the `agy acp --login` instruction for an authentication failure.

Claude Code asks "trust this folder?" once per directory and stores the answer as
`projects[<dir>].hasTrustDialogAccepted` in its global config (`~/.claude.json`, or under `$CLAUDE_CONFIG_DIR`). The CLI
has no flag for it: `-p` skips the dialog, but Nexus drives the interactive CLI. So before launching, Nexus records that
key itself, and only when all of these hold:

- the session's directory is inside a **registered Nexus project**, or is a linked git worktree of that project
  (read from the worktree's `.git` file); the project folder is trusted, or the worktree folder itself;
- no folder at or above it is already trusted;
- the config file already exists and parses (Nexus never creates or repairs it);
- the folder is not `$HOME` or `/`.

The write is atomic (temporary file and rename, same mode) and adds nothing else. The child's notes record
"Nexus marked <dir> trusted in Claude Code". Any other folder still ends `needs_attention`; Nexus does not answer the
dialog and never trusts a folder the user did not add as a project. Decision 2026-11-13 (user: auto-trust Nexus projects
and their worktrees; ledger row in [porting-ledger.md](porting-ledger.md)).

## Bounds and trust

Prompts over 64 KiB or containing NUL are refused, never truncated. Output lines are bounded at 4 MiB and 32 MiB in all;
the answer buffer at 64 Ki characters with announced clipping. Stderr keeps its last 16 KiB, strips terminal controls
and redacts labelled credentials. The Claude Code and legacy stream-json preflight (`--version`, `--help`, stdout and
stderr) is bounded to 64 KiB and five seconds per probe. ACP preflight and `nexus doctor` read `--version` only (and the
doctor enforces agy 1.5.0 or newer). The ACP transport bounds one JSON-RPC message at 4 MiB, the whole stream at 32 MiB,
queued notifications at 1,024 and concurrent inbound client requests at 64; it gives an exited agent 10 s to close its
output. The child environment comes from a fixed allowlist (no API keys, no `PYTHONPATH`).

CLI output is untrusted data. Terminal text is diagnostic only, never assistant prose, and never drives approval:
a model printing something that looks like a prompt can at worst cause a false `needs_attention`. ACP permission
requests are handled only through the approval path above; a model's prose never approves anything. Cancellation sends
`session/cancel` when an ACP session exists, then stops the owned process group (TERM, then KILL), and never signals a
reaped leader or a stored pid. Detached descendants are not guaranteed to be cleaned up. Windows is not supported
(`pty` and process groups are POSIX).

## Tested versions

Offline fixtures recorded from real runs, sanitized: Claude Code **2.1.322** (transcript and a subagent sidechain) and
agy **1.5.0** (legacy stream-json: two turns, a denial, `/usage`), in `nexus-core/tests/fixtures/workers/`. Those agy
fixtures belong to the legacy transport. agy **1.5.14** ACP has a sanitized fixture in
`nexus-core/tests/fixtures/workers/agy-1.5.14-acp/live.json`: the `session/new` result, a real permission request, tool
updates and prompt/resume usage. The ACP tests use that fixture with `fake_agy_acp.py` and `fake_acp_agent.py`; the
fixture came from a signed-in direct probe (not through Nexus) of two small turns. The live Nexus E2E also used agy
**1.5.14** on 2026-11-23. Newer CLIs are not thereby known compatible; unknown transcript, stream or ACP update shapes
are kept as wire events and disclosed, not guessed at. The M0 questions in the design that have no recorded answer are
still open.

## Verification

```sh
cd nexus-core
uv run pytest -q tests/test_zmx_acp.py tests/test_zmx_antigravity_acp.py \
  tests/test_zmx_workers.py tests/test_zmx_claude.py tests/test_zmx_runtime.py tests/test_zmx_reliability.py
```

All use fake executables and scratch homes; no model call. On 2026-11-23 the six external-worker test files listed in
`plans/06-zmx.md` passed **194 tests** offline. The separate live Nexus E2E is described above (approval API, deny, file
absent, same session after a fresh-process resume, usage recorded). It is not a daemon-restart test. The TUI/NSH
approval-card check is in `reports/acp-approval-parity-2026-11-23/`; the denial-card attention check is in
`reports/zmx-attention-parity-2026-11-23/`. The shared `zmx-acp-denial` scenario asserts the full attention text in
both clients: TUI `cargo test --test scenarios` passed 11, and the targeted NSH ACP-denial test passed; the full NSH
scenario file still has the known `Connection reset by peer` flake in unrelated tests. Client scenarios are
`interface/scenarios/zmx-*.json`
(plain child, parallel tools, denial, needs attention, interrupt, recovered), run by the TUI `tests/scenarios.rs` and
NSH `scenarios.test.tsx`. Paired captures from a real Core: `reports/zmx-2026-11-12/` (seeded by
`reports/zmx-2026-11-11/visual_core.py`). Those captures predate ACP and show the legacy Antigravity transport; the
11-23 report above is the ACP approval-card pair.

## Reference to nexus01

Read at `nexus01` commit `c3f18489` (`~/repos/nexus-old`): `nexus/agents/antigravity_worker.py`,
`nexus/agents/claude_code_worker.py` and their tests. Carried over for Claude Code: stdin lifetime, argv, explicit
completion, denials, bounds, diagnostics, group cleanup, PTY and hook handling. The legacy Antigravity stream-json
worker also carried over its stream parsing, stdin lifetime, bounds and diagnostics. **ACP is new**: it is not a port
from nexus01. Not copied: the `external.*` events and reducers, old runtime glue, Nexus read-only enforcement inside the
CLI, the one-year print timeout, and the blanket long-string redactor that masked paths.
