# Plan 03: Session names and recursive (RLM-style) context access

Status: proposal (advisor draft, 2026-10-17). Not implemented. Doc updates are
future implementation tasks.

Phase 3 of 3. **Requires plan 01** (immutable logical ids, supervision edges,
segments). Plan 02's manager uses names for addressing but does **not** get the
context tool by default.

---

## 1. Executive intent

1. Give every logical session a **unique, human-friendly three-word name**
   (Docker-style, e.g. `brisk-amber-heron`) with a hard uniqueness guarantee.
   The immutable logical ID stays the real key; the name is for people and
   prompts.
2. Give a root a **Recursive Language Model (RLM) tool**: the model writes small
   Python programs, run in the **Pydantic Monty** interpreter, over session
   contexts exposed as variables, and can make bounded recursive model
   sub-calls over slices. A root may inspect a child's context **only** through
   a connected **and** authorised supervision edge. Never arbitrary
   cross-session or cross-workspace access.

### Non-goals

- Full Python. Monty is a subset interpreter, not CPython (see §2.3). No
  imports of arbitrary libraries, no filesystem, no network inside the sandbox.
- Writing to another session's log, steering it, or editing its context.
- Giving the manager (plan 02) or subagents this tool in v1 (U3).
- Replacing compaction. RLM is an on-demand lookup, complementary to plan 01
  handoffs.

---

## 2. Current state and research

### 2.1 Workspace

- Session ids: ≤ 80 chars, `[A-Za-z0-9_-]` (`nexus/session/ids.py`,
  `docs/sessions.md:29`). Child ids `<parent>/sub/<n>` plus SHA-1 suffix
  (`nexus/runtime.py:1804-1849`). No name column exists; `title` is mutable and
  not unique (`docs/sessions.md:44-46`, `:171-198`).
- Child transcripts are already readable by the host through `AgentTranscript`
  (`docs/host.md:42`) and the child's first `context.assembled` request
  (`docs/agents.md:417-419`, `nexus/runtime.py:5315`). A **model** has no such
  access today; the child "does not see the parent conversation", and the
  parent sees only the child's report (`docs/agents.md:339-341`).
- The design idea is "nothing the agent can see is hidden from the user"
  (`docs/README.md:9-11`). Every RLM read must therefore be visible as tool
  rows with exact inputs and outputs.
- Bounded host projections already exist to copy: tail reads ≤ 512 KiB, pages
  ≤ 200 (`docs/sessions.md:158-160`).

### 2.2 RLM (verified by search on 2026-10-17)

- Paper: Alex L. Zhang, Tim Kraska, Omar Khattab, "Recursive Language Models",
  arXiv:2512.24601 (v1 31 Dec 2025, v3 revised 11 May 2026), MIT CSAIL:
  https://arxiv.org/abs/2512.24601
- Core idea from the abstract: treat the long prompt as part of an external
  environment; the LM programmatically examines, decomposes and recursively
  calls itself over snippets. In practice the input is a **variable in a Python
  REPL**.
- Reported results (abstract, v3; not reproduced here): inputs up to two orders
  of magnitude beyond the context window; with GPT-5, median improvement 26 %
  vs compaction, 130 % vs CodeAct with sub-calls, 13 % vs Claude Code, at
  similar cost.
- Reference implementation: https://github.com/alexzhang13/rlm ("supporting
  various sandboxes"), minimal version github.com/alexzhang13/rlm-minimal, blog
  https://alexzhang13.github.io/blog/2025/rlm/. **Not read in detail**: their
  exact sub-call function names and prompts are not copied here.

### 2.3 Pydantic Monty (verified by search on 2026-10-17)

- Repo: https://github.com/pydantic/monty ; site https://pydantic.dev/monty ;
  article https://pydantic.dev/articles/pydantic-monty ; PyPI `pydantic-monty`.
- A from-scratch bytecode VM in Rust using Ruff's parser; **not CPython**.
  Supported Python is a subset (early versions lacked classes, `match`, context
  managers, generators and most of the stdlib; current coverage not verified).
- Deny-by-default: no filesystem, network, or environment; `open`,
  `__import__`, `eval`, `exec` do not exist; `os`/`sys` are stubs. The host
  exposes capabilities only through **external functions** it supplies.
- Resource limits: memory, allocations, stack depth and execution time can be
  tracked and execution stops with a resource error. Recent releases replaced a
  session `max_duration` with feed and turn limits and reject unknown limit keys
  (per release notes summary; exact keys not verified).
- Execution can pause on an external function call and resume with a host
  value; state can be serialized (snapshot) and resumed.
- **API instability (important):** the 0.0.8 README shows
  `Monty(code, inputs=[...]).run(inputs=..., external_functions=..., limits=ResourceLimits(...))`
  and `start()`/`resume()`. A 2026-09-09 third-party post says 0.0.23 changed to
  a pooled context manager (`Monty()` → `checkout()` → `feed_run(...)`,
  `external_lookup=`). **Neither form is verified against an installed
  version.** The plan therefore pins one version and wraps it behind a Nexus
  adapter (§3.4).
- Maturity: the project describes itself as experimental; its author reports no
  successful sandbox escape so far, which is not a guarantee.

### 2.4 Docker-style names (verified by search on 2026-10-17)

- `moby/pkg/namesgenerator.GetRandomName(retry int)` returns
  `adjective_surname`; with `retry > 0` it appends a random digit. The package
  is frozen. https://pkg.go.dev/github.com/moby/moby/v2/pkg/namesgenerator
- Docker's uniqueness comes from the daemon retrying on conflict, not from the
  generator. Nexus must do the same with a DB constraint.

---

## 3. Target design

### 3.1 Names

Format: `adjective-adjective-noun` (or `adjective-color-animal`), lowercase
ASCII, hyphen-separated, each word 3–10 letters, total ≤ 32 chars. Example:
`quiet-cobalt-otter`.

| Rule | Contract |
| --- | --- |
| Key | the logical id (plan 01) is immutable and is the only foreign key anywhere |
| Uniqueness | `UNIQUE (name)` across the **whole DB** (all projects), because cross-project lists and the manager address sessions by name |
| Generation | CSPRNG (`secrets`) pick from curated word lists; insert in the same `BEGIN IMMEDIATE` as the logical session row; on `UNIQUE` conflict retry up to 16 times; then append `-2`…`-99`; then fail loudly (never reuse) |
| Space | e.g. 512 × 128 × 512 ≈ 33.5 M names. At 100 k sessions, the chance any single draw collides is about 0.3 %; the retry loop makes failure negligible. The DB constraint, not probability, is the guarantee |
| Immutability | a name is assigned once and never reassigned, even after purge (keep a `retired_names` table, or simply never delete the row's name entry) so old references never point to a new session |
| Rename | not supported in v1 (U1). Titles stay the editable label |
| Word lists | reviewed, vendored, no offensive or confusable words; no word combos that look like commands. Bump `NAMES_VERSION` on change (existing names unaffected) |
| Display | dashboard shows `title` with the name as a secondary label; prompts and tools accept name or id |

Schema (part of or after plan 01's v6):

```sql
CREATE TABLE session_names (
  name        TEXT PRIMARY KEY,           -- global uniqueness
  project_id  INTEGER NOT NULL,
  logical_id  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  retired_at  REAL,                       -- set on purge; name never reused
  UNIQUE (project_id, logical_id)
);
```

Event: `session.named` (`name`, `names_version`) as an early record in the
first segment, so replay and export carry it. Backfill: existing sessions get
names lazily on first list/open in a transaction, or in a migration batch (U2).

Retrieval host commands:

| Command | Behavior |
| --- | --- |
| `SessionResolve(ref)` | `ref` = name or id → `{logical_id, project, name, kind, state}`; only sessions the caller's workspace may list; trashed → `trashed: true`; purged → `retired` |
| `SessionList`, `SessionTree`, `ProjectSessionsList` | add `name`; `query` matches names too (literal substring, existing rules `docs/sessions.md:79-83`) |
| CLI | `nexus sessions show <name>`; chat `/open <name>` |

### 3.2 Authorisation for context access

A session **A** may read session **B**'s context only if all hold:

1. `A` and `B` are in the **same project** (edges are project-scoped, plan 01
   §3.2).
2. There is a supervision path from `A` down to `B` using `spawned` edges only
   (A is B's ancestor). Not `forked`, not `managed` (manager excluded, U3), not
   sibling, not upward. The path length ≤ `max_depth`.
3. Each edge on the path has `authority.context_read = true`. Recommended
   default: `true` for `spawned` edges created by a Nexus-run child; `false` for
   external workers (Claude Code / agy: their transcripts are Claude's/agy's own
   format and may include data Nexus never mediated, U4).
4. The tool is enabled for A's role and the permission engine allows
   `ContextQuery(<target-name>)` (`ask` by default for targets other than self).
5. A's own segments are always readable by A (that is the non-recursive base
   case and helps after a plan 01 handoff).

The check runs in the host port on **every** external-function call, not once
per program, so an edge that ends or a session that moves to trash mid-program
stops access.

### 3.3 The `context_query` tool

Tool input:

```json
{
  "program": "<python source, ≤ 16 KiB>",
  "targets": ["self", "quiet-cobalt-otter"],
  "purpose": "find why the child's test run failed"
}
```

Inside the Monty program, names bound by Nexus (inputs and external functions;
everything returns plain `str`/`int`/`list`/`dict`, never host objects):

| Name | Kind | Meaning |
| --- | --- | --- |
| `targets` | input | list of the authorised target names |
| `meta(t)` | external | `{name, kind, segments, records, tokens_estimate, status}` |
| `messages(t, start, stop)` | external | list of `{seq, role, text}` (text blocks only; thinking omitted unless already user-visible; tool results clipped to 8,000 chars) |
| `tool_calls(t, start, stop)` | external | list of `{seq, tool, input_summary, is_error, result_excerpt}` |
| `search(t, needle, limit)` | external | literal substring hits with `seq` and a 300-char window; ≤ 50 hits |
| `report(t)` | external | the child's final `SubagentOutcome.render()` or handoff digest |
| `llm(prompt, text)` | external | **recursive sub-call** (below) |
| `FINAL(value)` | convention | the program's return value (last expression) is the tool result |

Design choice: contexts are exposed as **lazy accessor functions**, not as one
giant string variable. The RLM paper puts the whole prompt in a variable; doing
that literally would copy up to MBs into the sandbox per call and bypass
per-read authorisation. Accessors give the same "context as environment"
pattern with per-read checks, bounds and a visible audit trail. (Inference, not
from the paper.)

Recursive sub-call `llm(prompt, text)`:

- Runs a **single, tool-less** model request (no loop, no tools, no session),
  like the auto-titler (`docs/sessions.md:179-182`), on the **calling
  session's own model** by default. No silent upgrade; a different model only
  if the user configured `[context_query] sub_model` explicitly.
- Bounds per tool call: `max_sub_calls = 16`, `max_sub_input_tokens = 32k`
  each, `max_total_sub_tokens = 200k`, `max_depth = 1` (a sub-call cannot call
  `llm` itself in v1; RLM-style deeper recursion is U5).
- Each sub-call is a visible, relayed event `context_query.sub_call` with
  model, token counts and a bounded excerpt of prompt/answer. Usage is charged to
  the calling session and to the shared `SubagentBudget` token/cost budget
  (`docs/agents.md:364-366`).
- `text` passed to the sub-model is wrapped as data
  (`<context source=... seq=...>`), and the sub-model output is returned as data
  to the program, never executed.

Tool output: the program's final value converted to text (≤ 25,000 tokens,
`tools.max_result_tokens`, `docs/context.md:111-112`), plus an appendix of
every read (`target`, function, seq range, bytes) so the user sees exactly what
was accessed.

### 3.4 Monty adapter

`nexus/tools/rlm/monty_adapter.py` (new, L2-ish tool support):

- Depends on a **pinned** `pydantic-monty==<X>` (version chosen in M1 spike);
  optional extra `nexus[rlm]`. If not installed, the tool is absent from the
  catalog and `nexus doctor` says why.
- One adapter API used by Nexus: `run(program, inputs, functions, limits) ->
  Result | Error`. All version-specific calls (`Monty(...).run/start/resume`
  vs pool `checkout/feed_run`) live only here.
- Limits (keys verified against the pinned version in M1; values proposed):
  memory 64 MiB, time 10 s CPU per program, stack depth 200, allocations cap,
  output ≤ 1 MiB. Unknown keys are rejected by Monty (per release notes), so a
  typo fails loudly.
- External calls are dispatched through the pause/resume mechanism so the
  async host port (DB reads, model sub-calls) runs outside the interpreter;
  wall time while waiting on a sub-call does not count toward the CPU limit,
  but the overall tool call has a separate bound (`max_tool_seconds = 120`,
  cancellable by the turn's cancel token, `nexus/core/cancel.py`).
- Run in-process first (Monty is designed for it); optional subprocess
  isolation (`python -I` worker) as a defence-in-depth flag (U6).
- Never pass host objects: only JSON-like values cross. Snapshots are not
  persisted in v1 (avoids deserialisation attack surface on stored bytes).

### 3.5 Events

| Event | Data |
| --- | --- |
| `context_query.started` | `call_id`, `targets` (names + logical ids), `program_digest`, `purpose` |
| `context_query.read` | `call_id`, `target`, `fn`, `range`, `bytes`, `denied` (bool + reason) |
| `context_query.sub_call` | `call_id`, `model`, `input_tokens`, `output_tokens`, bounded excerpts |
| `context_query.finished` | `call_id`, `status` (`ok`/`error`/`limit`/`denied`/`cancelled`), `limit` hit, totals |

The ordinary `tool.*` rows carry the full program text (it is the tool input)
so the user can read it. Reads and sub-calls are relayed for children like
other tool events.

---

## 4. Milestones

| # | Work package | Depends on |
| --- | --- | --- |
| M1 | **Monty spike**: install pinned versions in a throwaway venv, record the real API, limit keys, error types, pause/resume, supported syntax (comprehensions, f-strings, slicing, dict/list methods); write a compatibility note. Abort criteria: no external-function pause or no memory/time limits → stop and revisit | — |
| M2 | Names: word lists, generator, `session_names` table, `session.named`, `SessionResolve`, list/query integration | plan 01 M2 |
| M3 | Name UX: Ratatui and desktop labels, `/open <name>`, CLI `sessions show` | M2 |
| M4 | Authorisation function `may_read(A, B)` over edges, with exhaustive tests (siblings, forks, manager, cross-project, trashed, ended edges) | plan 01 M3 |
| M5 | Host-side read port: `meta/messages/tool_calls/search/report` over segments, bounded and redacted | M4, plan 01 M7 |
| M6 | Monty adapter + `context_query` tool for `self` only (no recursion, no children) | M1, M5 |
| M7 | Child targets behind `ContextQuery(<name>)` permission (`ask`) | M6 |
| M8 | `llm()` sub-calls with budgets and events | M7 |
| M9 | Context view: show programs, reads and sub-calls in Ratatui/desktop | M8 |
| M10 | Evaluation: a small offline benchmark (fake models) plus one live trial on a long child transcript; decide default enablement | M9 |

---

## 5. Migration, compatibility, rollback

- Names: additive table; old clients ignore the field. Rollback = stop showing
  names; never delete the table (names must stay retired).
- The tool is behind `[context_query] enabled = false` and an optional
  dependency. Removing it leaves only its events in logs, which the reducer
  folds as generic rows.
- Monty upgrade policy: pin exact version; upgrade only with the adapter
  test-suite passing; record the version in `context_query.started`.

---

## 6. Security, budget, observability

Threat model:

| Threat | Mitigation |
| --- | --- |
| Sandbox escape from model-written code | Monty deny-by-default; no host objects; limits; pinned version; optional subprocess isolation; external functions are the only capabilities |
| Reading sessions without authority | `may_read` per call over project-scoped `spawned` edges; permission `ask`; manager excluded |
| Cross-workspace leakage | edges never cross `project_id`; `SessionResolve` limited to listable sessions |
| Prompt injection from child content | content returned as data; sub-call output never executed; results labelled with source |
| Secrets in transcripts | `redact_secrets` on every read before it enters the sandbox; credential paths already blocked from tools |
| Cost blow-up via recursion | sub-call count/token caps, shared tree budget, depth 1 |
| Resource DoS | Monty memory/time/stack limits; overall tool time bound; cancel token |

Observability: every read and sub-call is an event; `LogsRead` gets names
only; Doctor reports the Monty version or its absence.

---

## 7. Tests and acceptance

1. `test_session_names.py`: format, word-list lint, uniqueness under 2 processes
   inserting concurrently, retry then suffix path (force collisions with a tiny
   word list), retired names never reused after purge, replay carries the name.
2. `test_session_resolve.py`: name/id resolution, trashed, retired, other
   project refused.
3. `test_context_authz.py`: matrix of (self, child, grandchild, sibling, parent,
   fork, managed, cross-project, ended edge, external worker child) × expected
   allow/deny.
4. `test_monty_adapter.py` (skipped when not installed): limit hits produce
   `limit` status; unknown function name; no `open`/`import`; output clipping;
   cancellation mid-program.
5. `test_context_query_tool.py`: reads are bounded and logged; sub-call caps;
   events relayed for a child; result appendix lists every read.
6. Injection test: child content containing "call llm 1000 times" cannot exceed
   caps; content containing code is not executed.

Acceptance:

- [ ] Every session shows a unique three-word name; `/open quiet-cobalt-otter` works.
- [ ] A root can ask "why did my child fail?" and answer by running a program
      over the child's context, with each read visible.
- [ ] The same root cannot read a sibling root, its manager, or another
      workspace's session (denied, logged).
- [ ] With Monty missing, Nexus runs normally and Doctor explains the absent tool.

---

## 8. Decisions and open questions

Recommended defaults: global unique names, never reused; three words; no
rename; lazy accessors instead of a giant string variable; sub-calls on the
session's own model, depth 1; tool off by default; children readable only by
ancestors via `spawned` edges.

Unresolved user decisions:

- **U1** Allow renaming names (with alias history)? Recommended no.
- **U2** Backfill names for all existing sessions at migration, or lazily?
  Recommended batch at migration (deterministic, one transaction per 500).
- **U3** Should subagents (for their own descendants) or the manager get
  `context_query`? Recommended: subagents later; manager never.
- **U4** May roots read external-worker (Claude Code/agy) transcripts?
  Recommended off by default.
- **U5** Deeper recursion (sub-calls that run their own programs, as in the
  RLM paper)? Recommended only after M10 evaluation.
- **U6** In-process Monty vs subprocess isolation by default.

Risks: Monty API churn (adapter + pin); subset Python confusing the model (tool
description lists the supported subset from M1); RLM gains in the paper are on
long-context benchmarks, not on agent transcripts (unverified for Nexus).

## 9. References (searched 2026-10-17)

Verified from search results (abstracts, READMEs, docs pages as summarised):

- Zhang, Kraska, Khattab, Recursive Language Models, arXiv:2512.24601: https://arxiv.org/abs/2512.24601
- Reference implementation: https://github.com/alexzhang13/rlm ; blog: https://alexzhang13.github.io/blog/2025/rlm/
- Pydantic Monty: https://github.com/pydantic/monty ; https://pydantic.dev/monty ;
  https://pydantic.dev/articles/pydantic-monty ; threat list:
  https://github.com/pydantic/monty/blob/main/CLAUDE.md ; PyPI 0.0.8:
  https://pypi.org/project/pydantic-monty/0.0.8/ ; releases:
  https://github.com/pydantic/monty/releases
- Third-party (API change claim, not verified):
  https://pydevtools.com/blog/pydantic-monty-secure-python-interpreter-for-ai-agents/
- Docker names: https://pkg.go.dev/github.com/moby/moby/v2/pkg/namesgenerator

Not verified: the current Monty API, limit key names, supported syntax, and the
RLM reference implementation's internals. M1 must settle the first three
before any code depends on them.
