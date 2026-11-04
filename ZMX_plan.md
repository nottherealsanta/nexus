# ZMX: Claude Code and Antigravity inside Nexus

Status: **plan, nothing built on `main`** (written 2026-11-04). Tick items and move their record to
[PLAN.md](PLAN.md#landed) as they land. Keep "not verified" honest, as PLAN.md asks.

The goal: a Nexus **subagent** can run on the real Claude Code or Antigravity (`agy`) CLI. This is **ZMX mode**. The
vendor's own CLI runs its own agent loop, tools, permissions and sign-in, and Nexus shows all of it as a child session in
the TUI and NSH. This was built on the `nexus01` branch (`~/repos/nexus-old`); this plan brings it to `main`.

Scope, decided by the user on 2026-11-04:

- **Subagents only.** A root session never runs in ZMX mode. Fitting root sessions to each CLI is too much work, and a
  child can accept compromises that a root could not.
- **Off by default.** Nothing changes for anyone until they switch it on.
- **Always labelled.** The user can always tell a subagent is running in ZMX mode.
- **Settings are aligned where that is cheap.** Otherwise the CLI uses its own (section 4.5).

## 1. What "ZMX" and "simulate" mean here

- **"ZMX" is a historical name, not a dependency.** nexus01's plan started around the ZMX terminal multiplexer and then
  dropped it: *"No ZMX dependency is currently required"*. nexus01 uses a Python PTY for Claude Code and plain pipes for
  `agy`. `zmx` is not installed on this machine, and this plan does not add it. "ZMX mode" is now the user-facing name
  of this feature, even though no `zmx` program is involved. Code may say "worker".
- **"Simulate" means driving the real, unmodified CLI.** It does not mean imitating a vendor client on the wire. Nexus
  never reads, extracts or forwards a CLI's tokens and never proxies its model traffic. `~/.gemini/oauth_creds.json` and
  the Claude credentials stay untouched. nexus01 had the same contract.
- **This replaces the earlier direction in PLAN.md section 10**, which said to use the Agent SDK and
  `opencode-antigravity-auth` "rather than ZMX". That project was not read for this plan; going by its name it reuses
  Antigravity's sign-in, which this plan rules out. Check that before confirming decision D5.

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

No model turn was run for this plan. `agy models` was run and lists `gemini-3.8-flash-{high,medium,low}`,
`claude-sonnet-4-6`, `claude-opus-4-6-thinking` and `gpt-oss-120b-medium`, preceded by a `Fetching available models...`
line that the parser must skip.

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

Each source line becomes a `WireEvent` first, so the inspector sees everything and wire events are never dropped. The
normalized events derived from it follow.

| Claude transcript | agy stream | Nexus events |
| --- | --- | --- |
| (turn start) | `init` | `MessageAdded(user)`, then `session.worker` (new, section 4.6) |
| first entry of a new `message.id` | a new `step_index` | `StepStart`, which opens a `turn_request` |
| `thinking` block | reasoning step, if agy sends one (verify) | `ReasoningDelta`, `PartDone(Reasoning)` |
| `text` block | `agent_response` `text_delta` | `TextDelta`, `PartDone(Text)` |
| `tool_use` block | tool step | `ToolCallStarted`, `ToolArgsDelta`, `PartDone(ToolCall)` |
| `message.usage` | `step_update.usage`, `result.usage` | `Usage` |
| last block of the response | step `DONE` | `Done`, then `MessageAdded(assistant)`, then `ToolStart` per call |
| `tool_result` block | tool step ended (`DONE`/`ERROR`/`CANCELED`) | `ToolEnd`, and `MessageAdded(tool)` when the step's calls are all in |
| `Stop` hook | `result` `SUCCESS` | `TurnEnd("end_turn", steps)` |
| `StopFailure`, unexpected exit | other status, EOF | `ProviderError`, so the turn fails through the existing path with its partial output kept |
| prompt detected, soft denial | same | `needs_attention` (section 4.5) |

Every part has `origin` set to `claude-code` or `agy`, and `raw` set to the source entry, as the ledger requires.

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

### 4.4 Transport

| Backend | First release | Why |
| --- | --- | --- |
| Claude Code | Interactive PTY, as in nexus01 | The contract nexus01 chose: real interactive use, not `-p` or the SDK. The `claude` provider already covers the SDK |
| agy | Headless stream-json, as in nexus01 | Documented machine interface; no PTY needed |

Decision D3 asks whether to measure `claude -p --output-format stream-json` too. It would give structured events and
perhaps token deltas, but it is a different product path, and its subscription use may differ.

**Process lifetime:** one CLI process per Nexus turn, with `--resume` / `--conversation` for the next one. It survives
daemon restarts better than a long-lived process and matches nexus01. Long-lived processes are a later optimisation.

**Prompts:** Claude's argv cap stays at 64 KiB and refuses rather than truncates; agy is bounded on stdin. A prompt with
images is refused with a clear error in the first release, as the `claude` provider does.

### 4.5 Permissions, settings and attention

Nexus does not mediate these tools. `permissions.py`, an agent's `permission:` rules, `disabled_tools` and path
confinement do not reach inside a CLI. The child's ZMX label states this. Do not imply otherwise.

**Align where cheap, otherwise let the CLI decide:**

| Nexus setting | Claude Code | agy |
| --- | --- | --- |
| Agent prompt | `--append-system-prompt` if M0 confirms it, otherwise a labelled prompt prefix as in nexus01 | Labelled prompt prefix |
| Model and effort | `--model`, `--effort` | `--model`, `--effort` |
| Working directory | `cwd` | `cwd` |
| Agent `tools:` | `--tools`, if the names map simply; otherwise the CLI's own set, disclosed | The CLI's own set, disclosed |
| MCP servers | Pass the session's servers with `--mcp-config`: Nexus entries already use Claude Code's keys (`nexus/mcp/config.py`). If M0 shows otherwise, the CLI's own servers | The CLI's own servers |
| Skills, memory, plugins, hooks | The CLI's own | The CLI's own |
| CLI settings | Inherited; `setting_sources` can narrow them | Inherited |
| Nexus `permission:` rules, `disabled_tools`, `steps` | Not applied, disclosed | Not applied, disclosed |

Nothing here is a reason to refuse a child. nexus01 refused many combinations; here a setting that does not carry over
is shown as "not applied" and the child runs anyway. The exception is a request the worker cannot run at all, such as a
prompt with images. An alignment that turns out to be costly in M0 falls back to the CLI's own behaviour without another
decision.

**Permissions:**

| Nexus session permission | Claude Code | agy |
| --- | --- | --- |
| `ask` | `dontAsk`: what would prompt is denied | `--mode accept-edits`; soft denials end as `needs_attention` |
| `allow` (the default for new sessions) | `auto`, the CLI's classifier, disclosed | `--mode auto`, with the fallback warning shown |
| Bypass | Never | Only `worker_dangerously_skip_permissions` in user-level config, shown on the label |

- **Never** `bypassPermissions`, and never a keystroke that answers a prompt.
- **Prompts are not answered inside Nexus.** Trust, sign-in and permission prompts end the child as `needs_attention`, with
  the exact command the user must run.
- **`needs_attention` is its own end state.** It goes through the existing `escalate` path (`session.escalated`), so the
  parent's report carries it, and the child's own timeline shows the instruction in full.

### 4.6 Nexus features and how each behaves

| Feature | External session behaviour |
| --- | --- |
| Interrupt | Kills the process group (`start_new_session`; TERM, then KILL). Partial output stays |
| Steer while working | Not delivered mid-turn. Converted to a queued input, with a visible notice. agy's per-line turns are a possible later change |
| Queued input | Starts the next turn with `--resume` / `--conversation` |
| Continue (`NO_USER_MESSAGE`) | 409 with a reason: a CLI cannot resume with no message |
| Switch model | Same backend: allowed for the next turn, if the spike confirms `--resume --model`. Otherwise refused |
| Switch backend or provider | Refused in the first release. Nexus's stored history cannot enter a CLI's session, and the reverse carries foreign tool calls |
| Handoff | First release: refused. Auto-handoff is off |
| `/btw`, title generation, one-shots | Never run on an external model. Titles use the configured title models; `/btw` is refused with a reason |
| Change directory | Refused while external: a Claude transcript belongs to its cwd. Decide later |
| Worktree session | Runs with `cwd` = session directory. Trust prompts appear as `needs_attention` |
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

A new `session.worker` event records the launch facts durably: provider, CLI version, argv without the prompt, cwd,
effective permission mode, settings sources, MCP, CLI session id, transcript path, and the authority statement. Replay
shows the same thing as live.

### 4.7 Recovery

`nexus/recovery.py` never kills by stored PID, and that stays. For an external turn:

- The `turn`, `turn_request` and `turn_tool` rows reconcile as they do today, but the text changes: a tool here was run by
  the CLI, and it may have **finished or may still be running**. Say so, and do not retry it.
- Store the CLI session id in the durable worker record, so the next prompt can `--resume`.
- On the next daemon start, read the rest of the transcript once to complete the record if the CLI finished after the
  daemon died. Label this as recovered output.
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
  found CLI shows as **"installed, sign-in not verified"**, and the first failed turn says to run `agy`.
- **Sign-in:** `claude auth login` as the `claude` provider does. For agy the Settings row says to run `agy` in a terminal,
  with Re-check. Nexus signs nobody in to Antigravity.
- **Executable:** `$NEXUS10_AGY_CLI`, then `PATH`, then usual locations, as `cli_path()` does. Version floor checked in
  preflight, plus the required flags from `--help` (stdout and stderr).
- **Usage panel:** `provider_usage.py` already runs `claude -p /usage`; add `agy -p /usage`, ported with its parser and
  bounded.
- **Config (`config.py`):** port only the worker keys that still apply: `permission_mode`, `tools`, `allowed_tools`,
  `disallowed_tools`, `setting_sources`, `mcp_config`, `restricted`, `dangerously_skip_permissions` (user-level only).
  Unknown keys are errors, as elsewhere in Nexus. Exact placement is decided in M1 after reading `docs/config.md`.

### 4.9 UI, in both clients in the same change

Follow `.agents/skills/nexus-ui-parity/SKILL.md`. Any command or setting change goes through `interface/manifest.toml`.

- **The ZMX label, the main requirement.** A child in ZMX mode says so everywhere it appears, with the same wording in both
  clients: a `ZMX · Claude Code` or `ZMX · Antigravity` mark on the subagent strip, the parent's child card and the child's
  tab, and a card at the top of the child's timeline stating model, cwd, permission mode, what was and was not applied from
  section 4.5, and that the CLI ran its own tools without Nexus permissions.
- **Settings:** the off-by-default ZMX switch with a one-line explanation of what it gives up.
- **Model choices:** ZMX models appear only in subagent model pools, and only when switched on, labelled `ZMX`. They never
  appear in the root model picker.
- **Tool cards:** the normal cards through the display mapping, with a quiet provenance mark, for example
  `Claude Code (own tools)`.
- **`needs_attention`:** its own state with the full manual instruction, in the session and on the parent's child card.
- **Turn footer:** real tokens, or "usage unknown". Never 0.
- **Composer:** while busy, sending says it will queue, since steering is unavailable.
- **Refused actions:** on a ZMX child, switching, handoff and `/btw` are disabled with the reason, not hidden.
- **Settings › Providers:** the two rows and their install, sign-in and re-check states.
- **Inspector (`nexus/inspector`):** wire events, the raw entries and the rebuilt requests.

The file explorer, editor and Git rules are unaffected. Verify with a mock scenario in `nexus/mock/scenarios/` and
screenshots from TUI (xterm.js) and NSH web, per the skill. This includes iOS only where the shared timeline already
reaches it.

### 4.10 Security

- Child environment comes from a fixed allowlist (`auth/claude.py child_env`); no API keys or `PYTHONPATH`.
- State in `$NEXUS10_HOME/workers/`: owned, `0700`, not a symlink, refused rather than repaired, as nexus01 did.
- The hook script runs with `python -I`, imports nothing from Nexus, receives no credentials, validates the session id,
  bounds its input and writes atomically.
- Terminal text is untrusted: ANSI-stripped, bounded, redacted, and diagnostic only. It is never treated as a model
  response and never drives approval.
- **Prompt detection never approves anything.** A model can print text that looks like a prompt; the worst result must be
  a false `needs_attention`.
- Project config cannot widen authority (`dangerously_skip_permissions`, and also `mcp_config` unless reviewed).
- Transcript and stream content is untrusted data, and is shown as data. Bounds as in nexus01, and every clip is announced
  in the timeline.
- Paths in the transcript are not trusted for reading: the transcript path must resolve under the CLI's config directory.

## 5. Code layout

```text
nexus-core/nexus/workers/
  __init__.py       ExternalAgent: LoopEvent surface, event mapping (section 4.2)
  base.py           WorkerPolicy, WorkerResult, clip, bounds, terminal_text, snapshot_workspace, changed_paths, process-group stop
  claude_code.py    PTY, HOOK script, preflight, argv, supervise, startup/permission detection
  claude_transcript.py  TranscriptReader, entry parsing, API-response grouping, request rebuild
  antigravity.py    argv, stdin protocol, NDJSON parsing, SCHEMA table, preflight
nexus-core/nexus/llmhub/
  providers.py      new dialect and two provider rows
  auth/agy.py       probes only (from antigravity_cli.py)
  catalog.py        agy dynamic models
nexus-core/tests/
  fakes/            fake `claude` and `agy` executables
  fixtures/workers/ recorded, sanitized, version-tagged transcripts and streams
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

Answer these with real but small runs, and record each answer in this file with the CLI version:

- [ ] Claude: can `api-request-*` entries rebuild a real request? Compare once against a local capture.
- [ ] Claude: does one `message.id` always mean one API response? How do parallel results appear?
- [ ] Claude: what happens to `--session-id`, `--resume --model`, `--append-system-prompt` and `--effort` in a PTY?
- [ ] Claude: `StopFailure` payload; permission-prompt wording under `dontAsk` and `auto`.
- [ ] Claude: does `--mcp-config` accept Nexus's server entries as they are?
- [ ] Claude: do `claude -p` and interactive use count the same against the subscription? (Input to D3.)
- [ ] Claude: the format of `subagents/agent-*.jsonl`, and how it links to the parent call.
- [ ] Claude: which hidden requests does interactive use make (`ai-title`), and does `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` still apply?
- [ ] agy 1.4.12: a real stream-json turn. Record `init`, `step_update`, `result`, reasoning, usage, tool steps and subagents.
- [ ] agy: `--conversation` follow-up, `--settings` allow rule, `--print-timeout 0s`, whether `--mode plan` blocks writes.
- [ ] agy: does `--mode auto` work on this account? What does a soft denial look like now?
- [ ] Both: what an interrupt leaves behind, and whether `--resume` works after it.

Done when: every box has an answer and a fixture, and section 4 is corrected where the spike disagrees with it.

### M1. Agy as a child session (smallest vertical slice)

Start with agy: it has a structured stream and no PTY, so it tests the mapping without PTY risk.

- [ ] `workers/base.py`, `workers/antigravity.py`, `auth/agy.py`, provider row, dynamic catalog, config keys.
- [ ] `ExternalAgent` with the section 4.2 mapping; `_agent()` selects it.
- [ ] Guards from section 4.6 for everything external sessions refuse.
- [ ] `session.worker` event; `files_changed`; fake `agy`; every agy lesson from section 2 has a test.
- [ ] The off-by-default switch, and the refusal of ZMX models for roots, each with tests.
- [ ] Routing: `POST /api/session {parent, model}` and the `session` tool via `[agents.models]` or an agent `model:`.
- [ ] Docs: `llm-providers.md`, `auth.md`, `models.md`, `config.md`, `daemon.md`, `api.md`, `stream-events.md`, porting ledger.

Done when: an offline suite passes, and (real, approved) `orchestrate` launches an agy child that runs a tool in a scratch
directory, its parent gets the answer, and the session reads correctly in `GET /message` and the inspector after a restart.

### M2. Claude Code as a child session

- [ ] `workers/claude_code.py`, `claude_transcript.py`, hook script, PTY, preflight, provider row.
- [ ] Display mapping for tool names (Core); fake `claude`; every Claude lesson from section 2 has a test.
- [ ] Follow-up through `message_session` → `--resume`, with the policy fingerprint check.
- [ ] Claude's own subagents: show them inside the calling tool's card, read-only. Trees stay one level deep in Nexus.

Done when: the same real check passes on Haiku, and Sonnet and Opus ids are checked rather than assumed.

### M3. Both clients and the timeline rule

- [ ] Section 4.9 in the TUI and NSH, in the same change; mock scenarios for a normal run, parallel tools, a denial,
      `needs_attention`, an interrupt and a recovered turn.
- [ ] Section 4.3: starting context at level 2 for both, plus `/context`; level 1 for Claude only if M0 shows it is cheap.
- [ ] Inspector shows raw entries and rebuilt requests.
- [ ] Ledger row for any client difference, and only with the user's approval.

Done when: TUI and NSH web screenshots of each scenario agree, and `check_manifest.py` and `check_theme.py` are clean.

### M4. Reliability

- [ ] Section 4.7: recovery text, reading the rest of a transcript, CLI session id reuse, orphan record and offer.
- [ ] Race tests: cancel during `Stop`, exit before flush, duplicate hook, transcript rewrite, oversized line, crash between entries.
- [ ] Usage panel for agy; turn summaries and the spend ledger with external usage.
- [ ] `nexus doctor` reports CLI, version and flags. A new CLI version with an unknown transcript shape warns rather than misreports.
- [ ] Update `docs/daemon.md#crash-recovery`, and add `docs/external-workers.md` and an entry in `docs/README.md` and AGENTS.md's table.

### M5. Later, not in the first release

- Answering CLI prompts inside Nexus, for example through a `PermissionRequest` hook. Not verified, and not needed for
  subagents, since `needs_attention` covers it.
- A live terminal view or human handoff into the PTY (nexus01 deferred this too). Needs a terminal surface in both clients.
- Long-lived processes for steering. Nexus control tools through a local MCP server. Handoff between backends.
- Windows: `pty` is POSIX only. Mark external providers unavailable there; do not fail at turn time. See PLAN.md 1.1.

## 7. Tests

| Layer | How | Real model? |
| --- | --- | --- |
| Parsers | Recorded, sanitized, version-tagged fixtures from M0 | No |
| Workers | Fake executables: argv, cwd, tty state, stdin log, hook firing, delays, crashes, malformed output | No |
| `ExternalAgent` + `_run` | Fake CLI through the real runtime; assert stored messages, turn rows, events, and replay equal to live | No |
| Guards | Each refused action in section 4.6 returns its reason and changes nothing | No |
| Recovery | Kill at each boundary, restart, assert no duplicates and no invented results | No |
| Regression | The existing Nexus-loop and `claude` provider suites pass unchanged | No |
| Clients | Mock scenarios, TUI tests, NSH tests, screenshots | No |
| Real | `tests/` marked and skipped by default; run by hand with approval, results recorded here | Yes |

PLAN.md section 0 reports the local suite as red today. Record a baseline before M1, so new failures are distinguishable.

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
| D5 | Confirm dropping the `opencode-antigravity-auth` route from PLAN.md section 10 | Drop it, once a read of that project confirms it reuses OAuth credentials, which contradicts section 1 |
| D7 | Default permission for `allow` sessions: `auto`, or something stricter? | `auto`, disclosed. Never bypass |
