# Multiplayer Nexus

Status: design proposal; not implemented.

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
