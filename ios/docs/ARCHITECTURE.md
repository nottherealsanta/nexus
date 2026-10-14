# iOS architecture (proposed)

Labels: **current** / **proposed** / **unverified** ([README](../README.md#labels-used-throughout)).

## Shape

```
iPhone/iPad app (SwiftUI + NexusKit)
   │  HTTPS over the tailnet (tailscale serve → loopback)       [proposed]
   ▼
Nexus gateway  (separate opt-in process, one per host)          [proposed]
   │  Unix domain sockets, existing UDS protocol                [current transport]
   ▼
Per-workspace Nexus daemons (HostFacade, reducer, storage, tools, credentials)
```

## Ownership

| Concern | Owner | Status |
| --- | --- | --- |
| Reducer (events → `ConversationView`), session storage, tools, permissions, provider credentials | Host daemon | **current** |
| Presentation projection (stable ids, JSON patches) | Host, Python (`host_support/browser_view.py`) | **current** code, **no remote export** ([CONTRACT_GAPS](CONTRACT_GAPS.md)) |
| Device identity, pairing, workspace routing, command allowlist, rate/size limits | Gateway | **proposed** |
| Rendering, local draft, Keychain, mic capture, WebRTC peer | App | **proposed** |

Rules:

- The Swift app does **not** reimplement the reducer. It applies host-produced
  snapshots and patches and renders them.
- The gateway does **not** keep an authoritative reducer of its own. It routes
  and filters; presentation comes from the host.
- Credentials (provider keys, ChatGPT OAuth) never leave the host. See
  [security.md](../../docs/security.md#boundaries).
- The server never runs on iOS. No on-device model, tools or session storage.

## Why a gateway

The existing HTTP/SSE listener ([http_sse.py](../../nexus/host/transports/http_sse.py))
is **current** loopback-only: it refuses non-loopback binds at construction, needs a
bearer token and a mandatory exact `Origin` on every request. Widening it would
weaken a documented boundary ([host.md](../../docs/host.md#httpsse-and-the-browser)).
The daemon UDS is `0600` in a `0700` dir and is per workspace
([host.md](../../docs/host.md#daemon)). A separate gateway process, started only
by explicit opt-in, can authenticate devices and fan out to workspace daemons
without touching either boundary. Details: [GATEWAY](GATEWAY.md).

## App modules (proposed)

| Module | Responsibility |
| --- | --- |
| `NexusKit.Transport` | HTTPS client, SSE reader with reconnect, request signing |
| `NexusKit.Models` | `Codable` mirrors of host results and the presentation snapshot |
| `NexusKit.Projection` | snapshot store + patch applier (custom `append` op, atomic) |
| `NexusKit.Keychain` | device key and token storage |
| Features | Sessions, Conversation, Approvals, Composer, Dictation, Live |

Models are generated or hand-checked against fixtures exported from Python
([TESTING](TESTING.md)); `PROTOCOL_VERSION` (currently 3 in
[protocol.py](../../nexus/host/protocol.py)) is checked at connect and a mismatch
shows an update-required screen rather than guessing.

## Data flow for one conversation (proposed)

1. App lists sessions through the gateway (all authorized workspaces).
2. Opening a session fetches a presentation snapshot with a base cursor.
3. App subscribes from that cursor; the host stream yields ordered patches.
4. Sends and approvals are gateway-allowlisted commands routed to the owning daemon.
5. On any reconnect the app refetches authoritative state rather than trusting local state.

## Non-goals (v1)

No server on iOS, no direct tailnet bind of daemons, no Tailscale Funnel or any
public exposure, no APNs push, no offline execution of agent work, no settings,
provider-login or extension management from the phone.
