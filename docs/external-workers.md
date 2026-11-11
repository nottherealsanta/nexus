# ZMX external workers

**Out of date (noted 2026-11-11):** the runtime, registry, settings and client integration described as missing below now exist; see [plans/06-zmx.md](../plans/06-zmx.md) for the current status. This page needs a rewrite.

**Status (2026-11-10): transport groundwork only, not a usable session backend.**
There is no ZMX provider, configuration switch, runtime routing, API or client UI yet.
The existing `claude` Agent SDK provider and every non-ZMX session path are unchanged.
Importing `nexus.workers` does not probe a CLI, read credentials or load a provider.

The design and remaining milestones are in [plans/zmx-design.md](../plans/zmx-design.md) and
[plans/06-zmx.md](../plans/06-zmx.md).
The eventual feature is subagents only, off by default, and visibly labelled in both clients.
Do not wire this transport into session creation before its guards and paired disclosures are ready.

## Implemented

`nexus-core/nexus/workers/base.py` contains independent policy/result/event types,
a fixed child-environment allowlist, diagnostic sanitizing and labelled-secret redaction,
bounded stream/probe readers, and owned live-process-group TERM → KILL cleanup.
It has no dependency on the runtime, provider registry, store, Nexus tools or either client.

`nexus-core/nexus/workers/antigravity.py` contains:

- An explicit-executable `AntigravityWorker`; construction/import does not execute anything.
  No automatic discovery, sign-in, credential access or project config is implemented.
- Bounded `--version` and `--help` preflight, reading stdout **and stderr**, requiring
  agy 1.4.12+ and the stream-json/mode/model/print-timeout flags.
- One real CLI process per call, in the assigned cwd and its own POSIX process group.
  A single `{"event":"user","message":{"content":...}}` line carries the prompt on stdin.
  Stdin stays open until the terminal event or a failure/attention condition. No `-p`,
  prompt on argv, approval keystroke, wall-clock run deadline or Nexus tool dispatch.
- `auto`, `accept-edits` or `plan` mode. There is **no bypass option** in this foundation;
  an inherited `always-proceed` init is refused. A future user-level bypass needs its own
  authority/source validation and tests; project config must never enable it.
- Internal `WorkerEvent`s: `wire` before each consumed record's interpretation, followed
  by init/text/tool-start/tool-end/result or notice events. Unknown and malformed records
  remain available to the callback as raw bytes. These are **not** persisted events or
  a new UI protocol. Raw delivery occurs before parsing, including on a parser failure.
  Trailing stdout is drained within the same bounds during exit reconciliation, retained
  as wire records without interpreting further work. The future `ExternalAgent` must map
  transport events into normal LoopEvents.
- Explicit `init` + `result.status == "SUCCESS"` + a non-empty final response + successful
  process exit for completion. EOF, partial prose, a nonzero exit, unfinished tools or a
  terminal record followed by a hung process are not success. A failed stdin write/drain
  stops the run with a transport error rather than leaving it waiting for an assignment
  known not to have been delivered.
- Soft denials, empty final responses and sign-in/trust/approval requests become
  `needs_attention`; no approval is given. Denied actions and mode-fallback diagnostics
  are retained. Tool `DONE`, `ERROR`, `CANCELED` and `CANCELLED` close once, even when the
  first record for a tool is its ending. An ordinary incomplete stream closes open rows
  with an explicit missing-result error, not an invented tool result.
- Real terminal usage when supplied; otherwise `None` means **unknown**, not zero.
  No cost estimate. Every result states that the CLI owns its tools/settings/MCP/plugins,
  Nexus permissions/restrictions/step limits do not apply, and some CLI context is invisible.

The schema is the `nexus01` documented stream shape, tested with fakes, **not a newly
recorded live agy schema**. `--print-timeout 0s` follows the 11-04 help probe and still
needs approved live verification. Future CLI versions passing preflight are not thereby
verified compatible; unknown shapes are disclosed instead of silently interpreted.

## Bounds and trust

Prompts exceeding 64 KiB or containing NUL are refused, never truncated. Output has a
4 MiB line bound and 32 MiB aggregate bound. Exceeding either fails the run and records
a notice, rather than skipping a record and later claiming complete success. The partial
and final answer buffer is 64 Ki characters with explicit clipping counts. Stderr keeps
its last 16 KiB with a clipping notice, strips terminal controls, and redacts labelled
credentials; sanitized display diagnostics have a separate 1,500-character bound with
an explicit notice (cursor-control expansion cannot bypass it). Ordinary long workspace
paths are not mistaken for secrets. Preflight is
limited to 64 KiB and five seconds per probe. Step indices are bounded to 0–999 to bound
transport state; this is a record limit, not enforcement of a Nexus agent's step budget.

Raw stdout and normalized tool payloads are untrusted CLI/model data. The callback is
responsible for ordinary wire-event storage/redaction and for not interpreting it as
instructions or approval. Diagnostic redaction is best-effort, not a guarantee that all
unlabelled secrets are removed. Terminal diagnostics never become assistant prose.

Cancellation re-raises `asyncio.CancelledError` after stopping the owned live group;
repeated cancellation cannot abandon the shielded cleanup task. A callback failure also
stops the CLI and propagates. Cleanup never signals an already reaped leader or a stored
PID. It observes leader exit independently of inherited pipes and closes only the owned
asyncio pipe transports after reaping, so descendants holding descriptors cannot hang
cleanup or probes. **Detached descendants and descendants surviving a reaped
leader are not guaranteed to be cleaned up**; orphan tracking/recovery is still M4 work.
Windows is not supported by these process-group transports yet.

## Reference to nexus01

Read at `nexus01` commit **`c3f18489`**, in `~/repos/nexus-old`:

| Source | What was carried over deliberately |
| --- | --- |
| `nexus/agents/antigravity_worker.py` | stdin lifetime, argv, stream schema, explicit completion, denials, fallback warning, group cleanup |
| `nexus/agents/claude_code_worker.py` | prompt/output bounds, diagnostic handling, explicit authority and clipping |
| `tests/test_antigravity_worker.py`, `tests/test_claude_code_worker.py` | fake CLI, cancellation, reaped-PID protection, clipping, long-path redaction regression cases |

Not copied: `external.*` durable events/reducers, old runtime glue, Nexus read-only
enforcement, deprecated `timeout_seconds`, one-year print timeout, Claude-specific result
field names, or the blanket long-string redactor that masked paths. Partial prose is
not substituted for an empty final answer; any reported denied action requires attention.

## Verification and next work

```sh
cd nexus-core
uv run pytest -q tests/test_zmx_workers.py
uv run pytest -q
uv run ruff check nexus tests
```

All worker subprocess tests use locally generated Python fake executables and temporary
homes/workspaces; no subscription/model call, real sign-in or migration is involved.
The baseline before this slice was **1310 passed, 1 skipped**. Final full Core suite:
**1387 passed, 1 skipped**; focused workers: **77 passed**; full Core Ruff and
`git diff --check` pass. Worker tests include inherited-pipe cleanup, repeated cancellation,
failed prompt delivery, trailing-output draining and adversarial Unicode/control sequences.

Still required: approved M0 live fixtures, executable/catalog discovery, user-only mode
settings, root/feature guards, `ExternalAgent` and durable ordinary-loop events, measured
file changes, follow-up/resume, Claude PTY/hooks/transcript support, context disclosure,
recovery/orphan records, both client integrations and matching screenshots. No M1–M4
milestone is claimed complete by this transport-only slice.
