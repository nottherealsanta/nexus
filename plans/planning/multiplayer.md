# Multiplayer Nexus

Status: design proposal, still under discussion; not implemented. Moved to `plans/planning/` on 2026-11-12 (user). The conversation is in
[multiplayer-discussion-log.md](multiplayer-discussion-log.md), which ends with "Where to pick up".

## Goal

Let people collaborate on independently hosted Nexus installations. A hosts the
runtime; B connects directly to A and works there. Nexus-operated infrastructure
may help with discovery, but must not host users' sessions, agents, credentials or
runtime traffic, or be required to keep an established collaboration running.

## User requirements

- A provides an execution runtime inside a container or VM, not A's host shell.
- B can request a new container or VM for a new worktree, subject to A's approval
  or configured policy.
- B can use A's agent, A's provider access and A's agent subscription.
- Alternatively, B can bring their own agent and subscription by connecting B's
  Nexus to A's Nexus.
- These are seamless session choices, not separate multiplayer product modes.
- B connects directly to A. A must approve B's request to join.
- A can share a link to initiate the connection.
- Discovery infrastructure may be hosted later, potentially at
  `nexus.santa.build`. It is not part of the initial requirement.
- Disconnections must not destroy sessions or accepted work. B must be able to
  reconnect and continue.
- Interactive work requires a sufficiently good connection to A's runtime.

## Two pillars (user, 2026-11-12, voice)

Multiplayer has two parts. The rest of this file is almost entirely the second one.

1. **Voice: real-time communication** between the players.
2. **Sharing** a session or a project.

### Voice (new; nothing designed yet)

Real-time voice between A and B while they work on the shared session. Not covered anywhere below.
Open questions:

- Transport: peer to peer between the two Nexus installs, or through a relay. The rule below still holds: Nexus-operated
  infrastructure must not carry session traffic, and voice is session traffic.
- Whether voice is part of the invitation (one string joins the session and the call) or a separate step.
- The timeline rule: the timeline shows what the model receives. Human-to-human audio is not model context, so it must
  either stay out of the timeline or be shown as an explicit, labelled event. Decide before building.
- How it relates to the Manager's voice control ([28](../28-manager-indicator.md)): two different voice paths, so the UI must
  make clear which one is listening.
- Mobile is a cut-down client; voice on mobile needs the user's approval (see AGENTS.md).

### Sharing: container, tailcat address, link or QR (user, 2026-11-12)

What the user described:

- A shares a **session or the whole project**. To be shareable it **must run in a Docker container** (the "any container"
  case below is the same rule: any OCI-style container, not only Docker). This matches "A provides an execution runtime inside a
  container or VM" in the requirements.
- A's project container is shared with **B's container, or other containers**. (Heard from voice: whether B's work runs in
  A's container, or B gets their own container joined to A's project, is not settled. See open questions.)
- **B must have Nexus installed.**
- **tailcat is the sharing mechanism** (the voice transcript spelled it "Talecat"; confirmed as Tailscale's tailcat). It provides a **base64 string**, the "tailcat address". A gives B that string, and B pastes it into
  Nexus to join.
- The string can be offered three ways: **pasted text, a QR code, or a link**. The link would be on `nexus.santa.build`
  (**later**; the install site is discussed in [install-site.md](install-site.md)). The string alone must work with no hosted
  site, in line with "optional discovery" below.

#### What tailcat is (checked 2026-11-12)

[tailcat](https://github.com/tailscale/tailcat) (Tailscale, released 2026-08-31, [blog](https://tailscale.com/blog/tailcat)) is
"netcat over Tailscale's data plane, without Tailscale's control plane". A Go library and CLI.

- **No account, no control plane, no root, no TUN device.** It is userspace WireGuard plus a gVisor TCP/IP stack, so it
  works inside an unprivileged container.
- **The address is `tc` + base64(CBOR)** of the server's WireGuard public key, a path-discovery key, a pre-shared key and
  the DERP relay info (about 140 bytes with a region number, longer with `--full-address`). It is exchanged out of band, so
  a pasted string, a QR code or a link all work.
- **Connection:** the client reaches the server through a DERP relay, then NAT traversal upgrades to a direct UDP
  connection when it can. If it cannot, traffic stays on the relay. Tailscale's free relays are rate-limited; you can run
  your own `derper` and embed its hostname in the address, so Nexus need not depend on Tailscale's relays.
- **Server side** runs `tailcat serve <ports>` (or the Go library) and prints the address; the client runs
  `tailcat forward <addr> 18080:8080` to get ordinary local ports, or `tailcat ssh <addr>`. Services include port forward,
  ssh, exec per connection, files, ping and perf.
- **The address is a bearer secret.** Knowing it is what lets a client connect. By default each server run makes a fresh
  ephemeral key and the address dies with the process (single use per run). Saved keys keep the address stable but then
  anyone who ever had it can reconnect. `serve --allow=<client node key>` restricts the tunnel to known clients.
- A browser build (WASM) exists, relayed over DERP only until WebRTC support lands.

#### What this means for Nexus (agent's reading, not decided)

- **tailcat is a transport and a ticket, not an admission system.** It fits "direct connection" and "invitation" below, and
  it removes the need for hosted discovery for the NAT problem: the string alone is enough to connect. This answers part of
  "Networking constraints": tailcat is a better first transport than "reachable HTTPS endpoints", and the DERP relay only
  carries WireGuard-encrypted packets. Still, a relay is third-party infrastructure on the path (see the rule at the top);
  the plan should say Nexus uses a relay the user can choose (Tailscale's, or their own `derper`).
- **A's Nexus (or the container) runs the listener; B's Nexus is the client.** Nexus should embed the tailcat Go library or
  ship the binary, started by A's Core, serving the Nexus API port only. B's Nexus then reaches it as a local port.
- **Never `no-auth-ssh`, and never expose a shell.** Its own docs warn the address would be the credential. The Nexus
  admission step (A approves B, scoped membership) must sit on top. Preferably use `--allow` once B's node key is known, so
  the pasted address is only good for the first knock.
- **Container:** the listener can run inside A's container, since it needs no root and no TUN. That fits "must be in a
  container" and the "any container" wording.
- **Voice:** tailcat can carry UDP (`DialUDPPort`, 1232 byte payloads), so a voice stream could ride the same tunnel. Not
  decided; see the voice questions above.
- **Mobile:** no tailcat build for iOS is mentioned. The browser WASM build is relayed only. Check before promising joins
  from a phone.

#### Cautions

- tailcat is new (2026-08-31) and the library API may change. Pin a version.
- Tailscale's default relays are throttled, and the project says to contact them for heavy use. Plan for a self-run `derper`.
- Licence and supply chain of the Go dependency are not checked yet.

How it fits the existing model (agent's reading, not decided):

- The tailcat address is the **invitation** in the join flow below: "permission to knock, not permission to enter". It is
  not the membership. A still approves B, and A still issues a scoped, revocable membership.
- The string must therefore carry only what B needs to find and authenticate A (A's pinned cell identity and a connection
  hint), a scope, an expiry and a one-use or limited-use token. It must not carry credentials, provider access or secrets.
- Treat the string, QR code and link as three encodings of one invitation. Anyone who sees a QR code or link can try to
  use it, so expiry, single use and A's approval carry the security.

Open questions from this idea:

- Embed the tailcat Go library in Core, or ship the `tailcat` binary? Which relay: Tailscale's, or our own `derper`?
- Single-use ephemeral address per invitation (default), or a saved key with `--allow`? Does A's Nexus start a new listener per invitation?
- The address pins A's WireGuard key, so a link on `nexus.santa.build` that merely carries the address cannot redirect B to an impersonator. Check that the link form (for example `https://nexus.santa.build/j#tc...`, with the address in the fragment so it never reaches the server) keeps that property.
- How the string is entered: a `nexus join <string>` command, a paste field in both TUI and NSH (parity rule applies),
  scanning the QR with the phone camera. Mobile is a cut-down client, so joining from mobile needs approval.
- What happens when the string is re-used, leaked or expired.

### Answers and first step (user, 2026-11-12, voice)

| Question | Answer |
| --- | --- |
| B's container | **Both shapes should be possible. The user wants to think about it more.** (1) B connects to A's container and works inside A's runtime, optionally using A's agent. (2) A and B each work in their own container under the same project, and each can watch the other's container working. |
| Session or project | **The whole project.** |
| Where to start | **B connects to A's container and works there, with either B's own agent or A's agent.** The user asked for advice on this. |

Agent's advice (not decided):

- **The starting point is right, and it is what the "first milestone" at the end of this file already describes.** Shape 1 is
  one runtime with one owner, which the rest of this file assumes: A's Nexus orders the runtime's operations and holds the
  authoritative state.
- **Build it in two stages even though both agent choices are wanted.**
  1. *A's agent, B as a remote client.* B's Nexus (or a thin client) attaches to A's Core over tailcat and uses A's sessions,
     A's agent and A's provider access. Nothing new is needed in the agent loop; this is mostly transport, admission and
     attribution. It proves the invitation, approval, reconnect and revoke path.
  2. *B's agent against A's runtime.* B's Nexus runs B's loop and sends tool calls to A's container. This needs the
     cross-host operation protocol (stable operation IDs, reconnection, results returning to B's context), so it is the
     harder half. It should come second, and its context rules ("no host switch may silently inject hidden context")
     deserve their own design pass.
- **Give B their own worktree in A's container, not A's working directory.** The plan already prefers separate worktrees
  and explicit merges over two agents writing to one directory. This is cheap now and it is the main protection against
  A and B clobbering each other.
- **"Whole project" means the invitation scope is the project.** B can see every session in it. Per-session scopes can come
  later. Because that includes A's agent, A's spend is exposed: the first version needs an explicit A-side switch for
  "B may run A's agent" and a spending or turn limit, off by default. B's own agent costs A nothing but still runs
  commands in A's container, so it needs the same approval.
- **Keep shape 2 possible; do not build it yet.** The one design rule that keeps the door open: a *runtime* has an owner, a
  *project* does not. A project is a set of runtimes that may belong to different cells, and "A watches B's container" is
  a read-only view of a runtime that another cell owns, using the same invitation and membership. So do not hard-code
  "A is the host and B the guest" in names, tables or the protocol; use "runtime host" and "member".
  Shape 2 needs extra things shape 1 does not: how code moves between the two containers (git), what each side sees of the
  other's agent context (the timeline rule), and what "same project" means when two cells each hold a copy. Decide those
  when the user returns to it.
- **Voice is independent of all of this** and can follow once the tunnel exists.

## Model: runtime host and agent host

Separate where work executes from where the agent runs and who supplies provider
access.

| Property | Example |
| --- | --- |
| Runtime host | A's Nexus |
| Runtime | Project X / worktree Y / container Z |
| Agent host | A's Nexus or B's Nexus |
| Agent configuration | A's build agent or B's custom agent |
| Provider access supplied by | A or B |
| Participants | A and B |

A owns the runtime and enforces its access policy regardless of which Nexus hosts
an agent.

### Use the host's agent

B participates in a session hosted by A's Nexus:

- A's Nexus runs the agent using A's provider access.
- Tools execute inside the assigned container or VM.
- A controls permissions, resource quotas and spending limits.
- B's client disconnecting does not stop the agent.
- B needs a compatible client, but does not need to operate their own Nexus
  daemon for this case.

### Bring your own agent

B's Nexus joins A's Nexus through an approved connection:

- B's Nexus runs B's agent using B's provider access.
- A's Nexus exposes only authorized runtime and workspace operations.
- Tool requests go to A; results return to B's agent.
- B's provider credentials remain on B's Nexus.
- B's Nexus may run on B's laptop or on a server B owns; it need not run on the
  device displaying the session.

This is the proposed first architecture for bringing a subscription. Hosting B's
agent on A while retaining B's provider access would require a separately defined
credential or gateway arrangement; it is not assumed here.

### Seamless, but explicit

The common session choices can be “Use host's agent” and “Use my agent.” The
runtime and collaboration remain the same.

Do not silently switch billing or authority. If B's agent host disconnects, Nexus
must not fall back to A's subscription without permission. Moving a session
between agent hosts requires an explicit context handoff; automatic live migration
is not assumed.

## Isolated runtimes and worktrees

A shared project may have multiple worktrees, each with its assigned isolated
runtime. B may request another worktree and runtime. A's policy determines whether
the request is automatically accepted, requires approval or exceeds a quota.

Runtime policy must cover:

- Filesystem mounts and access outside the worktree.
- Network access and exposed services.
- Secrets and provider credentials.
- CPU, memory, storage and runtime limits.
- Runtime creation, shutdown and deletion authority.

A worktree alone is not a security boundary. Containers may be suitable for
trusted collaboration; untrusted participants may require VMs, microVMs or a
stronger sandbox. The isolation implementation remains open.

## Direct connection and admission

An invitation is permission to knock, not permission to enter.

Proposed join flow:

1. A creates an invitation with a scope and expiry.
2. A shares its link with B.
3. B opens it and establishes a connection to A.
4. B presents an identity and requests admission.
5. A approves or rejects the request.
6. A issues B a scoped, revocable membership.

Peer identity must be stable across address changes and daemon restarts. Peers
must authenticate each other independently of the discovery service; for example,
an invitation can pin A's cell identity. Discovery must not be able to redirect B
to an impersonator merely by changing an address.

Membership is distinct from a network connection. A temporary disconnect should
not require a fresh invitation, while an expired or revoked membership must not
be restored by reconnecting.

### Optional discovery

A future service, potentially at `nexus.santa.build`, may resolve invitations to
connection information or help exchange connection candidates.

- It is optional and replaceable.
- A manually supplied address must work without it.
- It does not own session state or authorize membership.
- It does not carry session, agent or runtime traffic.
- Established connections do not depend on its continued availability.

Discovery may still expose connection metadata; its privacy and retention policy
will need definition if the service is built.

### Networking constraints

Direct connectivity cannot be guaranteed across every NAT, firewall or corporate
network without a relay.

Suggested initial transports:

- Reachable, authenticated HTTPS endpoints.
- LAN connections with authenticated peers.
- Private overlay networks such as Tailscale.
- tailcat (see "What tailcat is" above): direct WireGuard with NAT traversal and a DERP relay fallback.

NAT traversal can follow later. If direct connectivity fails, report that clearly
rather than silently routing private traffic through Nexus-operated infrastructure.
An optional user-operated relay can be considered separately.

## Durability and reconnection

The connection is temporary; the collaboration and accepted work are durable.
Disconnecting means detaching, not cancelling.

A retains:

- Runtime and worktree identities.
- Accepted operations, lifecycle state and recorded results.
- Memberships and revocations.
- Pending approvals and questions that A owns.
- Authorized event history or checkpoints sufficient for reconnection.

When B hosts the agent, B retains its agent session state. Session handoff and
history exchange must preserve the canonical ordered context rather than create
competing versions of it.

### Operation protocol

1. The caller submits an operation with a stable unique ID.
2. The runtime host durably records acceptance before acknowledging it.
3. The host records progress and results.
4. After disconnection, the caller reconnects using its last acknowledged event
   position and queries outstanding operation IDs.
5. Retrying the same ID returns the existing operation rather than creating a
   second runtime or executing another copy of the command.

A missing response is not proof of failure. Potentially destructive work must be
reconciled before retrying. Stable IDs prevent duplicate submission; they do not
make arbitrary external side effects exactly-once or automatically resumable.

Build on Nexus's existing durable event log and mutation journal where practical,
rather than introducing an independent session history. Replay retention and
checkpoint behavior must be explicit, including how a client catches up after its
cursor is older than retained events.

### Failure behavior

| Failure | Expected behavior |
| --- | --- |
| B's viewing client disconnects | The agent continues if its host and required runtime remain connected. |
| B's Nexus disconnects while hosting the agent | A may finish accepted runtime jobs; further agent steps wait for B's Nexus. |
| A's runtime becomes unreachable | Remote operations pause; the agent reconciles outstanding operations after reconnecting. |
| A's daemon restarts | Recover recorded state and identify interrupted execution; do not pretend every process resumed. |
| A's machine disappears | Wait for A unless an explicit replication or failover arrangement exists. |

Durable records do not guarantee that shell processes survive a daemon or machine
restart. Interrupted deployments and other external effects may require manual
reconciliation rather than an automatic rerun.

## Authority and safety

Effective agent authority is the intersection of:

```text
A's grant ∩ B's delegation ∩ the agent's allowed tools
```

B's agent instructions cannot override A's policy. B's Manager does not
implicitly acquire administrative authority over A's Nexus.

Permissions should distinguish:

- Viewing sessions.
- Contributing messages or proposed changes.
- Running agents and runtime operations.
- Creating worktrees and runtimes.
- Approving privileged actions.
- Administering the cell.

A may revoke membership. The effect on already accepted work must be defined
explicitly: denying future requests alone is different from stopping running jobs
or withdrawing access to mounted secrets.

Proposed default: invitations and memberships are not transferable. Delegation to
a third person, if supported later, requires an explicit grant.

Provider access must use supported authentication routes and comply with the
provider's terms. Do not assume every consumer subscription permits shared use,
remote hosting or credential forwarding. Keeping credentials on the owner's Nexus
is a useful boundary, but does not itself establish that a usage pattern is
supported.

## Session ownership and context

Start with one authoritative owner for each session and runtime resource, not a
multi-master synchronized Nexus database.

- The runtime host orders runtime operations and their events.
- The agent host owns the canonical agent session and its ordered context.
- When the hosts differ, tool operations and results carry stable references so
  reconnecting does not lose or duplicate context.
- Participants see the canonical history they are authorized to access, with
  attribution for messages, actions and approvals.
- No host switch may silently inject hidden context or omit tool results.

During a partition, each host may continue only work already authorized and
possible with its available dependencies. A locally drafted request is not an
accepted remote operation. Requests submitted after reconnection are checked
against current permissions.

For code collaboration, prefer separate worktrees and explicit merges over
concurrent unsynchronized writes to one directory.

## Role of `celld`

A cell is an independently operated Nexus environment with a stable identity,
local state, runtime resources and its own access policy.

`celld` is a possible name for the peer connectivity and admission layer around
Nexus Core. It is not a Nexus-operated central server and should not become a
second owner of session state.

Whether this layer is embedded in Core or shipped as a separate executable is an
implementation decision to make later.

## Suggested first milestone

Demonstrate this experience without hosted discovery:

1. A shares an invitation using a directly reachable address.
2. B requests admission and A approves.
3. B works in an isolated runtime on A using A's agent and provider access.
4. B requests a new worktree and runtime under A's policy.
5. B connects their own Nexus and starts a session using B's agent and provider
   access against A's runtime.
6. Disconnect and reconnect B's client and Nexus independently, verifying durable
   operation reconciliation, context continuity and pending approvals.
7. A revokes B's membership and the defined revocation policy is enforced.

## Open design questions

- Identity creation, verification, recovery and key rotation.
- Invitation encoding, expiry and replay protection.
- Container versus VM isolation and supported runtime hosts.
- Precise membership scopes, quotas and approval policy.
- Cross-host session/event protocol and retention guarantees.
- Revocation behavior for running work and mounted secrets.
- Supported provider authentication and subscription arrangements.
- Explicit agent-host handoff, if needed beyond starting a new session.
- Direct transport and endpoint setup before hosted discovery exists.
