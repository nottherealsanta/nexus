# Agent tool plan

## Goal and scope

Give every root agent and subagent the same **public tool vocabulary**: `bash`,
`read`, `glob`, `grep`, `edit`, `write`, `apply_patch`, `subagent`,
`todowrite`, `skill`, `question`, `websearch`, and `webfetch`. A tool may be
unavailable to a particular call because of an explicit profile, inherited
permission, or missing service configuration. In those cases, the agent should
receive a clear reason.
The vocabulary is common; authority still narrows from parent to child. In
particular, `explore` and `plan` remain unable to change workspace files.

This is an implementation plan, not a change to the running tool catalog. The
existing working tree has unrelated in-progress host and UI changes; implement
the plan in small patches that preserve those changes.

**Progress snapshot:** Phase 1 name/catalog migration is in place, and
`todowrite` is agent-scoped with event replay. Phase 2 `read` converts supported
local documents to bounded Markdown through Firecrawl AnyDoc; ordinary CSV
remains raw text unless conversion is explicitly requested. The plan is not
complete: `apply_patch`, durable `question`, web search/fetch, and isolated
subagent worktrees are still missing, among other alignment work.

## Decisions to make explicit in the implementation

1. **Public names are lowercase.** Rename the model-facing `Task` tool to
   `subagent` as requested. Keep `subagent_type` as the argument naming the role
   (`general`, `explore`, `plan`, `build`, or a discovered role). Existing stored
   `Task` events stay readable. Provide a time-bounded compatibility parser for
   old permission rules and saved agent definitions, but do not advertise both
   names to new model turns. Convert rules with exact tool names, including
   `Task(role:tier)` patterns, before evaluation; show a migration notice for
   collisions. Use the same approach for other existing PascalCase names.
2. **One tool contract and one permission path.** All built-ins use `ToolSpec`,
   `RegisteredTool`, `ToolContext`, and `ToolExecutionResult`. A new tool may use
   narrow service interfaces on `ToolContext`; it must not reach into `Runtime`
   or bypass `ToolManager` permission, hook, event, cancellation, and result-cap
   handling.
3. **Root and child catalogs share the same defaults.** Compose a base catalog
   from the requested tools, then intersect it with the selected role, parent
   authority, and permission rules. Read-only roles may use `question`, `skill`,
   `websearch`, `webfetch`, `subagent` (with read-only inheritance), and
   `todowrite`; they may not acquire `bash`, `write`, `edit`, or `apply_patch` when
   those can mutate files. A user-configured narrower profile remains valid.
4. **Keep background job control inside `bash`.** Its public schema covers
   running commands, reading job status/output, bounded waiting, and stopping
   a job. Reuse the existing registry-backed `BashOutput` and `KillShell`
   behavior. Keep those old names as compatibility aliases for saved calls and
   rules, without advertising them to new turns. `LS`, `MultiEdit`, and
   extension-management tools remain available through opt-in bundles.

## Current state and target

| Requested tool | Current Nexus state | Target work |
| --- | --- | --- |
| `bash` | `Bash` runs `/bin/sh -c`; `BashOutput` reads background jobs and `KillShell` stops them | Expose one `bash` tool for run/status/wait/stop; add configured shell and checked working directory; keep opaque job IDs and output caps |
| `read` | `read` handles UTF-8 files and workspace directory listings, with 2,000-line default and offsets; Firecrawl AnyDoc locally converts supported documents to bounded Markdown | Keep local document-to-Markdown conversion via optional `nexus-harness[documents]`; ordinary CSV stays raw unless `csv_as_markdown: true`. Images remain a separate future goal, not attachments implemented by `read` |
| `glob` | `Glob` has bounded deterministic traversal but returns up to 1,000 matches | Expose `glob`; default to 100 results with an explicit truncation notice |
| `grep` | `Grep` has a bounded regex worker and line-numbered results | Expose `grep`; default to 100 matches and keep the worker deadline |
| `edit` | `Edit` performs exact replacement with optional occurrence/all modes | Expose `edit`; keep ambiguity errors and atomic write; add only narrowly defined formatting-tolerant matching if useful |
| `write` | `Write` does atomic UTF-8 replacement, with optional parent creation | Expose `write`; retain overwrite protection guidance and permission checks |
| `apply_patch` | No built-in patch tool; `MultiEdit` only edits one file | Add a multi-file unified patch tool for add/update/delete/move |
| `subagent` | `Task` is injected per turn and already enforces budgets and inherited authority | Rename the public tool; add `worktree: true` to run a child in its own checkout |
| `todowrite` | `TodoWrite` stores session-scoped lists; child access is limited by selection | Expose `todowrite` to every agent with agent-scoped lists and durable replay |
| `skill` | `Skill` progressively loads a skill/resource and narrows active tools | Expose `skill` to root and child agents without widening authority |
| `question` | No model-facing question tool; approvals exist but are a different flow | Add durable questions for root and nested agents |
| `websearch` | No built-in web search | Add SearXNG-only search through a configured public instance |
| `webfetch` | No built-in URL fetch | Fetch the URL directly, convert the response to Markdown, and return it as the tool result |

## Shared tool behavior

### Schemas, limits, and errors

- Use JSON schemas with `additionalProperties: false`, field descriptions,
  concrete defaults, and validation before side effects. Keep names and
  arguments consistent: `path`, `pattern`, `query`, `url`, `offset`, `limit`,
  `timeout_s` where applicable. Normalize casing only at the compatibility
  boundary, never with fuzzy runtime dispatch.
- Every result has a concise display label, bounded model text, structured
  metrics, and an explicit `truncated` flag or marker. Do not silently drop
  matches, lines, or patch hunks. Reuse `tools.max_result_tokens` plus a hard
  byte ceiling. For large shell output, keep the existing job-owned tail and
  expose where the full output can be retrieved, with retention limits.
- Propagate cancellation to subprocesses, the question waiter, HTTP requests,
  and child agents. Apply per-tool timeouts. Report validation, policy, provider,
  HTTP, and target-page errors distinctly; never turn a failed call into an
  empty successful result.
- Emit tool call, progress, and completion events with session/turn/agent/call
  IDs. Keep secrets out of display, logs, durable events, and diagnostic URLs.
  Treat search snippets and fetched pages as untrusted source text, clearly
  delimited from the agent's instructions.

### Availability and permissions

- Update `nexus/tools/bundles.py`, the built-in registry, and role selection so
  the base catalog is selected for both root and child turns. Audit the static
  `READ_ONLY_TOOLS` allowlist and the `read_only` profile filter; adding a name
  to a bundle alone does not make it reachable from a read-only child.
- Preserve hard path boundaries and first-match permission evaluation. A
  `question` answer is data, never a permission grant. `subagent` cannot grant
  tools, tier, filesystem access, or network access its parent lacks. A child
  worktree changes the child's workspace root; it does not increase authority.
- Add separate `web` permission keys: `websearch` can key on the normalized
  query or configured service; `webfetch` keys on the canonical target URL/host.
  Network egress is controlled by the daemon, not by a prompt. Keep API keys in
  environment or the existing secret/config mechanism and redact them.

## Tool implementation details

### Local coding and filesystem

1. **`bash`:** Use `action: "run" | "status" | "wait" | "stop"` (default `run`).
   `run` requires `command` and may take `shell`, `workdir`, `timeout_s`, and
   `run_in_background`. Resolve `workdir` against the active workspace and
   reject an escape unless explicit policy allows it. Return exit code and
   bounded stdout/stderr, or an opaque `job_id` for background work. `status`
   and `wait` require `job_id` and may take `stdout_offset` and `stderr_offset`
   for incremental output; `wait` also takes bounded `wait_s`. `stop` requires
   `job_id`, terminates the registry-owned process group, and succeeds
   idempotently when it already finished. Reject raw PIDs and jobs owned by
   another session. Keep action-specific permission keys: the `run` key remains
   the command string for old rules; job actions use action plus job ID, so
   status/wait and stop can have different policy. Preserve timeout,
   cancellation, output caps, and lifecycle events. After daemon restart,
   report in-memory jobs as unavailable unless a supervisor proves ownership;
   never attach to a PID by guesswork. Command inspection remains a permission
   aid, not an OS sandbox; approved commands may read or write outside
   path-guarded file tools.
 2. **`read`:** Retain `path`, 1-based `offset`, and `limit` (default 2,000),
    plus the explicit `csv_as_markdown` boolean. Return numbered text lines and
    workspace directory entries with byte/line caps. For `.pdf`, `.doc`,
    `.docx`, `.docm`, `.ppt`, `.pptx`, `.pptm`, `.pps`, `.pot`, `.ppsx`,
    `.ppsm`, `.xls`, `.xlsx`, `.xlsm`, `.xlsb`, `.odt`, `.ods`, `.odp`, `.rtf`,
    and `.epub`, use the optional
    [Firecrawl AnyDoc](https://github.com/firecrawl/anydoc) dependency to
    convert locally to Markdown. CSV is also supported by that converter only
    when `csv_as_markdown: true`; without the flag it remains ordinary raw
    UTF-8 text. Require the `nexus-harness[documents]` extra, limit source
    documents to 16 MiB and converted Markdown to 4 MiB (then apply the normal
    configured tool-result/line caps), and return a clear error for scanned PDFs
    that require OCR. Conversion is local-only: do not use hosted OCR or make
    conversion network requests. Keep path-keyed read permission checks and
    hard `read_denyroots`; reading a file is non-mutating but is not globally
    workspace-confined, while directory listing is limited to the workspace.
    The conversion subprocess receives a scrubbed
    environment but runs as the same user; this is not a strong OS sandbox.
    Images remain a separate future goal: do not claim image attachments are
    implemented. Never present unsupported binary bytes as UTF-8 or send a
    whole file unexpectedly.
3. **`glob`/`grep`:** Align the public defaults to 100 results. Preserve
   deterministic order, root restriction, symlink checks, binary skipping,
   regex-worker deadline, and explicit count/truncation. Keep the `rg` via
   `bash` guidance for specialized counts; it does not bypass approval.
4. **`edit`/`write`:** Keep exact replacement as the reliable default. If
   whitespace-tolerant matching is added, make it a named opt-in mode with
   deterministic precedence and refuse ambiguous matches. `write` reads or
   checks an existing target before overwrite and returns a bounded diff or
   summary. Both tools recheck the path at commit time.
5. **`apply_patch`:** Parse an explicit patch grammar supporting Add, Update,
   Delete, and Move. Reject malformed hunks, duplicate/conflicting paths,
   binary patches, path traversal, and symlink escapes. Resolve and authorize
   every affected source/destination path before mutation. Verify all hunk
   context against a snapshot, then stage file changes and commit them in a
   deterministic order. If a commit fails partway, restore staged originals or
   report the exact partial state; never claim all-or-nothing without proving
   it. Return a combined, bounded diff and per-file status. Add one tool-level
   permission decision that includes every touched path, or require individual
   path approvals before any commit.

### Agent workflow

1. **`subagent`:** Reuse the current `Task` request shape: `prompt` required,
   `subagent_type`, `description`, `model`, optional narrowing `tools`, and
   `worktree: boolean` (default `false`). With `worktree=true`, reserve a child
   ID, create a uniquely named Git worktree in a daemon-owned writable root
   outside the active checkout from the parent's current commit, and start the
   child only after creation succeeds. Refuse the option in a
   non-Git workspace or when parent changes would be silently omitted; for the
   first version, require a clean parent checkout and explain how to proceed.
   Bind the child runtime, Bash cwd, file tools, path guard, hooks, context,
   and nested children to the new workspace. Rebase relative write roots onto
   that checkout while preserving absolute denies and all parent restrictions.
   The child may narrow tools and permissions but cannot escape its inherited
   policy through the new path.
   Record worktree path, branch, base commit, owner child ID, and final dirty
   state in durable events and the `subagent` result. Keep the worktree after
   completion for review; do not auto-merge, commit, or delete it. An explicit
   host review/integrate/discard flow owns cleanup, checks for unreviewed work,
   and reports conflicts without losing either checkout. Cancellation stops
   the child but retains its worktree for inspection.
   Advertise it as `subagent`; rename bundle/runtime references, rule examples,
   docs, and test fixtures. Preserve depth/fanout/concurrency/tier budgets,
   foreground report behavior, event relay, and child cancellation. Background
   execution is a separate opt-in design because it changes lifecycle and
   answer routing; it is not required for the first release.
2. **`todowrite`:** Keep replace-the-list semantics, item IDs, statuses, and
   priorities. Key state by `(session_id, agent_id)` so sibling child agents
   cannot overwrite one another's lists. Emit a durable snapshot on every
   revision; restore on replay/reconnect. A read-only role may update its own
   task list because this does not change workspace files.
3. **`skill`:** Keep progressive disclosure and snapshot pinning. Make it
   available to children and include provenance/hash in bounded output. Any
   `allowed-tools` or bundle declaration intersects current authority; a
   skill never activates a denied tool.
4. **`question`:** Schema supports a prompt plus one to three choices or free
   text, with stable question ID, source agent ID, and optional answer
   constraints. Add `question.requested` and `question.resolved` durable events,
   a broker separate from approvals, facade/protocol resolve operations, and
   TUI/web/CLI presentation. Route the answer to the exact waiting turn,
   including nested children; allow multiple outstanding questions without ID
   collisions. Define cancellation, reconnect, and unattended behavior so a
   child cannot leave its parent waiting forever. Responses never modify
   permissions.

### Web tools

1. **`websearch`:** Send an encoded query to a SearXNG `/search` endpoint, for
   example `https://priv.au/search?q=<encoded-query>`. Select a public instance
   from [searx.space](https://searx.space/) and make the actual base URL
   configurable; public instance availability and allowed output formats can
   change. Prefer `format=json` when the selected instance enables it. If it
   returns 403 for JSON, either parse the instance's HTML results with a tested
   adapter or try another allowlisted SearXNG instance. Do not silently switch
   to a different search provider. Bound query length, page count, timeout,
   redirects, and result count. Return title, canonical URL, snippet, engine/
   source when present, and search timestamp; deduplicate canonical URLs.
2. **`webfetch`:** Accept a fully formed HTTP(S) `url`, issue a direct GET,
   and return Markdown as the tool result. Convert HTML locally: remove scripts,
   styles, and repetitive page chrome; preserve headings, paragraphs, lists,
   links, tables, and code blocks; resolve relative links against the final
   URL. Pass through Markdown responses and turn plain text into readable
   Markdown. Decode using the declared charset with a safe fallback. Report an
   unsupported content type clearly instead of returning binary data as text.
   Include the source/final URL, title when available, HTTP status, content
   type, and an explicit truncation marker with the bounded Markdown body.
3. **Network safety:** Reject non-HTTP(S) URLs, credentials in URLs, localhost,
   private/link-local/metadata addresses, and redirects to them. Perform DNS
   checks before each direct connection, including redirects, and prevent DNS
   rebinding by connecting only to a vetted address. Do not forward local
   credentials or browser cookies. Restrict SearXNG connections to the
   configured allowlist. Add rate limiting, request deadlines, response-size
   limits, redirect limits, explicit 429 handling, and redacted telemetry.

## Implementation sequence

### Phase 1 — contract and catalog migration

1. Add a canonical-name/legacy-alias layer in the tool manager and permission
   parser. Establish a one-way mapping for existing names, especially
   `Task` → `subagent` and `BashOutput`/`KillShell` → `bash` job actions, with
   tests for old rules and replayed events.
2. Update bundles, profile selection, role tool sets, and per-turn injection.
   Check root, child, grandchild, read-only, and restricted profile catalogs.
3. Update built-in descriptions and docs so new turns see only lowercase names.
   Keep old persisted tool names renderable.

**Exit:** a new root and each seeded child can discover the intended tools
allowed by their authority; old sessions and permission rules still load.

### Phase 2 — finish local tools

 1. Align `bash`, `read`, `glob`, `grep`, `edit`, and `write` schemas/limits with
    this plan; retain existing tested internals where possible. The local
    Firecrawl AnyDoc Markdown conversion in `read` is implemented; remaining
    `read` alignment and image support are not implied complete.
2. Fold background job controls into `bash` using the current shell registry,
   and implement `apply_patch` over the existing path guard and atomic writer.
3. Add focused integration tests for job ownership, wait/stop permissions,
   output offsets, patch conflict, mixed file operations, cancellation,
   symlink escape, output caps, and approval coverage.

**Exit:** background commands can be inspected, waited on, and stopped through
`bash`; a multi-file patch either reports a verified commit or an exact
recoverable partial state; local tools respect the same workspace policy.

### Phase 3 — agent coordination

1. Migrate `Task` call sites and rule examples to `subagent`.
   Add `worktree: true` to the request, runner, child workspace construction,
   session events, and host review lifecycle. Test a clean parent, dirty
   parent, non-Git workspace, concurrent children, cancellation, and resume.
2. Make `todowrite` agent-scoped and durable; expose `skill` to children.
3. Build the question broker, events, facade/protocol operations, and all
   active user interfaces. Test concurrent root/child questions and reconnect.

**Exit:** nested questions route to the correct caller; list updates survive
replay; no child exceeds parent authority; isolated child changes remain
reviewable after the child finishes or is cancelled.

### Phase 4 — web integrations

1. Add a narrow HTTP client/service seam and configuration for SearXNG base
   URL(s), timeouts, caps, and instance allowlist.
2. Implement SearXNG search and direct URL fetching with local HTML-to-Markdown
   conversion as separate built-ins.
3. Use recorded HTTP fixtures for normal, empty, malformed, rate-limited,
   unavailable, redirected, and truncated responses. Include HTML conversion,
   relative links, declared charsets, and unsupported content types. Run
   opt-in live smoke tests only when network access is available.

**Exit:** search never uses a non-SearXNG provider; `webfetch` performs a
bounded direct fetch and returns Markdown with the source URL or a useful error.

### Phase 5 — release and documentation

Update `README.md`, `ARCHITECTURE.md`, `EXTENDING.md`, example agent definitions,
permission examples, tool descriptions, CLI help, and the host tool-list view.
Run targeted tests, then the offline suite and packaged-install smoke test.
Audit the tool names that providers actually receive and a scripted full
journey: search → fetch → read/edit/patch → question → subagent → final report.
Document the migration window and removal condition for aliases.

## Files likely to change

| Area | Primary files |
| --- | --- |
| Catalog and authority | `nexus/tools/bundles.py`, `nexus/tools/builtin/__init__.py`, `nexus/tools/manager.py`, `nexus/agents/model.py`, `nexus/agents/manager.py`, `nexus/runtime.py` |
| Local tools and jobs | `nexus/tools/builtin/{bash,bash_output,kill_shell,_jobs,read,glob,grep,edit,write}.py`, new `apply_patch.py`, `nexus/tools/permissions.py` |
| Agent workflow and worktrees | `nexus/tools/builtin/{task,todo,skill}.py`, new `question.py`, `nexus/agents/runner.py`, `nexus/runtime.py`, `nexus/session/*`, `nexus/events.py`, `nexus/view/*` |
| Question host/UI | `nexus/host/{protocol,facade,daemon}.py`, `nexus/ui/cli/*`, `nexus/ui/tui/*`, `nexus/ui/web/*` |
| Web | new `nexus/tools/builtin/{websearch,webfetch}.py`, a small HTTP adapter, `nexus/config/schema.py` |
| Verification | existing `tests/test_builtin_*`, `tests/test_tool_bundles.py`, `tests/test_permissions.py`, `tests/test_tool_integration.py`, host/replay tests, new web fixtures |

## External reference

- [SearXNG instances](https://searx.space/) lists public instances, including
  `priv.au` at the time of planning. The [SearXNG Search API](https://docs.searxng.org/dev/search_api.html)
  documents `/search`, `q`, and optional `format=json`, and warns that public
  instances may disable JSON.
- [Firecrawl AnyDoc](https://github.com/firecrawl/anydoc) provides the optional
  local document-to-Markdown converter used by `read`.
