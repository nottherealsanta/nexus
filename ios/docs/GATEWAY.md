# Gateway (proposed)

Nothing here exists. Command and route names are **sketches**, not existing
commands. Labels: **current** / **proposed** / **unverified**.

## What it is

A separate, explicitly opt-in process on the Nexus host (e.g. a sketch
`nexus gateway start`; off by default, no flag on existing commands). It
authenticates paired devices and relays an allowlisted subset of host commands
and the presentation stream to the owning per-workspace daemon **over UDS**.

It is **not** a widening of the existing HTTP listener, which stays loopback-only
with a bearer token and mandatory exact `Origin` (**current**,
[http_sse.py](../../nexus/host/transports/http_sse.py)). It adds no unauthenticated
tunnel to a raw daemon and holds no reducer, credentials or session files.

## Exposure (default proposal)

| Layer | Proposal |
| --- | --- |
| Gateway bind | loopback only |
| Remote reach | `tailscale serve` terminating TLS on the tailnet and proxying to the loopback gateway |
| Not in v1 | direct tailnet binds, Tailscale Funnel, any public exposure |

Tailscale gives connectivity, **not** a working endpoint today. Before relying on
it, validate in a real deployment (all **unverified**): which headers Serve adds
and which the gateway may trust, that only the proxy can reach the loopback port
(other local users/processes could), certificate availability and rotation,
MagicDNS/HTTPS enablement on the tailnet, and behavior when the host sleeps.
Device authentication stays required even inside the tailnet
([SECURITY](SECURITY.md)).

## Routing

- The gateway reads the registry of workspaces known to the Nexus home (the same
  shared index behind `ProjectSessionsList`, **current**) and connects to each
  workspace's daemon socket (`ensure_daemon` semantics, **current**).
- Workspaces are addressed to the phone by **stable opaque ids** issued by the
  gateway, never user-supplied paths. A device has an **allowlist** of workspace
  ids chosen from known roots at pairing time.
- Listing aggregates `ProjectSessionsList`-style results for authorized
  workspaces and reports `truncated` per source; the app never hides truncation.
- Replaces `ProjectSessionOpen`'s local socket/URL ([CONTRACT_GAPS](CONTRACT_GAPS.md#4-cross-project-listing-and-open)).
- Presentation stream requires the host change in
  [CONTRACT_GAPS §1](CONTRACT_GAPS.md#1-presentation-contract-is-not-remotely-reachable).

## Command allowlist (per phase, proposed)

Authorization is checked **again on every write** against current device
capabilities and scopes; revocation takes effect immediately.

| Phase | Allowed |
| --- | --- |
| P1 | `SessionList`/project listing, `SessionOpen`, `SessionStart`, `SessionEnqueue` (queue/steer/interrupt), `SessionCancel`, presentation snapshot + stream, `PermissionResolve`, `QuestionAnswer`, `Health` |
| P2 | `SessionQueueMove/Remove`, `ContextInspect`, `ToolsList`, `GitDiff`, `AgentTranscript`, `AgentCancel`, `ModelSelect`, `ReasoningEffortSelect`, `SessionListArchived`, archive/unarchive/restore (scope decision) |
| P3 | `VoiceStatus`, `VoiceTranscribe` via binary upload route, `VoiceCancel` |
| P4 | `LiveStatus`, `LiveConsent`, `LiveStart`, `LiveMute`, `LiveStop`, `LiveHelperEvent` |

**Denied always:** `Shutdown`, `WebLaunch`, provider credentials/login
(`ProviderKeySet`, `ProviderLogin*`, `ProviderLogout`), settings writes
(`Settings*`, `SetupSave`), extension mutation (`Extensions*`, `ContextExtensionSelect`
until reviewed), `Mock*`, `SessionDelete`/`Export` until reviewed. Unknown command
types are denied by default (allowlist, not denylist).

**A paired device is not read-only.** It can direct the agent and thereby run
`bash` under the host's normal permission engine; there is **no OS sandbox**
([security.md](../../docs/security.md)). Capabilities limit which Nexus commands
a device can send, not what an approved tool can do.

## Bounds (proposed, starting points)

Mirror the existing listener's posture ([host.md](../../docs/host.md#httpsse-and-the-browser)):
header 16 KiB / 64, command body 1 MiB, per-connection and global connection
caps, SSE queue 256 events / 4 MiB with slow-consumer drop, read 30 s / write 10 s
timeouts. Plus per-device rate limits, request-size limits by route, replay
protection for pairing and signed requests, and redacted errors only (never echo
bodies or tokens). Uploads use their own route and caps
([VOICE_AND_LIVE](VOICE_AND_LIVE.md#dictation)).

## Streams

Per device, bounded subscriptions (the daemon allows 64 per UDS client,
**current**). Revoking a device closes its streams. Gateway streams carry the
base-cursor semantics from CONTRACT_GAPS §3. A viewer attached through the
gateway counts in `Presence` like any view, so approvals and unattended policy
behave as for other clients (**current** semantics; per-device attribution is new).

## Open questions

Proposed default: one gateway per host, not one per daemon (D16 in
[DECISIONS](DECISIONS.md)); the gateway is the single pairing and routing point
for all authorized workspaces. Still open: how the gateway discovers and wakes daemons;
whether pairing state lives in the Nexus home (`0600`/`0700`, like
`live-consent.json`) or its own store; gateway lifecycle vs. idle shutdown of
daemons (**current**: 300 s with no viewers and no turns). Tracked in
[DECISIONS](DECISIONS.md).
