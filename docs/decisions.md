# Design decisions

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

## Architecture

| Decision | Why | Where |
| --- | --- | --- |
| **One daemon per workspace owns the runtime; every surface is a pure client** with no in-process fallback. | One place for locks, turns and approvals; surfaces can crash or disconnect without losing work. | [host.md](host.md) |
| **`core/loop.py` knows only protocols.** | Testable with fakes; every manager replaceable; layering enforced by tests. | [loop.md](loop.md), `tests/test_layering.py` |
| **Events are the spine; views are pure reductions.** Every drawable state is an event, persisted before fan-out, with a monotonic per-session `seq`. | Replay reproduces live state; reconnect is "fold to `seq`, follow from `seq+1`"; SSE `Last-Event-ID` maps onto `seq`. | [events-and-view.md](events-and-view.md) |
| **`view/` sits below `model/`** and imports only `events`. | Daemon, CLI, browser and replay share one reducer. | `view/` |
| **A turn outlives its viewers**; scheduling never consults a subscriber count. | Closing a tab must never cancel work. | `host/supervisor.py` |
| **Global turn cap (4) with per-session FIFO and round-robin.** | "Five sessions × parallel tools × subagents is a fork bomb." | `host/supervisor.py` |
| **Per-turn freeze** of config, environment, `SOUL/MEMORY/AGENTS`, tool catalogue; one manifest generation per loop iteration. | Determinism and prompt-cache stability; a reload is visible next iteration/turn. | `runtime.py`, `context/manager.py` |
| **Tool-level failures are model-visible results; harness-level failures end the turn.** | The model can self-correct; the user sees real failures. | [loop.md](loop.md#failure-handling) |
| **Assistant message is persisted before any tool runs.** | No provider accepts a result without its call; crash recovery closes dangling calls without executing. | `core/loop.py` |
| **Whole-batch permission check before any tool runs.** | One approval covers several calls; a denial never leaves half a batch executed. | `core/loop.py` |
| **Fallback models only on a provider error that produced no output.** A refusal or partial stream never falls back; no mid-stream rerouting. | Avoid duplicated or inconsistent output. | `model/router.py`, `core/loop.py` |
| **Switching provider mid-session is allowed, lossy and visible** (`context.degraded`). | Flexibility without silent corruption. | `model/capabilities.py` |
| **Registry is authoritative for capabilities; adapters own transport-only fields.** A provider rejection contradicting the registry degrades once and records `registry.mismatch`. | One source of truth that can be fixed upstream; turns still complete. | `model/registry.py` |
| **Everything is bounded:** sizes, counts, pages, queues, timeouts. | Hostile or runaway input must not grow memory or stall the daemon. | throughout |

## Storage

| Decision | Why | Where |
| --- | --- | --- |
| **Sessions are append-only records in one shared SQLite DB** (`~/.nexus/nexus.db`, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`); JSONL is an export format only. Replaced per-project JSONL directories. | Indexed listing, cross-project state, race-free `seq` across daemons, one transaction for delete/archive. Legacy directories are not imported. | [sessions.md](sessions.md) (`plans/STATE_PLAN.md`) |
| **Record bytes are unchanged msgspec JSON.** | Reducer, export, fork, replay untouched by the storage move. | `session/db.py` |
| **Snapshots are derived caches validated against the log; compaction never deletes history.** | Fast resume without risking loss; "omission from the prompt does not delete history". | `session/snapshot.py`, [context.md](context.md) |
| **Project extensions and settings live in `<workspace>/.agents/`; legacy `.nexus/` is a read-only lower-precedence fallback.** Machine state is under `~/.nexus/`. | A conventional, tool-neutral project dir; writes have one home. | [config.md](config.md) |
| **Config: layered, `msgspec`, unknown keys are errors; lists append; flat v1 bridges into v2.** | Typos must fail loudly; old configs keep working. | `config/` |
| **Secrets are references (`${env:…}`/keychain)**, resolved at use. | Nothing secret in files, logs or events. | [security.md](security.md) |
| **Keychain reads are cached per process, invalidated by a non-secret stamp file** (`~/.nexus/locks/credential-<sha256>.stamp`, replaced on every Nexus write or delete). Replaced a keychain read on every model request and status check. | macOS asks to allow access each time an executable outside an item's access list reads it (a second Python install, an upgrade, a Codex refresh rewriting the item), so per-request reads caused repeated prompts. Nexus logins and logouts in other processes are still seen on the next request; edits made in Keychain Access are seen after a daemon restart. | `auth/store.py` |
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
| **Declarations are never grants** (skills, agents, hooks narrow only); read-only roles are structural. | A markdown file must not expand authority. | `skills/activation.py`, `agents/model.py` |
| **Restricted frontmatter, no YAML dependency.** | Small attack surface; one bad line rejects the file. | `agents/model.py`, `skills/frontmatter.py` |
| **Tool names are lowercase; legacy names map one-way** and never widen rules. | Stable public vocabulary with compatibility for old rules. | `tools/names.py` |
| **Agent questions are data, not grants** (separate event group). | An answer must never be mistaken for an approval. | `events.py` |
| **A hook is policy, not a permission;** a `modify` is re-validated and re-gated. | Hooks cannot launder a denied call. | `hooks/` |
| **Children never exceed parents:** tools intersect, permissions inherit, tier is clamped, depth/fan-out/spend bounded. | Delegation must not be an escalation path. | [agents.md](agents.md) |
| **Worktree integration applies frozen bytes to a clean parent with a rollback journal; no Git commit/reset/stash/checkout; the user acknowledges an exact review.** | Reviewable, recoverable, no surprise history changes. | `agents/worktree_*.py` |
| **MCP is wrapped once (`mcp/client.py`); its output is untrusted;** a tool is mutating unless `readOnlyHint` is explicitly true; a dead server never fails a turn. | Upstream churn stays in one file; fail-safe defaults. | `mcp/` |
| **Web tools are public-address-only with address pinning;** search results' links are never fetched. | SSRF and rebinding defence; injection surface reduction. | `net/` |
| **`bash` yields long commands to background jobs** instead of killing them; `wait` returns on exit. | Long builds finish; the model stops polling. | [tools.md](tools.md#shell-jobs) |

## Providers and models

| Decision | Why | Where |
| --- | --- | --- |
| **Any OpenAI-compatible vendor is a config block, not code** (and must state a `base_url`). | No silent default to `api.openai.com`. | `runtime.py:_adapter_kind` |
| **The models.dev catalogue supplies descriptions only,** never endpoints or credentials; vendored snapshot with MIT attribution; offline mode. | A third-party catalogue must not redirect requests. | `model/registry.py` |
| **Tiers resolve deterministically** (user pin → curated → cost → `low`) and only clamp downward. | Predictable routing and spend. | `model/tiers.py` |
| **Codex models go through the OpenAI adapter's Responses dialect** (ChatGPT OAuth, experimental, private endpoint), not a Codex CLI subprocess. | One adapter, tokens in the keychain only. | `auth/codex.py` |
| **GitHub Copilot uses GitHub.com device sign-in with OpenCode's OAuth app id;** the GitHub token is the Copilot bearer (no `copilot_internal` exchange). | Simple, verified against `/models`. The device flow itself was not live-tested. The root `README.md` still says Copilot is omitted; it predates this. | `auth/copilot.py` |
| **OpenCode is integrated over ACP only;** Nexus never reads its credential store; ACP tool calls stay inside that agent. | Clear trust boundary. | `model/providers/opencode.py` |
| **Claude subscription via the official Agent SDK in an isolated worker,** SDK tools/hooks/settings/persistence disabled; text-only, buffered. | Nexus keeps logging, permissions and execution. | `model/providers/claude_agent.py` |
| **Provider usage reuses the credentials Nexus already holds;** Claude's comes from `claude -p /usage`, not the `api/oauth/usage` endpoint CodexBar calls. | Reading Claude Code's OAuth token from its keychain item would break "the CLI owns the credential store"; `/usage` is a local command that costs no tokens. Its text format is unversioned, so a CLI change shows as a row error rather than wrong numbers. | `host_support/provider_usage.py` |
| **Claude sign-in runs `claude auth login` headless and takes a pasted code;** Settings never signs Claude out. | The daemon never opens a browser, and the login is shared with Claude Code, so a Nexus "Disconnect" would surprise the user. | `host_support/provider_auth.py` |
| **`Ctrl+U` opens usage even in the composer.** | One direct key in both surfaces; the TUI composer loses readline's delete-to-line-start (`Cmd+Backspace` still works). | `ui/tui/app.py:on_event` |
| **Thinking summaries are provider-supplied and never invented;** no duration is fabricated. | Honest presentation. | [loop.md](loop.md#thinking) |
| **Voice uses Kestrel's internal Parakeet runtime, not Photon** (telemetry, no opt-out). Model download is consent-gated and never implicit. | Privacy. Real inference is unverified. | [voice.md](voice.md) |
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
| **No line caps;** new Textual behavior still gets its own module. | Caps produced contortions; layering is what matters. | `tests/test_phase3_exit.py` |
| **Conventional Commits drive versions; never edit `version` or `CHANGELOG.md` by hand.** A version-bump request defaults to the next patch and authorises the full release; a minor bump needs explicit approval. | release-please owns the files; patch releases should be cheap. | [release.md](release.md) |
| **Docs are the source of truth; code wins over plans.** | Agents need one place to look. | [README.md](README.md) |

## Known gaps and drift (as of this writing)

Stated so nobody builds on a false assumption.

- **No summarizer is wired in `Runtime`:** `hybrid` compaction is evict-then-drop,
  and `strategy = "summarize"` fails actionably ([context.md](context.md#compaction)).
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
- **Not done:** completion notifications for background shell jobs still running
  at turn end; `nexus ext restore`.

## Standard installation includes Claude Agent; the installer adds voice

Claude Agent SDK and `sounddevice` are required package dependencies; the old
`claude-agent` extra remains a compatibility alias. The voice runtime
(`moondream`, which needs `kestrel-native`) stays in the `voice` extra because
`kestrel-native` publishes no musl wheels: as a hard dependency it made
`install.sh` fail outright on Alpine. PEP 508 markers cannot tell musl from glibc,
so `install.sh` adds `voice` itself except on musl (or with `--no-voice`), and
`nexus update` keeps installed extras. Plain `pip`/`uv tool install nexus-harness`
needs `[voice]`; source checkouts use `uv sync --extra voice`. Claude login and the
consent-gated voice model download remain separate setup steps. Voice runtime
platform support beyond macOS and glibc Linux is not verified (see [voice.md](voice.md)).
