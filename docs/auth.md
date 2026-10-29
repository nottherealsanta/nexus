# Auth

`nexus/llmhub/auth/` holds each provider's sign-in. It hands the provider
adapters a header source; adapters never see stored credentials
([llm-providers.md](llm-providers.md)).

| File | Role |
| --- | --- |
| `auth/__init__.py` | `SIGN_INS = {"codex", "copilot", "grok", "opencode"}` |
| `auth/store.py` | `CredentialStore`: `~/.nexus10/auth.json` |
| `auth/device.py` | RFC 8628 device flow (copilot, grok) |
| `auth/codex.py` | ChatGPT OAuth: browser PKCE and device code |
| `auth/copilot.py` | GitHub.com device sign-in |
| `auth/grok.py` | SuperGrok device sign-in and Grok CLI proxy headers |
| `auth/opencode.py` | OpenCode Go API key |
| `auth/errors.py` | `AuthError`: short messages, never token material |

## Sign-in from clients

Both clients offer provider sign-in and sign-out in Settings. Device flows use
`POST /api/provider/{id}/login`; instructions arrive through `provider.login`
events and `GET` on the same path reports progress. OpenCode accepts an API key
through a masked input. `DELETE` cancels a pending flow and signs out. Keys are
never included in progress events. See [Configuration](config.md).

## The shared interface

Every sign-in class takes an optional `CredentialStore` and has:

| Member | Meaning |
| --- | --- |
| `methods` | Tuple of sign-in methods. First is the default |
| `async login(method, notify=print)` | Run the flow; `notify(str)` shows a URL or code to the user |
| `logged_in() -> bool` | Credentials exist (stored; opencode also accepts `$OPENCODE_API_KEY`) |
| `logout()` | Delete the stored entry (under the lock) |
| `async headers() -> dict[str, str]` | Auth headers for one request; refreshes tokens when needed |

| Provider | `methods` | Flow |
| --- | --- | --- |
| `codex` | `("browser", "device")` | PKCE in a browser, or a device code |
| `copilot` | `("device",)` | GitHub device code (RFC 8628) |
| `grok` | `("device",)` | auth.x.ai device code (RFC 8628) |
| `opencode` | `("api_key",)` | Paste a key |

CLI: `nexus login <provider> [--method M | --device]`, `nexus logout <provider>`,
`nexus status`. An unknown method for a provider raises `AuthError`.
`login` in `cli.py` maps `--device` to `device` for any provider.

## codex

Constants: issuer `https://auth.openai.com`, a public client id, redirect
`http://localhost:1455/auth/callback`, scopes `openid profile email offline_access`.

**Browser (PKCE).** A random verifier and its S256 challenge, a random `state`.
A one-shot HTTP server listens on `127.0.0.1:1455`. The authorize URL
(`/oauth/authorize`, with `id_token_add_organizations`, `codex_cli_simplified_flow`
and `originator=nexus`) is opened in the browser; if it cannot be opened, the URL
is passed to `notify`. The callback must be `GET /auth/callback`, with a code and
a `state` that matches (constant-time compare), otherwise `AuthError`. Waits up to
300 s. The code is exchanged at `/oauth/token` with the verifier.

**Device.** `POST /api/accounts/deviceauth/usercode`, tell the user to open
`https://auth.openai.com/deviceauth` and enter the code, poll
`/api/accounts/deviceauth/token` (HTTP 403 and 404 mean "not yet"; interval at
least 1 s, default 5 s; gives up after 900 s), then exchange the returned
authorization code and verifier at `/oauth/token`.

**Headers.** `authorization: Bearer <access>`, `originator: nexus`,
`user-agent: nexus/0.10`, plus `chatgpt-account-id` and
`x-openai-internal-codex-residency` when known. The access token is refreshed
when it is missing or expires within 60 s.

**Routing metadata.** The account id and the compute residency come from JWT
claims (`id_token`, then `access_token`; the account id may also come from
`organizations[0].id`). `no_constraint` residency is stored as none. JWTs are
decoded only to read these claims, never trusted for authorization, and tokens
over 16,384 characters are ignored.

## copilot

Device flow against `github.com/login/device/code` and
`/login/oauth/access_token` with a fixed OAuth app client id, scope `read:user`.
There is no Copilot token exchange: the GitHub token is the bearer. Before it is
saved, it is checked with `GET https://api.githubcopilot.com/models`. 403 means
no active Copilot plan or a blocked organization; other 4xx and 5xx are rejected.

Headers: `authorization: Bearer <github token>`, `user-agent: nexus/0.10`,
`openai-intent: conversation-edits`, `copilot-integration-id: copilot-developer-cli`.
GitHub Enterprise is not supported. This code never refreshes the token and has
no expiry handling.

## grok

Device flow against `https://auth.x.ai/oauth2` (`/device/code`, `/token`) with a
fixed client id, scope `openid profile email offline_access grok-cli:access api:access`
and `referrer=nexus`. Each poll waits 3 s longer than the server interval
(`extra_wait`) to stay clear of `slow_down`. The response must include a refresh
token, otherwise `AuthError`.

The access token is the bearer for the Grok CLI proxy and is refreshed when it
expires within 300 s.

**Client version requirement.** The proxy requires the Grok CLI's client
identity, including a current version. `headers()` raises `AuthError` unless a
version is found, in this order:

1. `$NEXUS_GROK_CLIENT_VERSION`
2. `~/.grok/version.json` (the installed Grok CLI): key `version`, then `stable_version`

A value must match `N.N.N` (1 to 4 digits each) with an optional
`-suffix`. The file is read up to 4096 bytes.

Headers: `authorization`, `x-xai-token-auth: xai-grok-cli`,
`x-grok-client-version`, `x-grok-client-identifier: grok-shell`,
`user-agent: xai-grok-cli`.

## opencode

An API key from the OpenCode Go subscription (opencode.ai/auth). `login` prompts
with `getpass` (no echo); the key must be non-empty, at most 4096 characters, and
contain no whitespace. `$OPENCODE_API_KEY` is used when set (a stored key wins if
both exist) and nothing is stored for it. Headers: `authorization: Bearer <key>`,
`user-agent: nexus/0.10`.

## device flow (`device.py`)

`device_flow(...)` runs RFC 8628 end to end and returns the token response.

* Requests are form-encoded, 20 s timeout, no redirects; responses over 64 KiB
  are rejected.
* The user is told `Open <verification_uri_complete or verification_uri> and enter code <user_code>`.
* Polling interval: the server's `interval`, clamped to 1 to 60 s (default 5),
  plus `extra_wait`. `authorization_pending` continues; `slow_down` adds 5 s
  (cap 60 s). `access_denied`, `authorization_declined`, `authorization_denied`
  raise "denied"; `expired_token` raises "code expired".
* Deadline: the server's `expires_in` clamped to 60 to 1800 s (default 900).

## Storage: `~/.nexus10/auth.json`

`$NEXUS10_HOME` overrides `~/.nexus10` (`paths.home()`). One JSON object, one
entry per provider id.

| Entry | Stored | Never stored |
| --- | --- | --- |
| `codex` | `refresh_token`, `account_id`, `residency` | access token, id token |
| `copilot` | `github_token` | n/a |
| `grok` | `refresh_token` | access token |
| `opencode` | `api_key` | n/a (an env key is not stored) |

Access tokens live in memory on the sign-in object only.

Safety checks (`CredentialError` on failure):

* The directory is created `0700` and must be a directory owned by the user with
  no group or other bits.
* The file is opened with `O_NOFOLLOW`, must be owned by the user and have no
  group or other bits (`0600`), at most 256 KiB, and valid JSON. Invalid JSON
  says to log in again.
* Writes go to a `0600` temp file in the same directory and are renamed over
  `auth.json`.

### Locking and refresh-token rotation

`store.locked()` takes an exclusive `flock` on `auth.lock` in the same
directory, across processes. Refresh tokens rotate (the old one stops working),
so a refresh does read, token request and write inside the lock:

* codex: reads the entry, refreshes, and writes back the new `refresh_token`
  (keeping the old one if none is returned), `account_id` and `residency`.
* grok: reads the entry, refreshes, and writes only when the server returned a
  different refresh token.

Writes in `login` and `logout` also hold the lock. `read()` alone does not.

Not verified: `nexus login copilot|grok|opencode` end to end (PLAN.md, step 3).
