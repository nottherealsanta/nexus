# ZMX: Claude Code and Antigravity inside Nexus

Status (re-audited 2026-11-23): **M1 and most of M2 are implemented in Core and both clients, offline only.** On
2026-11-23 Antigravity moved to ACP v1 (`agy acp`) with Nexus approval cards; Claude Code is unchanged. The ACP protocol
was probed live against agy 1.5.14 and its shapes now have an offline fixture. The full Nexus E2E also passed (approval
API deny, file absent, fresh-process resume, usage recorded); the TUI and NSH approval/prompt cards matched for short and
long arguments, and the denial card's full `Needs attention` error now wraps in both. Mobile and a daemon restart are
not verified.
Written 2026-11-04; moved from the repo root `ZMX_plan.md` to `plans/zmx-design.md` on 2026-11-11. The current
status table and the remaining work are in [06-zmx.md](06-zmx.md); tick items here and add a line to
[landed.md](landed.md) as they land. Keep "not verified" honest.

The legacy stream-json parser follows nexus01 and the 1.5.0 fixtures. The ACP worker follows ACP v1, the 1.5.14 probe
and its fixture (section 4.4). The per-vendor Settings control from section 6 of the old plan supersedes section 4.8's
single switch.

The goal: a Nexus **subagent** can run on the real Claude Code or Antigravity (`agy`) CLI. This is **ZMX mode**. The
vendor's own CLI runs its own agent loop, tools, permissions and sign-in, and Nexus shows all of it as a child session in
the TUI and NSH. This was built on the `nexus01` branch (`~/repos/nexus-old`); this plan brings it to `main`.

Scope, decided by the user on 2026-11-04:

- **Subagents only.** A root session never runs in ZMX mode. Fitting root sessions to each CLI is too much work, and a
  child can accept compromises that a root could not.
- **Off by default.** Nothing changes for anyone until they switch it on.
- **Always labelled.** The user can always tell a subagent is running in ZMX mode.
- **Settings are aligned where that is cheap.** Otherwise the CLI uses its own (section 4.5).

**Antigravity transport change (2026-11-23, user):** new Antigravity children run ACP v1 over stdio, not the legacy
headless stream-json CLI. ACP permission requests go to Nexus's existing approval cards; Claude Code keeps its PTY
contract. Old stream-json histories remain readable but cannot be continued. See sections 4.4 and 4.5.

## 1. What "ZMX" and "simulate" mean here

- **"ZMX" is a historical name, not a dependency.** nexus01's plan started around the ZMX terminal multiplexer and then
  dropped it: *"No ZMX dependency is currently required"*. nexus01 uses a Python PTY for Claude Code and plain pipes for
  `agy`. `zmx` is not installed on this machine, and this plan does not add it. "ZMX mode" was the user-facing name
  until 2026-11-23, when it became **External workers**; no `zmx` program is involved. Code says "worker" or
  "external worker".
- **"Simulate" means driving the real, unmodified CLI.** It does not mean imitating a vendor client on the wire. Nexus
  never reads, extracts or forwards a CLI's tokens and never proxies its model traffic. `~/.gemini/oauth_creds.json` and
  the Claude credentials stay untouched. nexus01 had the same contract.
- **This replaces the earlier direction in PLAN.md section 10**, which said to use the Agent SDK and
  `opencode-antigravity-auth` "rather than ZMX". That project was not read for this plan; going by its name it reuses
  Antigravity's sign-in, which this plan rules out. Read 2026-11-16 (decision D5): it does reuse the user's Google OAuth, so the route stays dropped.

## 2. What nexus01 built

Sources: `plans/archives/CLAUDE_CODE_ZMX_PLAN.md`, `plans/archives/ANTIGRAVITY_PLAN.md`,
`docs/antigravity-troubleshooting.md`, and `docs/agents.md` in `~/repos/nexus-old`.

| | Claude Code worker | Antigravity worker |
| --- | --- | --- |
| Code | `nexus/agents/claude_code_worker.py` (1264 lines) | `nexus/agents/antigravity_worker.py` (868), `nexus/model/providers/antigravity_cli.py` (246) |
| Transport | Interactive CLI in a Nexus-owned PTY, prompt on argv (64 KiB cap) | Headless pipes, `--input-format stream-json --output-format stream-json`, prompt on stdin |
| Readback | JSONL transcript read by byte offset, plus `SessionStart`/`Stop`/`StopFailure` command hooks writing atomic files | NDJSON events on stdout: `init`, `step_update`, `result` |
| Success means | A `Stop` hook. A quiet PTY or an exit is never success | `init`, then `result.status == "SUCCESS"` with a non-empty answer |
| Input Nexus sends | Cursor-position replies only, never approvals | The prompt only |
| Prompts it cannot answer | `needs_attention`: stop and name the manual command | `needs_attention`; agy soft-denies in headless mode |
| Follow-ups | `--resume <session>`, same policy fingerprint required | Not built (`--conversation` exists now) |
| Scope | Subagents only; a main agent used the Agent SDK instead | Subagents only |
| Live evidence | Haiku 5.5 on 2.1.296: trust prompt, `Read`, transcript and `Stop` confirmed | Docs and fake `agy` at first; the troubleshooting doc later records real denial output from agy 1.3.6. No successful live run is recorded |
| Still open at the end | Human handoff, root escalation, Sonnet/Opus IDs, orphan cleanup | Schema confirmation, `--mode plan` behaviour, automatic `/usage` |

The Antigravity column is the **legacy** transport. Current Antigravity children use ACP v1 (section 4.4); the legacy
worker remains for reading old sessions and for its fixtures.

Shared parts: `WorkerPolicy` (authority stated in every result), `external.started/tool/finished` events,
`snapshot_workspace` (Nexus measures changed files itself), and provider-generic UI rows in
`nexus/ui_support/external_worker.py`.

### Lessons to carry over

Each of these was a real bug or a verified behaviour. Each needs a test in the port.

- Claude Code: Ink queries the cursor position, so the PTY must answer `ESC[6n` or the CLI stalls.
- Claude Code: Ink puts styling between words, so prompt detection removes whitespace and borders first, and removes
  echoes of the prompt and of Claude's own replies. Otherwise prose that quotes a prompt is taken for one.
- Claude Code: `Stop` can land just before exit, and the transcript can flush after `Stop`. Re-check once after exit, and
  reconcile for 2 s.
- Claude Code: a resumed session can report a new session id. The first `SessionStart` pins the id for the later hooks.
- Claude Code: `AskUserQuestion` stalled forever because nothing answered it, so it stays disallowed.
- Claude Code: the derived transcript path is a best guess. The path from the hook always wins.
- `files_changed` must include commits made during the run (`HEAD` diff) and disclose that ignored files were not checked.
- `redact_secrets` masked any 40+ character path as a token. Do not port that behaviour.
- agy: some releases print `--help` on stderr, so preflight reads both streams.
- agy: stdin must stay open until the `result` event, because EOF ends the session.
- agy: `SUCCESS` with an empty answer usually means a soft-denied tool, so report `needs_attention`, not `completed`.
- agy: a denied tool ends with `state: ERROR`. Close the row on `DONE`, `ERROR` and `CANCELED`.
- agy: `result.denied_actions` and the stderr `--mode auto is not available` warning must both reach the user.
- agy: `--dangerously-skip-permissions` is a user-level opt-in only. Project config must not be able to set it.
- Both: no wall-clock deadline (user decision 2026-10-17). Do not port `timeout_seconds`.
- Both: read-only is prompt guidance, not enforcement (user decision 2026-10-17). Do not port the read-only refusals.

## 3. What has changed since nexus01

**On `main`:**

- A subagent is a full child session (`parent_id`, `parent_call_id`), not a run inside a tool call. `_ChildRuntime`,
  `SubagentRunner`, `SubagentOutcome`, `ConversationView`, `nexus/view/reduce.py` and the host protocol do not exist here.
  The worker modules port cleanly. **The runtime and UI glue must be rewritten, not copied.**
- A `claude` provider exists (`nexus/llmhub/claude.py`), but there Nexus owns the loop and runs its own tools through the
  Agent SDK. It stays as it is. It is a different thing from this worker.
- One timeline is now a project rule, stricter than anything nexus01 had to meet (section 4.3).
- The porting ledger records `agy` and the Claude agent as dropped vendors. This plan reverses part of that row.

**In the CLIs**, from local probes on 2026-11-04 (Claude Code 2.1.318, agy 1.4.12):

| Change | What it allows | State |
| --- | --- | --- |
| Claude transcripts now contain `api-request-shape` (`system`, `tools`), `api-request-blob` (content-addressed messages) and `api-request` (`params`, `tail`, `requestDigest`) entries | Possibly the model's real context, which the timeline rule needs | Shapes seen in one recent transcript. **Undocumented; meaning not verified** |
| Assistant entries carry `apiBlockIndex`, `requestId`, `message.id` and full `usage` | Regroup one API response into one step, and report real token usage. nexus01 called usage "unknown" | Fields seen; grouping not verified |
| Claude subagents write to `<session>/subagents/agent-*.jsonl` with a `.meta.json` | Show Claude's own subagents. nexus01 ignored sidechains | Files seen; format not read |
| `claude --append-system-prompt`, `--system-prompt`, `--name`, `--system-prompt-snapshot` | A real channel for the Nexus agent prompt, instead of nexus01's labelled prompt prefix | In `--help`; not tried |
| `agy --conversation`, `--continue` | agy follow-ups | In `--help`; not tried |
| `agy --settings` (session-only JSON) | Scoped allow rules without editing the user's settings | In `--help`; not tried |
| `agy --print-timeout` default `0s`, "waits until the turn completes" | Remove nexus01's one-year timeout workaround | In `--help`; not tried |
| `agy --input-format stream-json` "runs a turn for each" line | A long-lived process with several turns | In `--help`; not tried |

On 2026-11-23 a signed-in **agy 1.5.14** ACP probe ran two small turns in a scratch workspace. `session/new` and
`session/resume` returned the session id and advertised `default`/`accept-edits`/`auto`/`plan`;
`session/set_model` and `session/set_mode` succeeded; a real `write_file` request offered `allow_once`/`reject_once`
and a deny failed the tool; `session/resume` worked in a fresh process. The advertised `configOptions.model` values were
base ids while `session/set_model` accepted `gemini-3.8-flash-low`; the worker uses the exact id from
`models.availableModels` for that path and confirms it through the following `configOptions` update. This was not a
Nexus run.

`agy models` lists `gemini-3.8-flash-{high,medium,low}`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking` and
`gpt-oss-120b-medium`, preceded by a `Fetching available models...` line that the parser must skip.

## 4. Design

### 4.1 Shape: an agent-shaped backend, chosen at `_agent()`

`Runtime._run` (`nexus/runtime.py:2623`) consumes `agent.turn(user_text)`, an async generator of loop events, and does all
the durable bookkeeping from those events: turns, requests, attempts, tool lifecycle, usage, messages, input delivery and
bus publication. `Runtime._agent` (`:2014`) builds that agent.

So the worker becomes a second implementation of that surface:

```text
Runtime._run                    unchanged in the common path
  └─ self._agent(session)       provider dialect == "external-cli" ?
       ├─ loop.Agent            Nexus's loop: provider.stream() + Nexus tools
       └─ workers.ExternalAgent the CLI's loop: yields the same LoopEvents
            ├─ ClaudeCodeWorker  PTY + hooks + transcript reader
            └─ AntigravityWorker pipes + NDJSON
```

Why this shape:

- It keeps nexus01's rule that a worker is **not** repeated `provider.stream()` calls. The CLI owns the loop.
- Durable records, SSE events, turn summaries, the spend ledger, the inspector, export, `via Manager`, `wait: false`
  notifications and child reports all keep working, because they read the same records.
- There is no `external.*` parallel world, and so no second reducer in each client. nexus01 needed those events because
  its view layer was built differently.

`ExternalAgent` must provide what `runtime.py` touches today: `messages`, `model`, `provider`, `reasoning_effort`,
`render`, `request`, `turn`, `turn_id`. Writing it will show which of these need a real implementation and which need a
guard. `request()` backs `/context` and needs its own answer (section 4.3).

### 4.2 Event mapping

Each inbound source line becomes a `WireEvent` first, so the inspector sees everything and wire events are never
dropped. The normalized events derived from it follow.

| Claude transcript | agy ACP | Nexus events |
| --- | --- | --- |
| (turn start) | `session/new` or `session/resume` returns the session id | `MessageAdded(user)`, then `session.worker` (new, section 4.6) |
| first entry of a new `message.id` | first `tool_call`/text update, or the prompt start | `StepStart`, which opens a `turn_request` |
| `thinking` block | `agent_thought_chunk` | wire-only; **not** a reasoning row yet |
| `text` block | `agent_message_chunk` (`type: text`) | `TextDelta`, `PartDone(Text)` |
| `tool_use` block | `tool_call`, then `tool_call_update` | `ToolCallStarted`, `ToolArgsDelta`, `PartDone(ToolCall)` |
| `message.usage` | prompt-result token fields; `usage_update` is used/size | `Usage` from the prompt result, normalized per turn; a result without token fields stays unknown |
| last block of the response | `session/prompt` result `stopReason == "end_turn"` | `MessageAdded(assistant)` |
| `tool_result` block | tool status `completed`/`failed` | `ToolEnd`; `failed` is an error result |
| `Stop` hook | `stopReason == "end_turn"` with non-empty text | `TurnEnd("end_turn", steps)` |
| `StopFailure`, unexpected exit | other stop reasons, ACP error, EOF | `ProviderError`, so the turn fails through the existing path with its partial output kept |
| prompt detected, soft denial | `stopReason == "refusal"`, empty end turn, denied permission | `needs_attention` (section 4.5) |
| (permission prompt) | `session/request_permission` | existing approval card; the tool row joins by `agy:<toolCallId>` |

Every part has `origin` set to `claude-code` or `agy`, and `raw` set to the source entry, as the ledger requires. An ACP
`session/update` for another session, a malformed update, an unknown update kind, thought chunks and context-window
usage updates are wire-only and noted in the worker's notes. Nexus's own outbound ACP requests are not wire events.

Things to get right:

- **Parallel calls.** Results can arrive across several transcript entries, or in another order. Publish each `ToolEnd` as
  it arrives, and hold `MessageAdded(tool)` until the step is complete. On a missing result, close the call the way
  `_close_dangling_calls` does.
- **No token streaming from Claude Code.** The transcript is written per block, so text appears in blocks. This is a
  visible limit, recorded in the docs; do not parse the terminal to fake deltas.
- **Claude's own tool names.** `Read`, `Edit`, `Bash` and the rest are not Nexus's `read`, `edit`, `bash`, and both clients
  branch on the Nexus names (`nexus-tui/src/timeline.rs:2663`, `:2996`, `nexus-nsh/src/ui/timeline.tsx:167`, `:417`). Keep
  the real name and arguments in the record, because that is what the model saw. Add one display mapping in Core, sent with
  the tool event, so both clients get the same card without duplicating a table. Unknown tools use the generic card.

### 4.3 One timeline: what the model received

The AGENTS.md rule says everything the model receives is shown in order. A CLI adds its own system prompt, tool
definitions, memory files and reminders, so this is the hardest requirement in the plan. Honesty about it matters more
than coverage.

Three levels, tested in this order:

1. **Claude Code, from the transcript.** If the spike shows `api-request-shape`, `api-request-blob` and `api-request`
   rebuild the real request, then the timeline's starting context shows the real system prompt and tools, and the
   inspector shows each request. `attachment` entries (`rendered`, `renderedRole`) show injected context in order. This
   is what would make the rule hold, and the format is undocumented, so parse it defensively and keep a version-tagged
   fixture for each tested CLI.
2. **agy, or a Claude version without those entries.** Show a starting-context card saying what Nexus knows (CLI, version,
   model, cwd, tools from `init`, mode) and stating that **the CLI adds context that Nexus cannot see**. Never show an
   empty context as if it were complete.
3. **Never a proxy.** A local capture proxy would see everything, but it breaks section 1. A one-off local capture for a
   measurement, as was done for `docs/llm-providers.md`, stays a manual check.

`GET …/context` and `/context` return the rebuilt request at level 1, and the same disclosure at level 2. `sdk_adds` in
`docs/llm-providers.md` is the precedent for listing hidden additions.

This is an **exception to the timeline rule at level 2**, accepted by the user for ZMX subagents only (D2). Level 1 is
still worth trying if M0 shows it is cheap, but it no longer blocks anything: if it is costly, Claude Code uses level 2 too.
The Antigravity ACP approval card does not narrow this exception: it shows one permission request the CLI chose to ask,
not the CLI's system prompt, tools, memory or reminders.

### 4.4 Transport

| Backend | Transport | Why |
| --- | --- | --- |
| Claude Code | Interactive PTY, as in nexus01 | The contract nexus01 chose: real interactive use, not `-p` or the SDK. The `claude` provider already covers the SDK |
| agy | ACP v1 over stdio (`agy acp`); one process per turn | Gives structured updates and a real permission request Nexus can put on its approval card. The old headless stream-json worker is legacy-only |

Decision D3 asks whether to measure `claude -p --output-format stream-json` too. It would give structured events and
perhaps token deltas, but it is a different product path, and its subscription use may differ.

**Process lifetime:** one CLI process per Nexus turn. Claude Code resumes with `--resume`; Antigravity resumes with
`session/resume` when the agent advertises it and the stored policy fingerprint matches. It survives daemon restarts
better than a long-lived process. Long-lived processes are a later optimisation.

**Prompts:** Claude's argv cap stays at 64 KiB and refuses rather than truncates; the ACP prompt is bounded at 64 KiB and
is one text block. A prompt with images is refused with a clear error in the first release, as the `claude` provider
does.

**ACP setup is fail-closed (offline-tested; protocol-probed on 1.5.14):**

- `initialize` must negotiate protocol v1. `session/new` sends `cwd` and `mcpServers: []`; `session/resume` is used only
  for an `acp-v1` row with a session id, a matching policy fingerprint and an advertised resume capability.
- A requested model must be exact. If the id is an advertised `model` config value, the worker sets that option and
  requires its current value to match. Otherwise, if the id is in `models.availableModels`, it uses the
  `session/set_model` extension and waits for the following `config_option_update` to confirm the base model and any
  per-effort suffix; the live 1.5.14 server advertises base ids in the config option and exact per-effort ids in
  `models.availableModels`. An older agent with no model surface may accept `session/set_model` and confirm the exact id
  in its result. More than one model option, an unadvertised id or a different reported value fails.
- Session modes must be advertised and include `default`; `session/set_mode` must not report another current mode. The
  1.5.14 probe advertised `default`, `accept-edits`, `auto` and `plan`; `default` is described as asking before edits and
  commands. New children always use `default` (section 4.5).
- A resolved effort needs exactly one advertised `thought_level` option that allows it; a different reported value fails.
- `session/prompt` is sent only after the above. Auth errors become `needs_attention` with `agy acp --login`; other setup
  failures fail the turn before the model sees the assignment.

The policy fingerprint covers transport, model, mode, effort, resolved directory, the empty MCP server list and the
absent system prompt. ACP has no transcript: recovery records the session id and transport, and can resume only through
the advertised capability.

**Live Nexus E2E (2026-11-23):** a scratch Core ran an `agy/gemini-3.8-flash-low` child on agy 1.5.14. A real ACP
`write_file` request reached the approval API, a deny left the file absent and the turn `needs_attention`, and a
follow-up resumed the same ACP session in a fresh process and returned `resumed`. Usage was recorded on both turns.
Evidence: `/tmp/nexus-zmx-live.anqGQS/results.json`. No visual client or daemon restart was checked.

### 4.5 Permissions, settings and attention

Nexus does not run these tools and cannot enforce its own permission rules inside the CLI. `permissions.py`, an agent's
`permission:` rules, `disabled_tools` and path confinement do not reach inside it. The child's ZMX label states this.
Antigravity ACP is the narrow exception: the CLI's own `session/request_permission` goes to a Nexus approval card, and
Nexus answers only that per-call allow/deny. Claude Code still answers no prompt.

**Align where cheap, otherwise let the CLI decide:**

| Nexus setting | Claude Code | agy |
| --- | --- | --- |
| Agent prompt | `--append-system-prompt` if M0 confirms it, otherwise a labelled prompt prefix as in nexus01 | Labelled prompt prefix on the first ACP prompt |
| Model and effort | `--model`, `--effort` | ACP model config option or `session/set_model`, and the `thought_level` config option; no CLI flags |
| Working directory | `cwd` | ACP `cwd`; the policy fingerprint includes it |
| Agent `tools:` | `--tools`, if the names map simply; otherwise the CLI's own set, disclosed | The CLI's own set, disclosed |
| MCP servers | Pass the session's servers with `--mcp-config`: Nexus entries already use Claude Code's keys (`nexus/mcp/config.py`). If M0 shows otherwise, the CLI's own servers | The CLI's own servers; ACP sends `mcpServers: []` |
| Skills, memory, plugins, hooks | The CLI's own | The CLI's own |
| CLI settings | Inherited; `setting_sources` can narrow them | Inherited |
| Nexus `permission:` rules, `disabled_tools`, `steps` | Not applied, disclosed | Not applied, disclosed |

Nothing here is a reason to refuse a child. nexus01 refused many combinations; here a setting that does not carry over
is shown as "not applied" and the child runs anyway. The exception is a request the worker cannot run at all, such as a
prompt with images. An alignment that turns out to be costly in M0 falls back to the CLI's own behaviour without another
decision.

**Permissions:**

| Nexus session permission | Claude Code | agy (ACP) |
| --- | --- | --- |
| `ask` | `dontAsk`: what would prompt is denied | Not mapped; ACP mode `default` |
| `allow` (the default for new sessions) | `auto`, the CLI's classifier, disclosed | Not mapped; ACP mode `default` |
| Bypass | Never | Never: `--dangerously-skip-permissions` exists in `agy acp --help` but Nexus does not pass it |

- **Claude Code:** **never** `bypassPermissions`, and never a keystroke that answers a prompt. Trust, sign-in and
  permission prompts end the child as `needs_attention` with the exact command.
- **Antigravity ACP approvals:** the CLI asks, and the request becomes an ordinary Nexus approval with the same card,
  shortcuts, mobile card and `via Manager` path. Only per-call `allow_once`/`reject_once` options are selected; Nexus
  never selects `allow_always`, `reject_always` or another persistent option and never edits the CLI's settings. If no
  per-call allow option exists, no card is shown: the worker selects the per-call reject if present, otherwise cancels.
  If approval fails, is interrupted or is missing, the request is denied/cancelled, never allowed.
- **Session grants are exact.** "Allow for session" stores a runtime grant for the exact tool name and JSON argument
  payload; another payload, even one string different, asks again. The grant is per-session, not persisted, lost on
  daemon restart and cleared with session grants. A deny grants nothing. This deliberately means a `read`/`grep`-style
  Nexus pre-approval or a `tools.permission` rule cannot auto-answer an ACP request.
- **The card shows a claim, not a guarantee.** Its tool name, title and arguments come from the ACP payload and are
  untrusted, bounded and sanitized for display. Nexus sends only the option id back; it does not rewrite or execute the
  claimed arguments. A CLI can mislabel or skip the request entirely, so this is a gate, not a sandbox.
- **`needs_attention` is its own end state.** It goes through the existing `escalate` path (`session.escalated`), so the
  parent's report carries it, and the child's own timeline shows the instruction in full. A denied ACP request leaves
  the turn failed and the worker `needs_attention`.

### 4.6 Nexus features and how each behaves

| Feature | External session behaviour |
| --- | --- |
| Interrupt | ACP sends `session/cancel` when a session exists, then kills the owned process group (`start_new_session`; TERM, then KILL). Pending approvals are cancelled. Partial output stays |
| Steer while working | Not delivered mid-turn. Converted to a queued input, with a visible notice. agy's per-line turns are a possible later change |
| Queued input | Starts the next turn with Claude `--resume` or ACP `session/resume` |
| Continue (`NO_USER_MESSAGE`) | 409 with a reason: a CLI cannot resume with no message |
| Switch model | Refused on a ZMX child; start a new child. ACP also refuses a resume whose policy fingerprint differs |
| Switch backend or provider | Refused in the first release. Nexus's stored history cannot enter a CLI's session, and the reverse carries foreign tool calls |
| Handoff | First release: refused. Auto-handoff is off |
| `/btw`, title generation, one-shots | Never run on an external model. Titles use the configured title models; `/btw` is refused with a reason |
| Change directory | Refused while external: a Claude transcript belongs to its cwd. Decide later |
| Worktree session | Runs with `cwd` = session directory. Claude Code trust prompts appear as `needs_attention` (auto-trust for a Nexus project folder). Antigravity writes no trust state |
| Agent `steps` limit | Not mapped; shown as not enforced |
| `ask`, `skill`, `code`, MCP, `session` tools | Not available: the CLI has its own. A later option is exposing control tools through `--mcp-config` |
| Hooks (`hooks.py`) | Do not fire, since Nexus runs no tool. Documented |
| Kernels and background jobs | None; the CLI manages its own |
| Fallback models | None: a worker failure fails the turn. A child's pool may pick a ZMX model, but no silent switch happens after launch |
| Root sessions | Refused: `_plan_session` and `update_session` reject a ZMX model when `parent` is `None`, with the reason. It never enters the remembered model |
| ZMX switched off later | Existing ZMX children stay readable. New turns and new children are refused, naming the setting |
| Manager | Can read and message these children (`via Manager` works unchanged). Its own model can never be ZMX |
| Spend ledger | Real tokens where reported. Otherwise "unknown", never zero. Cost is not estimated |
| Files changed | Nexus measures them with `snapshot_workspace`, and never takes the model's word |

A new `session.worker` event records the launch facts durably: provider, transport (`acp-v1` or `stream-json-v1`), CLI
version, argv without the prompt, cwd, effective permission mode, settings sources, MCP, CLI session id, transcript
path, policy fingerprint and the authority statement. Replay shows the same thing as live.

### 4.7 Recovery

`nexus/recovery.py` never kills by stored PID, and that stays. For an external turn:

- The `turn`, `turn_request` and `turn_tool` rows reconcile as they do today, but the text changes: a tool here was run by
  the CLI, and it may have **finished or may still be running**. Say so, and do not retry it.
- Store the CLI session id and transport in the durable worker record, so the next prompt can Claude `--resume` or ACP
  `session/resume`.
- On the next daemon start, read the rest of a Claude transcript once to complete the record if the CLI finished after
  the daemon died. Label this as recovered output. ACP has no transcript to read; a resumed ACP turn must not invent
  the missing output.
- Orphan processes: record pid + start time in `$NEXUS10_HOME/workers/`, and offer cleanup only when both still match.
  Never kill automatically. nexus01 left this open.

### 4.8 Models, config and sign-in

- **The switch:** off by default, one setting in Settings and in `config.py` (placement decided in M1 after reading
  `docs/config.md`; for example `[agents] zmx = true`). While off: the providers are not listed, no CLI is probed, and a
  `claude-code/…` or `agy/…` ref is refused with a reason naming the setting. A project file cannot turn it on.
- **Provider ids:** `claude-code` and `agy`, with dialect `external-cli`. Refs are `claude-code/claude-haiku-5-5` and
  `agy/gemini-3.8-flash-high`; `split_ref` already handles them. `claude` stays the SDK provider.
- **Routing:** a child gets a ZMX model only explicitly: an `[agents.models]` pool, an agent file's `model:`, or
  `POST /api/session {parent, model}`. A pool entry is skipped, visibly, while the switch is off or the CLI is missing.
- **Catalog:** `claude-code` reuses models.dev `anthropic`. `agy` reads `agy models` with a bounded probe, cached, no model
  turn. Exact ids only, no aliases and no silent substitution.
- **Connected:** `claude-code` reuses `auth/claude.py` (`status`, `cli_path`, `child_env`). agy has no status command, so a
  found CLI shows as **"installed, sign-in not verified"**, and a failed ACP turn says to run `agy acp --login`. ACP
  `initialize` advertises auth methods but no signed-in status.
- **Sign-in:** `claude auth login` as the `claude` provider does. For agy the Settings row says to run
  `agy acp --login` in a terminal (the flag is in `agy acp --help`; the 1.5.14 probe was signed in already), with
  Re-check. Nexus signs nobody in to Antigravity.
- **Executable:** `$NEXUS10_AGY_CLI`, then `PATH`, then usual locations, as `cli_path()` does. `nexus doctor` uses
  `AntigravityAcpWorker.preflight()` and requires agy **1.5.0** or newer (the first `agy acp`); older CLIs fail with an
  upgrade instruction, and a successful row names `ACP v1 stdio (agy acp)` and `sign-in not verified`.
- **Usage panel:** `provider_usage.py` already runs `claude -p /usage`; add `agy -p /usage`, ported with its parser and
  bounded. ACP does not replace that command; ACP turn usage comes from the prompt result, and an absent result usage
  stays unknown.
- **Config (`config.py`):** port only the worker keys that still apply: `permission_mode`, `tools`, `allowed_tools`,
  `disallowed_tools`, `setting_sources`, `mcp_config`, `restricted`, `dangerously_skip_permissions` (user-level only).
  Unknown keys are errors, as elsewhere in Nexus. Exact placement is decided in M1 after reading `docs/config.md`.

### 4.9 UI, in both clients in the same change

Follow `.agents/skills/nexus-ui-parity/SKILL.md`. Any command or setting change goes through `interface/manifest.toml`.

- **The ZMX label, the main requirement.** A child in ZMX mode says so everywhere it appears, with the same wording in both
  clients: a `ZMX · Claude Code` or `ZMX · Antigravity` mark on the subagent strip, the parent's child card and the child's
  tab, and a card at the top of the child's timeline stating model, cwd, permission mode, what was and was not applied from
  section 4.5, and that the CLI ran its own tools. Antigravity adds that its permission requests go to Nexus approval
  while Nexus permission rules still do not apply; Claude Code adds that it does not ask Nexus at all.
- **Settings:** the off-by-default ZMX switch with a one-line explanation of what it gives up.
- **Model choices:** ZMX models appear only in subagent model pools, and only when switched on, labelled `ZMX`. They never
  appear in the root model picker.
- **Approvals:** Antigravity ACP permission requests reuse the existing approval cards in both clients and mobile, with
  no separate ZMX card layout. The card labels the CLI request (`Antigravity · …`) and joins the ACP tool row.
- **Tool cards:** the normal cards through the display mapping, with a quiet provenance mark, for example
  `Claude Code (own tools)` or `Antigravity (own tools)`.
- **`needs_attention`:** its own state with the full manual instruction, in the session and on the parent's child card.
- **Turn footer:** real tokens, or "usage unknown". Never 0. Antigravity ACP maps the prompt result's token fields and
  shows usage unknown when the result has none.
- **Composer:** while busy, sending says it will queue, since steering is unavailable.
- **Refused actions:** on a ZMX child, switching, handoff and `/btw` are disabled with the reason, not hidden.
- **Settings › Providers:** the two rows and their install, sign-in and re-check states.
- **Inspector (`nexus/inspector`):** inbound wire events and raw entries; outbound ACP requests and rebuilt requests
  remain open.

The file explorer, editor and Git rules are unaffected. Verify with a mock scenario in `nexus/mock/scenarios/` and
screenshots from TUI (xterm.js) and NSH web, per the skill. This includes iOS only where the shared timeline already
reaches it. The 2026-11-23 ACP checks verified the TUI and NSH approval/prompt cards for a short `read_file` request and
a long `execute` command after both clients began wrapping a value to three rows (`reports/acp-approval-parity-2026-11-23/`),
and the denial card's full `Needs attention` error now wraps under the title in both
(`reports/zmx-attention-parity-2026-11-23/`). Mobile is not checked.

### 4.10 Security

- Child environment comes from a fixed allowlist (`auth/claude.py child_env`); no API keys or `PYTHONPATH`.
- State in `$NEXUS10_HOME/workers/`: owned, `0700`, not a symlink, refused rather than repaired, as nexus01 did.
- The hook script runs with `python -I`, imports nothing from Nexus, receives no credentials, validates the session id,
  bounds its input and writes atomically.
- Terminal text is untrusted: ANSI-stripped, bounded, redacted, and diagnostic only. It is never treated as a model
  response and never drives approval.
- **Prompt detection never approves anything.** A model can print text that looks like a prompt; the worst result must be
  a false `needs_attention`.
- **ACP permission requests are untrusted input and only a gate.** The payload's tool name, title and arguments are
  sanitized for display; Nexus returns only the selected per-call option. The CLI can pre-approve, mislabel or skip the
  request, so this is not a sandbox. Persistent ACP options are never selected, and Nexus advertises no filesystem or
  terminal client capabilities and sends no MCP servers.
- **Nexus permission rules do not decide ACP requests.** In particular, a `read`/`grep` auto-approval or a
  `tools.permission` rule cannot answer one. "Allow for session" is keyed to the exact tool name and argument payload
  and is lost on restart.
- Project config cannot widen authority (`dangerously_skip_permissions`, and also `mcp_config` unless reviewed).
- Transcript and stream content is untrusted data, and is shown as data. Bounds as in nexus01, and every clip is announced
  in the timeline.
- Paths in the transcript are not trusted for reading: the transcript path must resolve under the CLI's config directory.

## 5. Code layout

```text
nexus-core/nexus/workers/
  __init__.py       package docstring only; no discovery or credential reads on import
  agent.py          ExternalAgent: LoopEvent surface, event mapping (section 4.2)
  acp.py            ACP v1 stdio JSON-RPC client: framing, correlation, inbound requests, process bounds
  base.py           WorkerPolicy, WorkerResult, clip, bounds, terminal_text, snapshot_workspace, changed_paths, process-group stop
  claude_code.py    PTY, HOOK script, preflight, argv, supervise, startup/permission detection
  claude_transcript.py  TranscriptReader, entry parsing, API-response grouping, request rebuild
  antigravity.py    AntigravityAcpWorker (ACP setup, updates, permission callback); AntigravityWorker is the legacy
                    stream-json transport kept for old sessions and fixtures
nexus-core/nexus/llmhub/
  providers.py      new dialect and two provider rows
  auth/agy.py       probes only (from antigravity_cli.py)
  catalog.py        agy dynamic models
nexus-core/tests/
  fakes/            fake `claude` and `agy` executables
  fixtures/workers/ recorded, sanitized, version-tagged transcripts and streams; ACP adds agy-1.5.14-acp/live.json
```

| nexus01 | Action |
| --- | --- |
| `agents/claude_code_worker.py` | Port. Split transcript reading out; drop `timeout_seconds`, read-only refusals and `ConfigError` paths that no longer apply |
| `agents/antigravity_worker.py` | Port. Drop the one-year timeout if the spike confirms `0s` |
| `model/providers/antigravity_cli.py` | Port to `auth/agy.py` |
| `tests/claude_code_fakes.py`, `test_claude_code_worker.py`, `test_antigravity_worker.py`, `_cli`, `_usage`, `_skip_permissions` | Port, then update to new versions and the new event mapping |
| `runtime.py` `_plan_external`, `_plan_antigravity`, `_run_external` | **Rewrite** as `ExternalAgent` and its guards; read for the rules they enforce |
| `test_claude_code_routing.py`, `test_antigravity_runtime.py` | Rewrite for child sessions; keep their cases as a checklist |
| `events.py` `external.*`, `view/reduce.py`, `ui_support/external_worker.py` | **Do not port.** Reuse the wording of the rows and status strings |
| `rust/tui` `external` block kind, `test_ratatui_*external*` | **Do not port.** Build on the current timeline in both clients |
| `claude_agent_auth.py`, `claude_code_main_enabled`, `claude_code_subagent_backend` | **Do not port.** `claude` vs `claude-code` provider refs replace the switch |
| `usage_gate.py` Antigravity branches, price tiers | Do not port; `[agents.models]` pools cover routing |
| `docs/antigravity-troubleshooting.md` | Port and update to 1.4.12 |

## 6. Milestones

Each milestone lands whole, with its docs. "Real" checks use the user's account and **need the user's approval first**,
with the cheapest model. All tests use a scratch `$NEXUS10_HOME`. Never run `nexus migrate` against the real home.

### M0. Spikes (no product code)

Answer these with real but small runs, and record each answer in this file with the CLI version. **Status 2026-11-16
(offline sync; no live run in this pass):** `[x]` has an answer recorded from a fixture or from the 11-12 live runs
(`reports/zmx-2026-11-12/live/`); `[~]` is partly answered; `[ ]` still needs a real run, which needs the user's approval
(listed in `reports/pre-release-2026-11/user-checklist.md`).

- [~] Claude 2.1.322: can `api-request-*` entries rebuild a real request? **Seen, not compared.** The recorded transcript
  (`nexus-core/tests/fixtures/workers/claude-2.1.322/transcript.jsonl`) holds `api-request-shape`, `api-request-blob` and
  `api-request` entries (1, 5 and 3 in one run). Nexus shows them as wire events and keeps the context declared incomplete
  (`test_zmx_reliability.py`). The comparison with a local capture of the real request is open.
- [~] Claude: does one `message.id` always mean one API response? How do parallel results appear? Three `assistant`
  entries in the fixture; the 11-12 live run showed one tool call and result. Parallel results are **not** recorded.
- [~] Claude: `--session-id`, `--resume --model`, `--effort` in a PTY. **Worked in the 11-12 live runs** (Claude Code
  2.1.322: `--session-id` for the first turn, `--resume` after it, `--effort` passed; the worker requires these flags and
  `nexus doctor` checks them). `--append-system-prompt` is **not confirmed**: Nexus labels the prompt in the first
  message, as the fallback in section 4 says.
- [ ] Claude: `StopFailure` payload; permission-prompt wording under `dontAsk` and `auto`. Not recorded.
- [ ] Claude: does `--mcp-config` accept Nexus's server entries as they are? Not run.
- [ ] Claude: do `claude -p` and interactive use count the same against the subscription? (Input to D3.) Cannot be
  answered offline; the vendor-terms item in 06 is related.
- [x] Claude: the format of `subagents/agent-*.jsonl`, and how it links to the parent call: recorded in
  `claude-2.1.322/subagents/` (`agent-<id>.jsonl` plus `agent-<id>.meta.json` with the parent's `toolUseId`); parsed by
  `SubagentReader` (`306670ac`).
- [ ] Claude: which hidden requests does interactive use make (`ai-title`), and does
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` still apply? The fixture shows `atis-latch` entries; not measured.
- [x] agy 1.5.0: a real stream-json turn, with `init`, `step_update`, `result`, reasoning, usage, tool steps:
  `agy-1.5.0/turn1.ndjson`, `turn2.ndjson`, `usage.json` (the live `/usage` command). Subagents inside agy: not recorded.
- [x] agy: `--conversation` follow-up (`turn2.ndjson`), a denial (`denial.ndjson`), `--mode plan` ends as a plan,
  `--print-timeout 0s`; the 11-12 live run on agy 1.5.6 found it needs 0.6 to 2.2 s to exit after stdin closes (the exit
  grace is now 10 s). `--settings` allow rule: not separately recorded.
- [ ] agy: does `--mode auto` work on this account? What does a soft denial look like now? (`denial.ndjson` is the
  `--mode plan` denial; `auto` is not recorded.)
- [x] agy 1.5.14 ACP: signed-in protocol probe (not through Nexus) confirmed `session/new`, advertised modes
  `default`/`accept-edits`/`auto`/`plan`, `session/set_model`, `session/set_mode`, a real `write_file` permission
  request with allow/deny, and `session/resume` in a fresh process. Two small turns. The advertised model config option
  listed base ids while `session/set_model` accepted `gemini-3.8-flash-low`; see M6.
- [ ] agy ACP through Nexus (parent/child turn, approval cards, UI, restart). Not run.
- [~] Both: what an interrupt leaves behind, and whether `--resume` works after it. Offline tests cover resume after an
  interrupt (`5dee5561`); a real Claude interrupt followed by `--resume`, and a real ACP interrupt followed by
  `session/resume`, were not recorded.

Done when: every box has an answer and a fixture, and section 4 is corrected where the spike disagrees with it.

### M1. Agy as a child session (smallest vertical slice)

Start with agy: it has a structured stream and no PTY, so it tests the mapping without PTY risk.

- [x] **Transport foundation only (11-10):** isolated `workers/base.py` and `workers/antigravity.py`, raw-first parser,
  process supervision and fake-CLI safety/regression tests. No `external.*` durable protocol, runtime imports or UI.
  `--print-timeout 0s` is provisional from the 11-04 help probe, not a verified live turn. Live fixtures remain M0 work.
- [x] Workers' runtime contract, provider row, dynamic catalog (`agy_models`), config keys `[zmx]` (11-11 audit). `auth/agy.py` not added: sign-in stays the CLI's own.
- [x] `ExternalAgent` with the section 4.2 mapping; `_agent()` selects it (`workers/agent.py`).
- [x] Guards from section 4.6 (`test_guards_change_nothing`). Check each refusal against 4.6 once more when M0 lands.
- [~] `session.worker` event and fake `agy` done; `files_changed` not confirmed; lesson-by-lesson test coverage not re-audited.
- [x] The off-by-default switch, and the refusal of ZMX models for roots, each with tests (`test_off_by_default_and_roots_refused`).
- [~] Routing: `claude/…` children map to `claude-code/…` when enabled (`registry.child_ref`); explicit `agy/…` refs. Real routing through `orchestrate` unverified.
- [x] Docs (11-12): `external-workers.md` rewritten; `llm-providers.md`, `auth.md`, `models.md`, `config.md`, `daemon.md`, `api.md`, porting ledger, AGENTS.md (D2). `stream-events.md` unchanged: ZMX emits ordinary loop events.

Done when: an offline suite passes, and (real, approved) `orchestrate` launches an agy child that runs a tool in a scratch
directory, its parent gets the answer, and the session reads correctly in `GET /message` and the inspector after a restart.

### M2. Claude Code as a child session

- [x] `workers/claude_code.py`, `claude_transcript.py`, hook script, PTY, preflight, provider row (offline tests in `test_zmx_claude.py`).
- [x] Display mapping for tool names (Core, `agent.display_tool` → `raw.display`); fake `claude`. Lesson-by-lesson coverage not re-audited.
- [x] Follow-up through `message_session` → `--resume`, with the policy fingerprint check (`test_resume_requires_exact_policy`).
- [x] Claude's own subagents: shown inside the calling tool's card, read-only (`306670ac`, `5dee5561`). Trees stay one level deep in Nexus.

Done when: the same real check passes on Haiku, and Sonnet and Opus ids are checked rather than assumed.

### M3. Both clients and the timeline rule

- [~] Section 4.9 in the TUI and NSH: Settings control, `ZMX · <vendor>` label and root-picker exclusion done. The
      `zmx-*` scenarios include normal runs, parallel tools, denial, `needs_attention`, interrupt and recovery; the ACP
      denial scenario asserts the full attention text in both clients.
- [~] Section 4.3: disclosure starting context (`context incomplete`, `CLI inherited`, `Not applied`) in both clients; `/context` and level 1 open.
- [~] Inspector shows inbound raw entries (wire events). Outbound ACP requests are not recorded, and rebuilt requests
  wait on the M0 api-request question.
- [ ] Ledger row for any client difference, and only with the user's approval.

Done when: TUI and NSH web screenshots of each scenario agree, and `check_manifest.py` and `check_theme.py` are clean.
**11-12:** met offline: `reports/zmx-2026-11-12/` pairs agree; both checks clean.

### M4. Reliability

- [x] Section 4.7: recovery text, reading the rest of a transcript, CLI session id reuse, orphan record and offer (`306670ac`, `5cf811c8`).
- [x] Race tests: cancel during `Stop`, exit before flush, duplicate hook, crash between entries (`test_zmx_reliability.py`).
- [x] Usage panel for agy (`agy --print /usage`); turn summaries and the spend ledger use reported usage, unknown when none.
- [x] `nexus doctor` reports CLI, version and transport. Its Antigravity row uses the ACP preflight and requires 1.5.0 or
  newer; Claude Code keeps its flag check. Unknown shapes stay wire events and are disclosed.
- [x] `docs/daemon.md#crash-recovery`, `docs/external-workers.md`, `docs/README.md` and AGENTS.md's table (11-12).

### M6. Antigravity ACP and approvals (2026-11-23)

Migrates Antigravity only; Claude Code stays on PTY. The ACP protocol probe, the Nexus API E2E and the TUI/NSH approval
cards are live evidence; mobile and a daemon restart are not.

- [x] `workers/acp.py` (ACP v1 stdio JSON-RPC), `AntigravityAcpWorker`, ACP selection in `agent.py`, and
  `Runtime._approve_external`. Implemented, offline-tested and exercised by the Nexus E2E through the approval API.
- [x] Permission requests use the existing approval cards. Only per-call allow/deny is returned; persistent options are
  never selected; a missing/failed approval denies or cancels. "Allow for session" is keyed to the exact tool name and
  argument payload and is lost on restart. A denied request fails the turn and leaves the worker `needs_attention`.
- [x] Legacy stream-json and unmarked Antigravity histories are readable; a prompt is refused before a new message and
  the user is told to start a new child.
- [x] ACP protocol probe on 1.5.14 (signed in, scratch workspace): `session/new`, modes, `session/set_model`,
  `session/set_mode`, a real permission request and deny, and `session/resume` in a fresh process. No Nexus UI or
  approval card was involved.
- [x] Nexus E2E (2026-11-23): scratch Core, `agy/gemini-3.8-flash-low` child, real `write_file` approval surfaced and
  denied, file absent, `needs_attention` turn, same ACP session resumed in a fresh process and returned `resumed`, and
  usage recorded. Worker row showed `transport: acp-v1`, mode `default`, effort `low` and the same session id. Not
  checked there: allow/allow-for-session live behavior, interrupt and a daemon restart. The visual check is separate
  below.
- [x] Model selection: 1.5.14 advertises `configOptions.model` values as base ids and exact per-effort ids in
  `models.availableModels`; the worker uses `session/set_model` for the exact id and confirms it through the following
  `configOptions` update. The Nexus E2E used `gemini-3.8-flash-low`.
- [x] ACP usage mapping: prompt-result token fields are normalized per turn; `usage_update` stays used/size and
  wire-only. Both E2E turns recorded usage.
- [x] `nexus doctor` ACP preflight. It uses `AntigravityAcpWorker.preflight()` and requires agy 1.5.0 or newer, reporting
  `ACP v1 stdio (agy acp)` and `sign-in not verified`.
- [x] Sanitized ACP fixture (`tests/fixtures/workers/agy-1.5.14-acp/live.json`).
- [x] TUI/NSH approval/prompt-card parity: short `read_file` and long `execute` cards match after both clients began
  wrapping a parameter value to three rows; paired captures in `reports/acp-approval-parity-2026-11-23/`. Mobile is not
  checked.
- [x] Denial and escalation wording: ACP says `Denied through Nexus`; legacy stream-json and Claude keep `Denied by
  the CLI`, and the parent escalation says Nexus denied the ACP request (`interface/scenarios/zmx-acp-denial.json`).
- [x] Denial-card attention detail parity: both clients wrap the full `Needs attention` error under the title; paired
  captures in `reports/zmx-attention-parity-2026-11-23/`.
- [x] Scenario verification: the shared `zmx-acp-denial` scenario asserts the full text in both clients; TUI
  `cargo test --test scenarios` passed 11 and the targeted NSH ACP-denial test passed. The full NSH scenario file still
  has the known `Connection reset by peer` flake in unrelated tests.
- [ ] Mobile approval card check.
- [ ] Daemon restart with an ACP child: both E2E turns ran in one daemon process.

Done when: the mobile check passes, an approval is answered by a real card, the exact grant behavior is observed, a
daemon restart resumes the child, and the docs reflect the result.

### M7. Later, not in the first release

- Answering other CLI prompts inside Nexus (Claude trust/sign-in, non-permission ACP requests). ACP permission requests
  are handled now; `needs_attention` covers the rest.
- A live terminal view or human handoff into the PTY (nexus01 deferred this too). Needs a terminal surface in both clients.
- Long-lived processes for steering. Nexus control tools through a local MCP server. Handoff between backends.
- Windows: `pty` is POSIX only. Mark external providers unavailable there; do not fail at turn time. See PLAN.md 1.1.

## 7. Tests

| Layer | How | Real model? |
| --- | --- | --- |
| Parsers | Recorded, sanitized, version-tagged fixtures from M0; ACP protocol shapes from the 1.5.14 probe (`agy-1.5.14-acp/live.json`) with fake clients | No |
| Workers | Fake executables: argv, cwd, tty state, stdin log, hook firing, delays, crashes, malformed output; ACP framing, notifications, permission callbacks | No |
| `ExternalAgent` + `_run` | Fake CLI through the real runtime; assert stored messages, turn rows, events, approval decisions, session grants and replay equal to live | No |
| Guards | Each refused action in section 4.6 returns its reason and changes nothing | No |
| Recovery | Kill at each boundary, restart, assert no duplicates and no invented results | No |
| Regression | The existing Nexus-loop and `claude` provider suites pass unchanged | No |
| Clients | Mock scenarios, TUI tests, NSH tests, screenshots | No |
| Real | `tests/` marked and skipped by default; run by hand with approval, results recorded here | Yes |

**Live E2E (2026-11-23):** scratch Core + agy 1.5.14; `agy/gemini-3.8-flash-low` child; a real `write_file` request
on the approval API, denied (file absent), turn `needs_attention`; same ACP session resumed in a fresh process and
returned `resumed`; usage recorded. It was not a visual client check and did not restart the daemon. Evidence:
`/tmp/nexus-zmx-live.anqGQS/results.json`.

Baseline before transport work (2026-11-10): `cd nexus-core && uv run pytest -q` — **1310 passed, 1 skipped**.
Final full Core suite: **1387 passed, 1 skipped**; focused workers: **77 passed**; full Core Ruff and
`git diff --check` pass. **2026-11-23 ACP sync:** the six ZMX files (`test_zmx_acp.py`,
`test_zmx_antigravity_acp.py`, `test_zmx_workers.py`, `test_zmx_claude.py`, `test_zmx_runtime.py`,
`test_zmx_reliability.py`) pass **194 tests** offline; full Core, Ruff and diff checks for this change are not yet
recorded. No personal-home migration was run.

## 8. Risks

| Risk | Effect | Answer |
| --- | --- | --- |
| Transcript format is undocumented and changes often | Silent loss or misreading | Version-tagged fixtures, `WireEvent` for every line including unknown ones, unknown-shape warning, tested-version list in docs |
| PTY prompt wording changes | A prompt goes unnoticed and the turn hangs (no deadline) | Hooks are primary; add an idle notice ("no activity for N minutes") that informs and never kills |
| Vendor terms on automated use of a subscription | Account risk for whoever switches it on | Unknown; nexus01 also left this unverified. Off by default, and the Settings text and docs say this plan did not establish that this use is permitted |
| Authority confusion | User believes Nexus permissions apply | The ZMX label (section 4.9), `session.worker`, provenance on each card, docs |
| Hidden context | An accepted exception to the one-timeline rule | Section 4.3, D2; stated on the ZMX card |
| Runaway usage with no deadline or step limit | Spends subscription quota | Interrupt, visible usage, and the idle notice. Consider a Nexus-side soft usage warning later |
| Writes outside the workspace | CLI's own permissions are the only barrier | Disclose; `auto` is the CLI's classifier, not a sandbox |
| ACP schema or agy behaviour changes | Setup fails or updates are misread | ACP v1 negotiation, fail-closed setup, unknown updates stay wire events, 1.5.14 probe, offline fixture and one Nexus E2E; no visual or restart check yet |
| A CLI skips or mislabels `session/request_permission` | The user approves a misleading card or nothing is shown | The card is a gate only when asked; disclose that it is not a sandbox; never select persistent options; show the CLI's claimed payload sanitized |
| Model ids differ between `agy models`, the ACP config option and `session/set_model` | A valid child is refused before the prompt | Fail closed today; reconcile the order and re-check live (M6) |
| ACP usage fields are missing or misunderstood | Tokens show unknown, or a user assumes zero spend | Normalize the prompt result only, show "usage unknown" when absent and never zero, and keep the independent `/usage` panel |

## 9. Decisions

**Decided by the user (2026-11-04):**

| # | Decision | Answer |
| --- | --- | --- |
| D1 | Root sessions too, or subagents only? | **Subagents only** |
| D2 | May a session hold context the timeline cannot show? | **Yes, for ZMX subagents only**, as part of the same compromise. Record it in AGENTS.md and the porting ledger when M1 lands |
| D4 | Align MCP and other settings, or inherit the CLI's? | **Align where cheap; otherwise the CLI decides** (section 4.5) |
| D6 | How is it released, given the unknown vendor terms? | **Off by default** |
| D8 | How is it shown? | **Clearly labelled as ZMX mode** (section 4.9) |

**Still open, each with a default that holds unless the user objects:**

| # | Decision | Default |
| --- | --- | --- |
| D3 | Claude Code by PTY only, or also evaluate `claude -p` stream-json? | PTY as in nexus01; measure in M0 and revisit only with evidence |
| D5 | Confirm dropping the `opencode-antigravity-auth` route from PLAN.md section 10 | **Confirmed 2026-11-16:** dropped. Read of the README ([NoeFabris/opencode-antigravity-auth](https://github.com/NoeFabris/opencode-antigravity-auth), 708 commits, v2.1.2): it signs in with "Google OAuth (Antigravity)" (`opencode auth login google`), keeps the user's Google accounts and quota state, and calls Antigravity's models itself; the README says "Unofficial plugin. Google may restrict accounts using third-party clients." That is credential reuse by a third-party client, which section 1 rules out and which the Antigravity terms (section 6, `plans/06-zmx.md`) name as a breach. ZMX runs the unmodified `agy` under the user's own sign-in and reads no credentials. |
| D7 | Default permission for `allow` sessions: `auto`, or something stricter? | Claude Code: `auto`, disclosed. Antigravity ACP: mode `default` (prompting) regardless of the Nexus policy; never bypass (2026-11-23) |
| D9 | Antigravity transport and approval flow? | **ACP v1; Antigravity only; existing approval cards; per-call allow/deny; exact tool+argument session grants; old stream-json histories readable but not continuable (user, 2026-11-23).** Nexus E2E passed through the approval API (deny, file absent, fresh-process resume, usage recorded); visual clients and a daemon restart remain unverified |
