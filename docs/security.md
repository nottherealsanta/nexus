# Security model

The daemon exposes a local endpoint that can run `bash` on the host. That is a
**permanent property of the design**, stated plainly: there is **no OS sandbox**.
Tools run on the host under the permission engine; extensions are trusted code.
Do not weaken any boundary below to make a feature easier.

## Boundaries

| Boundary | Enforcement | Code |
| --- | --- | --- |
| Loopback only | UDS `0600` in a `0700` dir; HTTP binds `127.0.0.1` (non-loopback refused at construction) | `host/daemon.py`, `host/transports/http_sse.py` |
| Peer authentication | bearer token (constant-time compare) on the peer API; one-use 60s ticket → `HttpOnly; SameSite=Strict` cookie + CSRF token for the browser; exact `Origin` (missing `Origin` refused) | `host/web.py`, `http_sse.py` |
| Browser hardening | strict CSP (no inline script/style, no external hosts), `nosniff`, `no-referrer`, `X-Frame-Options: DENY`, `Host` check | `host/web.py` |
| Credentials never cross the host | no command returns a key, token, env value or raw config; `ProviderKeySet` is inward-only and never echoed, logged or written to config; errors pass `redact_secrets` | `host/protocol.py`, `util.py`, `auth/` |
| Secrets are references | `${env:VAR}`/keychain references resolved only at request time; native keychain for OAuth/keys, read once per process and re-read only when a Nexus write or delete replaces the credential's non-secret stamp file | `config/`, `auth/store.py` |
| Permissions | `deny` absolute and daemon-side; `PathGuard` canonicalises before any allow; write roots and read-deny roots are hard; shared `nexus.db` is never tool-accessible | `tools/permissions.py` |
| Approvals never broaden | `*_always` persists an exact-action rule; unattended policy defaults to deny | `tools/permissions.py`, `session/session.py` |
| Child processes | command hooks and MCP children get a fixed safe environment plus explicitly configured names; shells are argv, not strings, unless opted in | `hooks/`, `mcp/client.py` |
| Untrusted data | MCP descriptions/results, web pages and search results are wrapped with no-authority delimiters and sanitised; results' links are never fetched | `mcp/bridge.py`, `tools/builtin/webfetch.py`, `websearch.py` |
| Outbound network | public-address-only, resolve once and pin the vetted address, validated redirects (≤ 5), size/time caps, rate limit | `net/outbound.py`, `net/local_search.py` |
| Model catalogue | only descriptive fields read; cannot set endpoints or credentials | `model/registry.py` |
| Provider URLs | http(s) only; plain http only for loopback; userinfo always redacted | `config/schema.py` |
| Extension code | quarantine validates (syntax, import in isolated subprocess with timeout, hash re-check); **does not sandbox** | `ext/quarantine.py` |
| Worktrees | authenticated records, service-root lock, no shell for children, integration applies frozen bytes only to a clean parent | `agents/worktrees*.py` |
| Settings agent | blocked from `credentials.json`, sessions, cache, daemon files, trash, `nexus.db` | `tools/permissions.py`, `host_support/settings_scope.py` |
| State at rest | `nexus.db` `0600` in a `0700` dir; Settings redacts secret-looking fields | `session/db.py`, `settings_inventory.py` |

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
