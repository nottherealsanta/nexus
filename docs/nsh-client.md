# NSH web client (basic) and the `/v1/web/*` API

`nexus web` opens the NSH client. It replaced the old plain HTML/JS browser
client, which was removed on 2026-10-26 (see [decisions.md](decisions.md)). A basic
Native Shell (NSH) Rust web client lives in `rust/app/` and is served by
`nexus web` at `/w/<key>/app/` (module: `nexus/host/web.py`, `NSH_ROOT` =
`nexus/ui/nsh/`). The plan is
[plans/planning/nsh-cross-platform-clients.md](../plans/planning/nsh-cross-platform-clients.md);
this page covers only what exists.

- **Build.** `scripts/nsh-app.sh package web` runs `nsh package --target web` in
  `rust/app` and replaces `nexus/ui/nsh/` with the output. That directory is
  build output and git-ignored. `scripts/nsh-app.sh run web` runs the NSH dev
  server. `NSH` overrides the binary; the fallback is `~/.local/bin/nsh`.
- **Launch.** `nexus web` (`cli.py:_web`) sends `WebLaunch` over UDS; the daemon
  returns `http://127.0.0.1:<port>/w/<key>/#ticket=T` (the shape `gateway/upstream.py`
  parses) and the CLI rewrites it to `.../w/<key>/app/?ticket=T`, because NSH 0.2.0
  apps cannot read the URL fragment. `--no-browser` prints that URL. A request to
  `/app/?ticket=T` redeems the ticket (one use, 60 s TTL, loopback `Host` check),
  sets the session cookie and answers `303` to `/app/` at once, so the URL the
  browser keeps no longer carries it (`Referrer-Policy: no-referrer` applies too).
  An invalid or used ticket still redirects, without a cookie. `GET /` and
  `/s/<id>[/a/<agent>]` answer `303` to `<prefix>/app/` so old bookmarks land here.
- **Serving.** `/app` redirects to `/app/`; `/app/` is `index.html`. A missing
  bundle returns 404 `{"error":"nsh app not built"}`. Same `Host` check and
  resolve + `relative_to` traversal protection as the existing statics. Static
  files need no cookie; the API is unchanged and still needs the web cookie, so
  open `nexus web` first (the app reuses its cookie and CSRF token).
- **Caching and MIME.** `index.html` and `manifest.webmanifest` are `no-cache`;
  other files have content-hashed names and are `immutable` for a year. `.wasm`
  is `application/wasm`.
- **CSP.** Route-scoped; see [security.md](security.md).
- **Transport.** Snapshot (`session-view`), then repeated `session-poll`
  long-polls (below); the session list is polled with `SessionList` every 5 s.
  NSH's Rust `fetch` returns whole bodies only, so SSE cannot be read; the SSE
  routes stay for other clients.
- **Status.** Foundation (WP1 of `plans/planning/nsh-tui-parity-inventory.md`):
  sessions, live timeline and send only. No settings, voice, attachments or
  approvals UI. Web verified with Playwright (sessions list, timeline, `session-poll`
  requests); `cargo test` in `rust/app` covers the patch applier (real captured frames
  replayed to the snapshot), presenters, launch parsing and the layering rule.

## Static serving is separate from Nexus core

Only `nexus/host/nsh_static.py` knows where the bundle lives (`NSH_ROOT`,
CSP, MIME/cache, `?ticket=` redirect); `web.py` delegates `/app` and `/app/*`
to it and keeps the generic `/v1/web/*` API, auth and the `/` redirect.
`tests/test_client_separation.py` enforces both directions: no Python file under
`nexus/` except `nsh_static.py` mentions `ui/nsh`, `NSH_ROOT`, `rust/app` or a
`"nsh"` path segment, and no `.rs` file in `rust/app/src` mentions `.nexus/`,
`nexus.db`, `sqlite` or `std::fs` (the client uses the HTTP API only).

## `rust/app` layout

| File | Responsibility |
| --- | --- |
| `src/lib.rs` | composes `App` only |
| `src/platform/` | the only target-specific code (`cfg(target_arch)`): `Launch` (API base, `Origin`, ticket). `tests/layering.rs` fails if `cfg(` (other than `cfg(test)`) appears outside `src/platform/` and `src/main.rs` |
| `src/api.rs` | `Api` (Copy): `connect()`, `get_json(path)`, `command(json)`; cookie/`Origin`/CSRF; writes never retried |
| `src/ops.rs` | pure patch-frame applier (`apply_frame`, `apply_ops`); real-frame fixtures in `tests/fixtures/` |
| `src/store.rs` | `Store` signals (workspace, sessions, selected, `view`, `seq`, `conn`, `notice`) and the per-session live loop |
| `src/present/` | pure view -> rows (`timeline.rs`, `sessions.rs`) |
| `src/components/` | `topbar`, `sidebar`, `timeline`, `composer` |

Live loop: snapshot, then `session-poll?from_seq=cursor&wait=25`, apply frames in
one `batch`; a `resync` frame, an op failure or an HTTP error re-snapshots with
backoff 1 s to 10 s. Selecting another session cancels the loop, and results are
also dropped when the selected id changed.

## Native (macOS) launch: not verified

The native target reads `NEXUS_URL` (`http://127.0.0.1:PORT/w/KEY/`) and
`NEXUS_TICKET`, redeems the ticket with `POST {NEXUS_URL}v1/web/ticket/redeem` and
`Origin`, keeps `csrf` from the JSON and the `Set-Cookie` pair, and sends
`Cookie`, `Origin` and `X-CSRF-Token` itself. `scripts/nsh-app.sh run macos` mints
the ticket from `nexus web --no-browser` and runs `nsh run --target macos` with
those variables; `package macos` runs `nsh package --target macos`. **Not verified
end to end**: only that the crate builds natively; the app was not run against a
daemon in this change (an earlier probe only showed the env variables reach the app
under `nsh run`). The ticket lives 60 s, so the script builds first.

## The `/v1/web/*` API

`host/web.py` (`BrowserRoutes`) owns every browser route on the daemon's loopback
listener (`host/transports/http_sse.py`). The NSH app, the gateway and iOS use this
API. Tickets and cookie sessions die with the daemon.

| Route | Purpose |
| --- | --- |
| `/app/*` | the NSH bundle (above); `/`, `/s/<session>...` redirect to `/app/` |
| `POST /v1/web/ticket/redeem` | JSON `{ticket}`, exact `Origin`; one-use, 60-second ticket -> `HttpOnly; SameSite=Strict` cookie (12 h) + CSRF token. Used by the gateway upstream (`/#ticket=` URL shape) |
| `GET /v1/web/bootstrap` | CSRF token + workspace path |
| `POST /v1/web/command` | any host command as JSON `{type:'SessionList', ...}` except `Shutdown` and `WebLaunch`; requires an exact `Origin` and `X-CSRF-Token` |
| `POST /v1/web/attachment` | JSON `AttachmentPrepare` only; same cookie/Origin/CSRF checks before buffering, 12 MiB request cap |
| `POST /v1/web/voice?request_id=...[&partial=1]` | raw mono 16 kHz PCM16 WAV for transcription ([voice.md](voice.md#flow)); same auth checks; 8 MiB cap (commands cap at 1 MiB) |
| `GET /v1/web/session-view?session=` | versioned snapshot (`schema_version: 1`, `seq`, `view`) from `HostFacade.web_snapshot` |
| `GET /v1/web/session-events?session=&from_seq=` | SSE `view` frames of JSON-Pointer ops (`add`/`replace`/`remove`/`append`) or `resync: true` |
| `GET /v1/web/session-poll?session=&from_seq=&wait=` | long-poll equivalent of `session-events` (below) |
| `GET /v1/web/workspace-events` | SSE `workspace` frames with the session list (polled every 0.5 s) |
| `POST /v1/web/logout` | drop the cookie session |

API responses carry the strict CSP (`default-src 'self'; script-src 'self'; ...;
frame-ancestors 'none'`), `no-store`, `nosniff`, `no-referrer`,
`X-Frame-Options: DENY` and a `Host` check; only `/app/` relaxes it
([security.md](security.md)). Static files and API are one origin, so no CORS.
Tests: `tests/test_web_transport.py` (routes, auth, CSRF, redirects, snapshot and
patch transport), `tests/test_host_web_nsh.py`.

## `session-poll`: long-poll patch transport

`GET /v1/web/session-poll?session=ID&from_seq=N&wait=S` is a generic,
client-neutral transport (module `nexus/host/web_poll.py`) for clients that
cannot read a streaming body (NSH's Rust `fetch` returns whole bodies). Same auth
as the other GET routes. `from_seq >= 0`; `wait` is clamped to 0-25 s (default 25).
It answers `{"schema_version":1,"session":ID,"frames":[...]}` with the frames
`subscribe_web` yields (at most 64 frames / 4 MiB per response, after waiting for
the first one and 30 ms for simultaneous ones); empty `frames` means timeout. A frame
over 256 KiB becomes the same `resync: true` stub as in SSE, and a facade error or
an ended subscription answers a `resync` frame (after any buffered frames).

One subscription (feed) lives per (browser session, session id) across polls and
a background pump buffers frames (8 MiB cap). A subscription attaches a viewer, and
attach/detach are presence events in the durable log: subscribing per request would
write events on every round and each response would wake the next poll in a loop (seen
in testing). `from_seq` is also the acknowledgement: frames up to it are dropped,
the rest are resent, so a lost response is refetched. A cursor older than the buffer
starts a new feed from that cursor; a feed idle 60 s is closed; shutdown closes all.
Clients still re-snapshot on `resync`, with backoff.
