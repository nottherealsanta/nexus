# Security model

## Shared runtime trust

All loaded projects share one Python process and event loop. Project Python
extensions can inspect other runtimes' in-memory state and a blocking extension
can stall every project. Module namespacing prevents collisions; it is not a
sandbox. A process crash interrupts turns in every project; session logs remain
durable. Use separate Nexus homes/installs when separate processes are needed.

Client environments enter only through the owner-only UDS and are not echoed
in health or logs. Project browser tickets and cookies are scoped to distinct
`/w/<project-key>/` paths. Existing loopback, CSRF and permission checks apply.


The daemon exposes a local endpoint that can run `bash` on the host. That is a
**permanent property of the design**, stated plainly: there is **no OS sandbox**.
Tools run on the host under the permission engine; extensions are trusted code.
Do not weaken any boundary below to make a feature easier.

## Boundaries

The optional device gateway has a separate trust boundary; see [Gateway](gateway.md)
for setup and usage.

| Boundary | Enforcement | Code |
| --- | --- | --- |
| Loopback only | UDS `0600` in a `0700` dir; HTTP binds `127.0.0.1` (non-loopback refused at construction) | `host/daemon.py`, `host/transports/http_sse.py` |
| Peer authentication | bearer token (constant-time compare) on the peer API; one-use 60s ticket → `HttpOnly; SameSite=Strict` cookie + CSRF token for the browser; exact `Origin` (missing `Origin` refused) | `host/web.py`, `http_sse.py` |
| Browser hardening | the `/v1/web/*` API and redirects carry a strict CSP (no inline script/style, no external hosts), `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and a loopback `Host` check | `host/web.py` |
| Query-ticket redeem | `GET /app/?ticket=T` (the `nexus web` launch URL) redeems the one-use, 60 s ticket, sets the `HttpOnly; SameSite=Strict` cookie and answers `303` to `/app/` immediately, so the ticket does not stay in the address bar or history and, with `Referrer-Policy: no-referrer`, is never sent as a referrer. The loopback `Host` check runs first; an unknown or reused ticket gets the same redirect with no cookie. The fragment form (`POST /v1/web/ticket/redeem`, exact `Origin`) remains for the gateway | `host/web.py`, `cli.py:_web` |
| NSH app route CSP | only `/app/` relaxes the CSP: `script-src 'self' 'wasm-unsafe-eval'` plus `sha256-` hashes of the inline scripts in the served `index.html` (computed per response, so no `unsafe-inline` for scripts), `style-src 'self' 'unsafe-inline'` because the NSH runtime inserts `<style>` elements and rules at runtime, `worker-src`/`img-src` allow `blob:`; `connect-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'` unchanged. This is the only browser CSP that differs from the strict one on the API ([nsh-client.md](nsh-client.md)) | `host/web.py` |
| Credentials never cross the host | no command returns a key, token, env value or raw config; `ProviderKeySet` is inward-only and never echoed, logged or written to config; errors pass `redact_secrets` | `host/protocol.py`, `util.py`, `auth/` |
| Secrets are references | `${env:VAR}` references resolved only at request time; private credential file for OAuth/keys, stored in `~/.nexus/credentials.json` (`0600`, parent `0700`); atomic replacement and bounded cross-process locking | `config/`, `auth/store.py` |
| Permissions | `deny` absolute and daemon-side; `PathGuard` canonicalises before any allow; write roots and read-deny roots are hard; shared `nexus.db` is never tool-accessible | `tools/permissions.py` |
| Turn refs | Main-checkout checkpoints are gone. Hidden `refs/nexus/turns/<session>/<turn>` pin each worktree turn commit so a diff survives a squash; they stay local under ordinary push (mirror pushes can publish them), are capped per session and deleted with the session. | `agents/session_worktrees.py`, `worktree_sessions.py`, [sessions.md](sessions.md) |
| Worktree landing exemption | A shell command that writes Git elsewhere (`-C`, `--git-dir`, `--work-tree`, after a `cd`) always asks, with **one deliberate exemption**: the exact landing the agent is told to run, `git -C <this project's main checkout> merge --ff-only <a Nexus worktree branch of this project>` (optionally preceded as its own command by `git -C <main> checkout\|switch <a base branch>`), is allowed without asking in every permission mode. It is lexed, not pattern-matched (`is_worktree_landing`, `WorktreeMergePolicy`): the told `&&` chains of those same commands (at most three segments, every segment itself a landing, e.g. checkout then merge) and the `cd <main> && git …` spellings are also allowed; any other flag (`--no-ff`, `--no-verify`, `--`), extra word, other path or branch, `;` chain, pipe, redirect, substitution (`$`, backtick), newline, backslash, env prefix or `-c` falls back to the ask; explicit `deny` rules still win. `--ff-only` can only move the base branch forward to a commit that already contains it, so nothing on the base is lost; the squash runs inside the worktree, which the path guard already allows. `worktree_list`, `worktree_create` and `worktree_move` also never ask (Nexus-owned, undoable checkouts). | `tools/permissions.py`, `runtime.py`, [tools.md](tools.md#session-worktree-tools) |
| Approvals never broaden | `*_always` persists an exact-action rule; unattended policy defaults to deny | `tools/permissions.py`, `session/session.py` |
| Child processes | command hooks and MCP children get a fixed safe environment plus explicitly configured names; shells are argv, not strings, unless opted in | `hooks/`, `mcp/client.py` |
| MCP detail | `McpServerShow` and the context rows expose only the command basename and argument count; env, headers, URLs and working directories are never shown, and errors are redacted | `host/facade.py`, `runtime.py` |
| Debug manager MCP | an ordinary project `mcp.json` entry, enabled by default with no env gate (disable it in Settings > MCP or with `"enabled": false`); stdio only (no listener), a plain host-protocol client of an already running daemon (never starts one, never reads storage); its launch/send/cancel change real sessions, so they are rate-limited, `cancel` reaches only roots that server launched, and each send names the debug MCP in its first line. The MCP client's own tool approval is the human gate ([manager.md](manager.md#debug-mcp-server-debug-only)) | `devtools/manager_debug_mcp.py`, `devtools/debug_mcp_port.py` |
| Untrusted data | MCP descriptions/results, web pages and search results are wrapped with no-authority delimiters and sanitised; results' links are never fetched | `mcp/bridge.py`, `tools/builtin/webfetch.py`, `websearch.py` |
| Outbound network | public-address-only, resolve once and pin the vetted address, validated redirects (≤ 5), size/time caps, rate limit | `net/outbound.py`, `net/local_search.py` |
| Model catalogue | only descriptive fields read; cannot set endpoints or credentials | `model/registry.py` |
| xAI SuperGrok token | `auth = "xai_oauth"` only on `[providers.xai]` with no `api_key` and no `base_url` but `https://cli-chat-proxy.grok.com/v1`; the device verification URL must be HTTPS on `x.ai`; only the refresh token is stored, sign-in responses are capped at 64 KiB, and errors never carry a token; the Grok CLI version header comes from one bounded (4 KiB) read of `~/.grok/version.json` or `NEXUS_XAI_CLIENT_VERSION`, accepted only as `N.N.N[-tag]`, so a corrupt or hostile value can never inject a header; the user agent is the fixed string `xai-grok-cli` the proxy requires (Nexus is not the official client), and authenticated inference with these headers is unverified live | `auth/xai.py`, `model/providers/xai.py`, `config/schema.py` |
| Provider URLs | http(s) only; plain http only for loopback; userinfo always redacted | `config/schema.py` |
| Persistent shell jobs | a `shell` run with `background` and no `timeout_s` runs until stopped with **the user's privileges** (it can bind ports and keep running after the turn); gated by the same `shell(...)` permission as any run, capped at 4 per session / 8 per runtime, refused to subagents and code mode, never started from a legacy `run_in_background` call, stopped by the daemon after `tools.shell_orphan_s` with no viewer and no turn, and killed (whole group) by `stop`, `nexus daemon stop` and runtime shutdown; not a sandbox ([tools.md](tools.md)) | `tools/builtin/{shell,_jobs}.py`, `host/daemon.py` |
| Shell restart recovery | after a daemon is killed, the next start signals a leftover process group only if its owner's `flock` is provably dead **and** a fresh `ps` listing re-verifies a recorded member's `(pid, start time, pgid, uid)` before SIGTERM and again before SIGKILL; never a bare PID or "the group exists"; only the user's own groups, never its own; state under `<home>/projects/<key>/shell-owners/` is `0700`/`0600`, opened without following symlinks, size-capped, and any malformed or foreign record is ignored; bounded by a 6 s budget; a reopened session records one `interrupted` exit and never wakes (`tools/builtin/_job_recovery.py`, [tools.md](tools.md)) | `tools/builtin/_job_recovery.py`, `host/daemon.py`, `session/session.py` |
| Extension code | quarantine validates (syntax, import in isolated subprocess with timeout, hash re-check); **does not sandbox** | `ext/quarantine.py` |
| External Claude Code worker | Nexus does **not** mediate Claude's tools; it bounds launch authority only ([below](#external-claude-code-worker)) | `agents/claude_code_worker.py` |
| External Antigravity worker | Nexus does **not** mediate agy's tools, settings, MCP servers or plugins; no `PathGuard`; inherits agy settings by default; `worker_inherits_agy_settings = false` refuses execution ([below](#external-antigravity-worker)) | `agents/antigravity_worker.py` |
| Subagent worktrees | authenticated records and service-root lock; legacy explicit worktree children have no shell, while managed isolated children use the session-worktree profile and normal scoped shell permissions; clean shared-root integration applies only the child diff without changing `HEAD` or the real index | `agents/worktrees*.py`, `agents/session_worktrees.py` |
| Session worktrees | authenticated project-level worktree records (no owning session; any session of the project may enter one, `validate_session_root` checks project and lifecycle) and validated roots; setup trust is bound to the reviewed config digest; a create from the main checkout verifies successful write receipts and hashes, copies only those bytes, and never changes the source checkout; in a shared worktree the harness commits only the paths the session wrote; a move queued by a tool is applied only at a step boundary of the owning turn, fails closed (root unchanged, or the turn fails when the rebuilt environment cannot be validated); manual integration rechecks a confirmed preview and fast-forwards only the clean original target to the reviewed session commit; no push | `agents/session_worktrees.py`, `worktree_sessions.py`, `session_integration.py` |
| Settings agent | blocked from `credentials.json`, sessions, cache, daemon files, trash, `nexus.db` | `tools/permissions.py`, `host_support/settings_scope.py` |
| GPT Live (experimental) | the ChatGPT OAuth bearer and account id exist only in `live/transport.py` (headers per request, scrubbed with `redact_secrets` plus the literal values from every error, raw dev log and result); `Live*` results carry the SDP answer and state only; the audio helper sees SDP, never a credential; the voice model's delegations are untrusted input that go through the normal permission engine; **approvals are resolved only on screen, never by audio**; the signaling and sideband hosts are fixed OpenAI hosts, not user URLs, and (like provider calls) bypass `net/outbound.py`; no raw audio is stored | `live/`, `host_support/live.py` |
| State at rest | `nexus.db` `0600` in a `0700` dir; Settings redacts secret-looking fields | `session/db.py`, `settings_inventory.py` |

### Gateway

The optional gateway is separate and opt-in. It binds loopback only; remote
access requires an operator-managed private HTTPS proxy. Do not expose it
publicly or use Tailscale Funnel. Pairing codes expire after 120 seconds by
default and are stored hashed. Device bearer tokens are revocable, scoped and
expire after seven days by default; the registry is not stored in the OS
Keychain. A bearer token grants its assigned scopes and workspace access. The
gateway does not sandbox host processes: access to allowed workspaces is bounded
by the host process's OS permissions. Trust the gateway machine and grant only
the required scopes and roots. See [gateway setup](gateway.md).

## External Claude Code worker

An explicit `claude-code/<model>` subagent runs Claude Code's own agent loop and
tools in a PTY. This is an authority boundary, not a sandbox, and every result
says so (`Nexus permissions/PathGuard did not mediate them`).

Not enforced by Nexus: Nexus tool grants, `[permissions]` rules, `PathGuard`
write roots and read-deny roots, tool budgets, and the `Task` tool profile do
not apply to Claude's tools. Inside the workspace and permission mode you
configure, Claude decides what it does; there is no OS-level sandbox, and
`dontAsk` with explicit `worker_allowed_tools` really lets it edit or run
commands. Token and cost budgets are refused, and Nexus iteration caps are not
mapped.

Enforced at launch: the policy is explicit and resolved before start;
`bypassPermissions` and `--dangerously-skip-permissions` are never passed, and
`auto` is passed only when `worker_permission_mode = "auto"` is explicitly
configured; the default is `dontAsk` with `--restricted`, no setting sources
(user/project/local settings, plugins and their hooks stay unloaded), and no MCP
servers (`--strict-mcp-config`); Nexus writes nothing to the PTY except cursor
position replies, so it never answers a trust, login or permission prompt and
ends such runs as `needs_attention`; read-only is never enforced on a role (prompt guidance only), but a wider `worker_tools`,
`worker_allowed_tools`, `worker_mcp_config` or `worker_setting_sources` is
refused; the child environment is an allowlist (plus `CLAUDE_CONFIG_DIR`), so
provider API keys are not inherited; hook scripts and settings live in a private
`~/.nexus/claude-code/` directory (owner-only, symlinks and loose permissions
refused); terminal text is stripped and redacted before it reaches the log,
every string in `external.tool` input and result content (nested lists and
dicts included, keys too) passes `redact_secrets` before clipping, the preflight
probes are time-bounded (15s) and the process group is only signalled while the
leader is unreaped, and
file changes are measured by Nexus rather than taken from Claude's report.
Residual risks: the file-change snapshot is workspace-wide, and an approved
Claude tool can touch anything its own policy allows. Prompt and permission
wording matched by the attention detection is not verified against every Claude
release. The trust prompt and a read-only `Haiku` run were verified live once (2026-10-13); permission prompts were not.

## External Antigravity worker

An explicit `antigravity/<model>` subagent runs the `agy` CLI headless
([agents.md](agents.md#external-antigravity-workers)). Like the Claude Code worker
this is an authority boundary, not a sandbox, and every result says so. It is
implemented and tested offline only: no real model turn was run, no credentials
were used, and the stream schema is not live-verified.

Not enforced by Nexus: tool grants, `[permissions]` rules, `PathGuard` write and
read-deny roots, the `Task` tool profile, and budgets. agy applies its **own** user
and global settings, MCP servers and plugins **by default**, including when no
provider section exists (`worker_inherits_agy_settings = true` may be omitted).
Set it to `false` to refuse every assignment. This does not enable Nexus
PathGuard inside agy or relax the restrictions below. No
sandbox is requested (`--sandbox` is never passed), and agy may edit files or run
commands under its own permission mode (`auto` by default; explicit `accept-edits` or `plan` overrides).
With the opt-in bypass below, agy's tools are not mediated by Nexus at all.

Enforced at launch (refuse, never widen or ignore): tool lists,
MCP config, setting sources, `worker_restricted`, worktree children, and token or
cost budgets are all refused with `ConfigError`, because agy's `--mode plan` is not
verified to prevent writes and Nexus cannot restrict agy's tools (read-only roles are not refused: read-only is prompt guidance only). Only
`auto`, `accept-edits` and `plan` are ever passed as `--mode`. Unless the user opts in
(below), bypass or "always proceed" is never passed, and an `init` reporting `always-proceed` (for
example from agy's global settings) ends the run as refused; Nexus also never edits
agy's own settings. The prompt travels on stdin, never argv, and
Nexus writes nothing else, so it never answers a login, trust or permission prompt:
those end the run as `needs_attention`. EOF, a non-zero exit or an unknown terminal
status is never success. The child environment is an allowlist, so provider API keys
are not inherited; the child leads its own process group, which is terminated on
completion, cancel and deadline; stderr and tool text are ANSI-stripped, redacted
and bounded; and file changes are measured by Nexus, not taken from agy's report.

**Opt-in permission bypass.** `worker_dangerously_skip_permissions = true` (default
`false`) on the Antigravity provider section appends agy's documented
`--dangerously-skip-permissions` ([headless docs](https://antigravity.google/docs/cli/headless))
and then accepts an `init` reporting `always-proceed`. It auto-approves **every**
tool action agy takes (shell, edits, network, MCP), and Nexus `PathGuard` and
permissions do not mediate any of them, so enable it only for work and workspaces
you would run unattended with full authority. Safeguards: it is honoured only from
the user-level `~/.nexus/config.toml` (or a session/flag override); setting the key in
a project `nexus.toml`, `.nexus/nexus.toml` or `.agents/nexus.toml` is a load-time
`ConfigError`, so a cloned repository cannot enable it; it is refused on any other
provider kind; preflight refuses to start if `agy --help` does not list the flag;
and every `external.started`/`external.finished` event
(`skip_permissions`), the authority statement, the result text and the UI header
(`Permissions: BYPASSED`) say so. It does not relax any other refusal: tool/MCP fields, worktrees and budgets are still refused. The opt-in
covers only Nexus-managed Antigravity workers; an agy launched directly by an external
harness's own subagent tool is outside Nexus and governed by that harness's settings.

With the bypass on and `worker_permission_mode = "auto"` (the default), Nexus passes
`--mode accept-edits` instead, because `auto` is not among agy's documented modes
(`default`, `accept-edits`, `plan`) and some accounts reject it; the bypass approves
every tool regardless. An explicit `accept-edits` or `plan` is passed unchanged, and
preflight requires the mode actually passed (`accept-edits` in that case) in
`agy --help`. Without the opt-in, `auto` stays the default. This opt-in is the
supported way to let agy run commands unattended; the Claude Code interactive worker
(historically "ZMX") is unrelated and not a substitute.

Credentials: agy owns its login and credential store. Nexus never signs in, reads
tokens, stores them or signs out; `agy` exposes no token-free sign-in check, so
Settings shows the CLI as present but sign-in as unverified. Residual risks: the
file-change snapshot is workspace-wide, anything agy's own policy allows can touch
anything, and attention detection wording is unverified against real agy output.

## GPT Live: data sent to OpenAI and consent

A GPT Live call ([live.md](live.md)) sends to OpenAI, through the user's own
ChatGPT account: the microphone audio (from the client's helper, directly to
OpenAI's media servers), the rendered voice instructions (workspace folder name,
agent name and, unless `new_conversation`, a bounded excerpt of the **current
session's conversation** and the last `carry_turns` spoken turns, all shown in
`live.started`), and **the agent's progress and final answers**,
which can contain file contents or command output. The voice model's text is
treated as an untrusted prompt. Live never auto-approves anything and an
unattended session keeps its `on_unattended` policy. The first use is
confirmed in a client dialog and recorded once per machine by the daemon
(`LiveConsent`, `<nexus home>/live-consent.json`, `0600`, timestamp and notice version
only; `/live revoke` deletes it). The **daemon refuses `LiveStart` without that record**
(`live_consent_required`, before any transport is opened), so every client is gated by
the same check; this is tested offline, not verified against a real call. `NEXUS_LIVE=off` and
`[live] enabled = false` disable it. The endpoints are undocumented, may change
or stop working, and nothing was exercised against the real service.

The session excerpt is built by the host from the durable event projection: user
prompts and the final assistant text of each turn, at most 60 items of 2,000
characters each, newest kept, plus a status word (running, awaiting input,
awaiting permission or error). Thinking, tool calls, tool output and the user
messages Live itself delegated are left out. Text is passed through the secret
redaction helper and control characters are replaced; redaction is pattern based, so
unrecognised secrets in prompts or answers can still be sent. The excerpt is
quoted as untrusted background, not instructions. It is clipped to 8 KiB (newest
first; the block states how many older messages were omitted), and the whole rendered
instructions stay within 32 KiB (the excerpt is placed before the spoken carry-over,
which takes what room remains). `new_conversation` sends none of it. Because this
widened what leaves the machine, the consent notice version was raised to 2: a
version 1 record counts as not given and the notice is shown again. Whether the
real service accepts or truncates instructions of this size is **not verified**.

A call on the **manager session** ([manager.md](manager.md)) widens that again: the excerpt then
holds what the manager read of *other* sessions (titles, status, final reports, up to 16 KiB per
result). That is a second consent, recorded in the same file with its own version
(`LiveConsent` with `scope="manager"`, needs the base record; revoking the base removes both).
The daemon refuses `LiveStart` on the manager without it (`live_consent_required`) and refuses
it while `[manager] enabled = false` (`manager_disabled`). Existing base consent stays valid
for ordinary calls. Not verified against the real service.

Manager `session_launch`, `session_send`, `session_cancel` and `session_show` run with **no approval
prompt**, so on a voice call a mis-heard request can start, message or cancel a session, or move the
native client to another session, before the user can object. Guards: a real user message of the current
turn (for Live, only the delegated request, never the `Voice:` lines), visibility, per-turn caps, and
that cancel reaches only what the manager launched. `session_show` changes no session and sends
nothing; it only asks a client that is showing the manager (or following the Live call) to switch,
never over a modal, form or pending prompt.

## Outbound HTTP (`net/`)

`SafeOutboundHTTPService` validates the URL, resolves the host **once** per socket,
rejects non-public addresses (`AddressPolicyError`), and hands httpcore the vetted
numeric address while keeping the original host for `Host`, SNI and certificate
verification, which defeats DNS rebinding. Redirects are re-validated each hop (≤ 5)
and may be restricted to host/origin allow-lists. Service limits: 10s default and 30s
max timeout, 20s deadline, 2 MiB wire / 4 MiB decoded, 8 concurrent, 0.5 req/s
sustained. Credential and hop-by-hop request headers (`authorization`, `cookie`, …) are never sent. `net/local_search.py` is a
fixed-destination client for the local SearXNG (`http://127.0.0.1:18765`, 512 kB,
10s). Config-time host validation for `[tools.web]` rejects private names and
literals; the transport repeats the check at connect time.

## Trust model for extensions

Skills, agents, hooks and MCP definitions are data; `.py` tools, Python hooks and
file providers are **code** that runs with the harness's privileges once loaded,
exactly like `nexus.toml` and `SOUL.md`. The permission engine gates *calls*, not
*loading*. Quarantine catches syntax errors, import crashes, import-time hangs and
obviously dangerous import-time effects; nothing more. A hook is policy, never a
grant, and a hook `modify` is re-validated and re-gated. `ToolContext` exposes no
`Runtime`, so a tool reaches only its narrow service views.

Installing skills from `npx skills` (Settings → Skills) **runs third-party code**
(`npx` downloads and executes the pinned `skills` package, which fetches the source)
under the user's account, at the user's explicit, confirmed request, never from the
model loop and never for a paired device (`SkillsInstall` is default-denied by the
gateway). The child gets the `build_child_env` allow-list plus proxy variables (no
Nexus or provider credentials), no stdin, a process group killed on timeout or
cancel, and bounded redacted output. Nexus imports only validated copies: no
symlinks or special files, no `tools/` folder, no permission bits. Installed skills
are then trusted extension data like any other. Overwritten folders go to settings
trash. A live run of the pinned package is unverified (see plan 04).

Adding an MCP server from Settings (`SettingsMcpAdd`) **starts a process or connects to a
URL the user typed**, at the user's confirmed request: the review states that and masks
env/header values and URL credentials. Like `SkillsInstall` it is never a model tool and
is default-denied by the gateway for paired devices. The entry is validated with the
loader's own parser (no `${input:…}`, no masked placeholders, `envFile` never read), error
text and reload failures are redacted with every literal env/header/URL secret of the draft,
and `SettingsMcpAdd`'s repr hides the entry. The insert never rewrites other servers, so a
masked placeholder can never restore another server's secret. Literal credentials are stored
as typed (prefer `${env:VAR}`); a project file may be committed. Symlinked roots/files and
paths outside the scope are refused, the replace is atomic and re-checks the file, and a
project add is refused while only the legacy `.nexus/mcp.json` exists.

## Dev and test safety

Dev mode uses an isolated home and sandbox workspace and never touches the
network ([devtools.md](devtools.md)). The offline test suite needs no credentials;
`live` tests are gated by markers and environment variables. Fixtures must not
contain credentials, headers, encrypted reasoning, prompts or private output
([provider-onboarding.md](provider-onboarding.md)).

## Reporting and reviewing

When changing anything in the table above, add or extend a regression test
(`tests/test_security_regressions.py`, `test_extension_security.py`,
`test_outbound_*.py`, `test_config_secrets.py`, `test_web_transport.py`) and keep
[decisions.md](decisions.md) accurate.
# In-progress UI signal and session snippet boundaries

Tool verb/activity signals are presentation/projection data; they are not
authorization evidence and must not grant capabilities or bypass host policy.
The sessions dashboard has no preview command (`SessionDashboardPreview` was
removed; see [decisions.md](decisions.md)). `last_user` and `last_agent` are bounded
snippets cleaned before they reach the terminal. They are not copied onto every
`SessionSummary`. Archive `SessionPreview` is unchanged.
The `i` inspector keeps full tool details reachable, so the local double-click
view hides nothing from the user. This note does not assert final verification.

### Code-mode interpreter boundary

The pinned `pydantic-monty` base dependency (default-on code mode) uses pure Monty, never an embedded CPython worker.
Only the isolated helper imports the dependency. Helpers run in a new process
group, with an explicit minimal locale-only environment and a private temporary
working directory; the daemon never changes its own environment or cwd. Deadline,
cancellation and cleanup kill the entire helper process group, including Rust
workers. No OS provider, filesystem mount, dependency installation or interpreter
snapshot is permitted. IPC is bounded, framed JSON with finite numbers and a
32-level nesting limit, not pickle. Tool authority remains with nested dispatch.

The kernel tools (`code_start`, `code_attach`, `code_run`, `code_reset`) keep these rules with
one long-lived helper per runtime (`tools/codemode/kernel_host.py`, the same scrubbed
environment and process-group kill) instead of one helper per call: a kernel's variables stay in
the helper's worker, never in the daemon, and no provider credential is ever bound into it. A
kernel's attachments are a request and never authority: every cell rebuilds its callable set from
the current iteration's catalog and every nested call is permission-checked, including tools
named explicitly in `code_start(tools=...)`. A timeout, cancel
or lost worker discards that worker, so a half-run cell never leaves state behind.

**Checkpoint HMAC (kernel plan §3.8).** After each successful cell the kernel's Monty `dump()` is
stored in `~/.nexus/nexus.db` (`code_kernel_snapshots`). Monty states that snapshot bytes are not
authenticated and must only be loaded unmodified, so each row is signed with HMAC-SHA256 under a
per-home key (`code-kernel.key`, `0600`, created on first use) over `session_id`, `kernel_id`,
`after_n`, `monty_version` and the bytes. Restore verifies the MAC in constant time, the Monty
version and the kernel id **before** the bytes reach the helper; anything else is `KernelLost` and the
row is deleted. The key protects against a database edited without the key (another local user
tool, a mismatched copy), not against a process that can read both files. A checkpoint holds whatever
the cells put in variables, including tool output (the same trust as tool results already stored in
the database); it never holds provider credentials, which never enter the kernel. The helper's frame
limit stays 2 MiB for every frame except snapshot `load` requests and `dump` replies (16 MiB), decided
by the frame's own shape on both sides.

### Code-mode sandbox threats

| Threat | Mitigation |
| --- | --- |
| Credential theft from the environment | Helper starts with an explicit minimal env and a private empty temp cwd; the daemon never mutates its own env or cwd |
| Escape to filesystem/network/process | Monty exposes none; `os`, `sys`, `pathlib` are virtual and denied; `subprocess`, `urllib` unavailable; no mounts or dependency installs |
| Runaway CPU/memory/output | Monty limits (time, 64 MiB, recursion), call/output/store caps, whole-call deadline |
| Orphaned workers | New process group, `killpg` on finish, cancel and deadline |
| Tool-policy bypass | Nested calls use the full pipeline ([loop.md](loop.md#nested-tool-calls-from-code-mode)) |
| Cross-session reads | `ContextQuery(<name>)` permission plus ancestry authz on every call ([context.md](context.md#recursive-context-code-mode)) |
| Prompt injection via tool/child output | Context text is redacted, marked untrusted and wrapped `<context source seq>`; caps hold regardless |

`ContextQuery(<name>)` is a synthetic permission key through the ordinary engine:
mode fallback always `ask` (`mode = allow` does not cover it), `deny` absolute; only an
explicit `allow` rule or a grant skips the prompt.
Linux execution of the sandbox is not verified.

Isolated subagent results return only to their parent session. The `worktree`
parameter is independent of the session preference and defaults to isolation
when the selected tools may write. For a shared root, Git checks and applies the
saved child diff to working files without changing HEAD or the real index;
overlapping edits refuse integration and retain the child. Owned session branches
accept local child merges; automatic conflict merges abort. Managed child shell
access uses the ordinary scoped permission checks; the legacy explicit-worktree
profile remains shell-free. Cleanup removes only the owned checkout and keeps its
branch; explicit integration never pushes.
