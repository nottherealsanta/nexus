# Contract gaps: what the host lacks for a phone client

Labels: **current** / **proposed** / **unverified**. Everything in the "current"
column was read from the code named; the gap and fix columns are proposals.

## 1. Presentation contract is not remotely reachable

| Fact (current) | Where |
| --- | --- |
| `SessionState` returns `SessionStateResult(session, seq, view)` where `view = view.to_dict()` of the reducer | [protocol.py](../../nexus/host/protocol.py), `HostFacade._dispatch` in [facade.py](../../nexus/host/facade.py) |
| That established projection **omits reducer reconciliation ids**. The browser contract adds them | docstring of `HostFacade.web_snapshot` |
| `HostFacade.web_snapshot` and `HostFacade.subscribe_web` produce the versioned browser snapshot (`schema_version: 1`, `seq`, `view`) and ordered JSON patches, using the Python projection `web_view` / `json_patch` | [facade.py](../../nexus/host/facade.py), [browser_view.py](../../nexus/host_support/browser_view.py) |
| They are reached only by `BrowserRoutes` (`/v1/web/session-view`, `/v1/web/session-events`), which require the cookie + CSRF browser session | [web.py](../../nexus/host/web.py) |
| The UDS transport handles `CommandFrame`s via `facade.handle` and, for `SessionSubscribe`, streams raw `Event` frames via `facade.subscribe`. It does **not** call `web_snapshot` or `subscribe_web` | `_command` / `_stream` in [daemon.py](../../nexus/host/daemon.py) |
| The peer HTTP routes `POST /v1/command` and `GET /v1/events` likewise expose commands and raw events only | [http_sse.py](../../nexus/host/transports/http_sse.py) |

**Consequence.** Do not assume `web_view(SessionStateResult.view)` rebuilds the
browser contract: the input already lost the ids. A gateway cannot get the
presentation stream from today's UDS or peer HTTP.

**Proposed fix (needs host change, new commands):** add explicit host
presentation commands and a stream contract, e.g. a snapshot command returning
what `web_snapshot` returns and a stream attach that yields what `subscribe_web`
yields, over UDS. Factor the existing Python projection; do not fork it.
Swift does not duplicate the reducer; the gateway adds no independent reducer.
Command names here are sketches.

## 2. Patch format is not generic RFC 6902

`json_patch` emits `add`, `remove`, `replace` and a custom **`append`** (string
suffix) op. Array growth uses `add` at `/-`; pointer parts escape `~`→`~0`, `/`→`~1`
([browser_view.py](../../nexus/host_support/browser_view.py)). A stock JSON Patch
library will mis-apply `append`.

Required of the Swift applier (proposed): implement the four ops explicitly;
JSON Pointer escaping; **bounded** pointer depth and op count per frame; atomic
application (apply to a copy, swap on success, otherwise resync). Golden fixtures
are exported from Python ([TESTING](TESTING.md)). The web twin is
`ui/web/js/projection.js` (`applyOperations`, atomic on a detached copy), documented in [web.md](../../docs/web.md).

## 3. Sequence numbers are monotonic, not contiguous

`seq` is monotonic per session; message/snapshot records share the sequence, so
gaps alone do **not** prove missed events ([events-and-view.md](../../docs/events-and-view.md),
`subscribe_web` docstring). `Session.subscribe` heals dropped bus messages from
the log before yielding the next event (**current**).

Also **current**: `subscribe_web` yields one frame per event after the cursor, and
its `ops` may be empty if the projection did not change. So the authoritative
stream can legitimately advance the cursor with no ops.

Proposed, needs a versioned spec:
- snapshot carries `base_seq`; each patch frame carries `base_seq → seq`;
- client rule: a patch applies only if its base equals the client's cursor;
  otherwise resync (refetch snapshot), never infer loss from a gap;
- snapshot+subscribe race: subscribe from `base_seq`; the host replays from the
  log and drops frames `<= cursor`; define replay-log retention and what the host
  says when `from_seq` is older than retention;
- an explicit `resync` frame (**current** in the browser route when a frame
  exceeds 256 KiB or a stream errors) becomes part of the spec.

## 4. Cross-project listing and open

| Fact (current) | Gap |
| --- | --- |
| `ProjectSessionsList` returns up to 1,000 sessions with `workspace`, `project_id`, `repo`, `worktree`; `truncated` is explicit; it excludes child, archived and trashed sessions ([host.md](../../docs/host.md#cross-project-session-navigation)) | No paging; a phone must show truncation, never silently drop. Archived/trash need `SessionListArchived` per workspace and an explicit UI scope |
| `ProjectSessionOpen` returns `socket_path` or a one-use **local** browser `url` | Unusable on a phone (local socket / `127.0.0.1`). The gateway must route by opaque workspace id instead |
| Each workspace has its own daemon, runtime, settings and permissions | "All sessions" means all **authorized registered** workspaces, aggregated by the gateway |

## 5. Write idempotency

No existing command carries an idempotency key (**current**; none assumed).
Proposed: gateway-level keys for `SessionStart`/`SessionEnqueue`/`PermissionResolve`,
bounded retention, safe expiry semantics (an expired key must not replay as a
fresh send). Until then the app never auto-retries non-idempotent writes
([UX](UX.md#drafts-and-retries)).

## 6. Uploads

`VoiceTranscribe` carries audio as `bytes` inside a command, but commands are
capped at 1 MiB (`DEFAULT_MAX_BODY_BYTES`); the browser voice route allows 8 MiB
(`MAX_WEB_VOICE_BODY_BYTES`) and attachments 12 MiB, both only on the cookie path
([http_sse.py](../../nexus/host/transports/http_sse.py)). A phone needs a proposed
authenticated **binary upload** route in the gateway that reuses the same limits
and the same host validation. See [VOICE_AND_LIVE](VOICE_AND_LIVE.md).

## 7. Other

- Web docs mark the browser surface "to be deprecated"
  ([web.md](../../docs/web.md)); the presentation contract should be promoted to
  a host-owned one, not left as a browser-only detail.
- Approval arbitration is `Presence` first-responder, counting views not
  identities ([host.md](../../docs/host.md#supervisor-and-presence)); per-device
  attribution would be new.
- Tool-result clipping: current projections clip text at `MAX_TEXT` with an
  ellipsis ([browser_view.py](../../nexus/host_support/browser_view.py)); a
  detail-load command for clipped content is **not verified** to exist for tool
  results. The app must show clipping and offer load only when the host supports it.
