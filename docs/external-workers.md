# ZMX: Claude Code and Antigravity as subagents

ZMX mode runs a **child session** on the real `claude` CLI (Claude Code, in an interactive PTY) or the real `agy` CLI
(Antigravity, headless stream-json). The CLI runs its own loop, tools, permissions, settings, MCP servers, plugins and
sign-in. Nexus starts it and records what it can observe as ordinary turns and messages. In both clients, the parent
card shows the agent name on the first line and `ZMX · Claude Code` or `ZMX · Antigravity` in the metadata below.
The child's heading retains the ZMX label.

It is **off by default**, **subagents only** (never a root session or the Manager), and Nexus **never answers a CLI
prompt**: trust, sign-in and permission prompts end the turn as `needs_attention` with the command the user must run.
Design, milestones and open questions: [plans/zmx-design.md](../plans/zmx-design.md), status in
[plans/06-zmx.md](../plans/06-zmx.md).

**Vendor terms are not verified** (text read 11-12, see [plans/06-zmx.md](../plans/06-zmx.md)). This plan has not established that driving a subscription CLI this way is permitted
by Anthropic's or Google's terms. The Settings note says so; the switch stays off until the user turns it on.

## Switching it on

User-level `config.toml` only (a project file cannot turn it on; see [config.md](config.md#zmx-settings)):

```toml
[zmx]
claude_backend = "zmx"   # "sdk" (default): `claude/…` is the Agent SDK provider; "zmx": `claude/…` children run the CLI
antigravity = true        # default false
```

Settings › ZMX shows the same two rows (`Agent SDK` / `ZMX`, `off` / `ZMX`) with the disclosure line, in both clients.
On a non-POSIX machine the rows say why ZMX is unavailable (`zmx_unavailable` on `GET /api/config`).

While a switch is off: no CLI is probed or started, no credentials are read, no `claude-code`/`agy` provider or model is
listed, and a `claude-code/…` or `agy/…` ref is refused with the setting to change. Existing ZMX children stay readable.
`tests/test_zmx_runtime.py::test_daemon_with_zmx_off_probes_no_cli_and_registers_no_provider` holds this.

## Providers, models and routing

| Provider id | Label | CLI | Models |
| --- | --- | --- | --- |
| `claude-code` | `ZMX · Claude Code` | `claude` (found as the `claude` provider finds it) | models.dev `anthropic` ids |
| `agy` | `ZMX · Antigravity` | `$NEXUS10_AGY_CLI`, then `PATH`, then `~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin` | `agy models` (about 6 s; warmed at Core start, cached 60 s, a stale list is served while it refreshes) |

Both have dialect `external-cli`. `GET /api/provider` lists them only when switched on, with `zmx: true` and no sign-in
methods (sign-in is the CLI's own: `claude auth login`, or run `agy` once). `GET /api/model` marks their models
`zmx: true`; clients list those in subagent pools, and in Change model only while a subagent is viewed, never for a root session or a draft.

A child gets a ZMX model explicitly (an agent's `model:`, an `[agents.models]` pool, or `POST /api/session` with a
`parent`), or by mapping: with `claude_backend = "zmx"`, a child whose model is `claude/<id>` runs as
`claude-code/<id>` (`workers.registry.child_ref`). Roots, the Manager and read-only forks refuse ZMX models.

## How a turn runs

`Runtime._agent()` returns `workers.agent.ExternalAgent` for a ZMX model instead of the Nexus loop. It yields the same
loop events, so turns, requests, tool rows, usage, messages, the event log, export, the inspector and `via Manager` all
work unchanged. One CLI process per Nexus turn; the next turn resumes the CLI's own session.

| | Claude Code | Antigravity |
| --- | --- | --- |
| Transport | Interactive PTY. Only cursor-position replies are ever written to it | Pipes; one `{"event":"user",…}` line on stdin, NDJSON out |
| argv | `--model M --session-id S` (first turn) or `--resume S`, `--permission-mode`, `--settings <hooks>`, `--disallowedTools AskUserQuestion`, `--effort` when set, `-- <prompt>` | `--input-format stream-json --output-format stream-json --model M --mode P --print-timeout 0s`, `--conversation C` on later turns, `--effort` when set |
| Completion | The `Stop` hook (an isolated `python -I` script) after the transcript is read to the end | `init` + `result.status == "SUCCESS"` + a non-empty answer + exit 0 |
| Events from | The CLI's own transcript (`~/.claude/projects/…/<session>.jsonl`), which must resolve under the CLI's config directory | The stream |
| Text | Arrives per block (the transcript is written per block), not per token | Streams |
| `ask` permission | `--permission-mode dontAsk` | `--mode accept-edits` |
| `allow` permission | `--permission-mode auto` (the CLI's classifier) | `--mode auto` |
| Bypass | Never | Never (`always-proceed` is refused) |

Every raw line becomes a `WireEvent` (`context_complete: false`) before it is interpreted, so the inspector's wire lane
shows the CLI's entries. Tool calls keep the CLI's real name and arguments (that is what its model saw); `raw.display`
carries a Core mapping (`Read` → `read`, `Bash`/`run_command` → `bash`, …, anything else `generic`) so both clients pick
the same card. Claude Code's own subagents (`<session>/subagents/agent-*.jsonl`) are read read-only and attached to the
calling tool's result (linked by the sidechain's `toolUseId`) as `raw.sidechain`; both clients show them inside that tool's card. Nexus trees stay one
level deep.

Usage is what the CLI reported; when it reports none, usage is unknown (`null`), never zero. No cost is estimated.
Antigravity quota comes from `agy --print /usage --output-format json` in the usage panel, only while switched on.

## What the session shows (the D2 exception)

A CLI adds its own system prompt, tool definitions, memory files and reminders that Nexus cannot see. For ZMX children
only, the user accepted (decision D2, 2026-11-04) that the timeline cannot show the model's full context. Instead of
Nexus's starting context, the child's timeline opens with a **ZMX card**:

```
ZMX · Claude Code   context incomplete
Needs attention     <instruction, when the last turn needed the user>
Denied by the CLI   <n> actions, when any
Run                 model … · permission … · cwd …
Settings            CLI inherited
Not applied         Nexus permission rules, disabled tools, step limits
```

Its facts come from the session's `worker` object (below). Everything Nexus *does* observe (the prompt, the CLI's
replies, tool calls and results, recovery notices) is shown in order as usual. This is recorded in AGENTS.md and the
[porting ledger](porting-ledger.md).

## The `worker` object

Frozen in the session's snapshot, updated per turn, and sent as `worker` on session JSON, on `/api/state` session rows,
on `session.updated`, and as its own `session.worker` event (see [api.md](api.md)). `null` for ordinary sessions.

| Field | Meaning |
| --- | --- |
| `provider`, `label`, `model`, `directory`, `permission_mode`, `effort` | What the CLI was started with |
| `status` | `not_started`, `running`, then the CLI's outcome: `completed`, `needs_attention` or `failed` (an interrupted turn stays `running` in the record; the turn row says `interrupted`) |
| `error` | The instruction or failure text (for `needs_attention`: the exact command to run) |
| `session_id`, `transcript_path`, `policy_fingerprint` | The CLI's session/conversation id, (Claude) transcript path and the policy it was started with; resume requires the same policy |
| `context_complete` | Always `false` |
| `disclosure`, `not_applied`, `settings`, `mcp` | The authority statement shown on the card |
| `usage` | Last reported usage or `null` |
| `denied_actions`, `notes`, `diagnostics` | What the CLI refused, blocks or records that stayed wire-only, and sanitized stderr |

## Refused on a ZMX child

Each returns 409 (or a tool error) with a reason and changes nothing (`test_guards_change_nothing`): switching backend,
model, agent or permission policy (start a new child); changing directory; one-shots and `/btw`; handoff; forking;
continuing without a user message; prompts with images. Steering while busy queues the input for the next turn.

## Recovery and orphans

`$NEXUS10_HOME/workers/records/<session>.json` (directory `0700`, ids validated) records the pid, process start time,
CLI session id and transcript of a running turn; it is removed when the process stops normally. After a daemon crash:

- The turn reconciles as usual, but the recovery text says the CLI ran the tool and **may still be running**; nothing
  is retried.
- For Claude Code, the rest of the transcript is read once and added as labelled recovered output.
- The CLI session id is kept, so the next prompt resumes the CLI session.
- A process that outlived the daemon is never killed automatically. `nexus doctor` lists it, and `nexus workers clean`
  stops it only if both the pid and its start time still match.

`nexus doctor` reports each switch (`off (no CLI probed)` while off) and, when on, the CLI path, version and required
flags.

## Bounds and trust

Prompts over 64 KiB or containing NUL are refused, never truncated. Output lines are bounded at 4 MiB and 32 MiB in all;
the answer buffer at 64 Ki characters with announced clipping. Stderr keeps its last 16 KiB, strips terminal controls
and redacts labelled credentials. Preflight (`--version`, `--help`, stdout and stderr) is bounded to 64 KiB and five
seconds per probe. The child environment comes from a fixed allowlist (no API keys, no `PYTHONPATH`).

CLI output is untrusted data. Terminal text is diagnostic only, never assistant prose, and never drives approval:
a model printing something that looks like a prompt can at worst cause a false `needs_attention`. Cancellation stops the
owned process group (TERM, then KILL) and never signals a reaped leader or a stored pid. Detached descendants are not
guaranteed to be cleaned up. Windows is not supported (`pty` and process groups are POSIX).

## Tested versions

Offline fixtures recorded from real runs, sanitized: Claude Code **2.1.322** (transcript and a subagent sidechain) and
agy **1.5.0** (two turns, a denial, `/usage`), in `nexus-core/tests/fixtures/workers/`. Newer CLIs that pass preflight
are not thereby known compatible; unknown transcript or stream shapes are kept as wire events and disclosed, not
guessed at. The M0 questions in the design that have no recorded answer are still open.

## Verification

```sh
cd nexus-core
uv run pytest -q tests/test_zmx_workers.py tests/test_zmx_claude.py tests/test_zmx_runtime.py tests/test_zmx_reliability.py
```

All use fake executables and scratch homes; no model call. Client scenarios are `interface/scenarios/zmx-*.json`
(plain child, parallel tools, denial, needs attention, interrupt, recovered), run by the TUI `tests/scenarios.rs` and
NSH `scenarios.test.tsx`. Paired captures from a real Core: `reports/zmx-2026-11-12/` (seeded by
`reports/zmx-2026-11-11/visual_core.py`).

## Reference to nexus01

Read at `nexus01` commit `c3f18489` (`~/repos/nexus-old`): `nexus/agents/antigravity_worker.py`,
`nexus/agents/claude_code_worker.py` and their tests. Carried over: stdin lifetime, argv, explicit completion, denials,
bounds, diagnostics, group cleanup, PTY and hook handling. Not copied: the `external.*` events and reducers, old
runtime glue, Nexus read-only enforcement inside the CLI, the one-year print timeout, and the blanket long-string
redactor that masked paths.
