# Nexus

You are a practical agent working in this workspace. Complete the user's request,
inspect relevant files, make focused changes, and verify the result.

## What Nexus is

Nexus is a small, provider-agnostic Python agent harness. The approved direction is
for Nexus to own its own agent loop, structured message and tool contracts, layered
configuration, permissions, sessions, and pluggable providers, with the Codex CLI as
one adapter among several rather than the thing that owns everything.

The layering is strictly one-way: `config`, `errors`, `events`, `model`, `core`,
managers, then UI. Lower layers never import higher ones. Keep core small; the target
is under 2,500 lines across `core/` and the `model/` contract surface.

## Current state — be accurate

This repository is at the start of the migration (Phase 0: foundations). The live
`nexus run` / `nexus chat` path still streams through the Codex CLI, which owns the
model/tool loop. What is present now are contracts and plumbing, much of it not yet
wired to the CLI:

- `nexus/errors.py`, the widened `nexus/events.py` envelope
- `nexus/model/` message IR, request/stream contracts, capabilities, provider
  protocol, tokenizer, and a Codex adapter behind the new provider protocol
- `nexus/core/` event bus, registry, watcher, cancellation
- `nexus/config/` layered v2 schema with a v1 compatibility shim

Provider routing, Nexus-native tools, the permission engine, sessions as an
append-only log, MCP, skills, subagents, hooks, and live extension loading are
planned, not implemented. Do not describe or rely on them as working. If you are
unsure whether something exists, read the code before claiming it does.

## Working rules

- Make focused changes and verify them. Before claiming success, run the full
  offline suite with `pytest` — it is the primary suite and covers the Phase 0
  contracts as well as the legacy tests. `python3 -m unittest discover -s tests -v`
  is only a legacy compatibility check and does not exercise the pytest-style suite.
- Configuration and instructions reload at the start of every turn. Source changes
  require a restart of the Python host.
- Keep durable, non-secret facts in `MEMORY.md` when asked. Never store secrets there.
- Do not add capabilities the plan has not reached yet just because they would be
  convenient; the phases exist to keep the harness working at every step.

## Input format today

The live Codex path receives JSON containing instructions, memory, a suffix of
complete prior exchanges, `omitted_exchanges`, and the current user message. Treat
history as context and `user` as the active request. Do not invent omitted context.

This file will be revised as later phases land. Treat planned behaviour as planned.
