# Discussion log: multiplayer

Newest last. This is the record of what was asked and answered, so we can come back to it. The working draft is
[multiplayer.md](multiplayer.md). Decisions are marked **(user)**; everything else is the agent's proposal and still open.

## 2026-11-12, first message (voice)

The user asked to add a new idea to the existing multiplayer discussion (`plans/multiplayer.md`, later moved here).
Multiplayer has two parts **(user)**:

1. **Voice**: real-time communication between the players.
2. **Sharing** a session or the project itself.

Sharing, as described **(user)**:

- A's session or project must run in a container (Docker, or any type of container) to be shareable.
- A shares the project with B's container or other containers.
- B has Nexus installed.
- The sharing mechanism is "Talecat" (voice spelling), which gives a base64 string. A gives B the string and B puts it into
  Nexus. It can also be a QR code or a link.
- The link would be on `nexus.santa.build`, but that is for later.

Agent: added a "Two pillars" section to the draft. Voice had nothing designed, so it got open questions only.

## 2026-11-12, second message

The user asked for the file to move to `plans/planning/`. Done with `git mv`; the planning README and `plans/README.md`
point to it.

## 2026-11-12, third message: "tailcat, search"

The agent searched and found that the tool is **tailcat** (Tailscale, released 2026-08-31, `github.com/tailscale/tailcat`), a
netcat-style tool over Tailscale's data plane with no account or control plane. Its address is `tc` + base64(CBOR) of the
server's keys and relay info. Written into the draft under "What tailcat is" and "What this means for Nexus". Key points:

- tailcat is a transport and a bearer-secret ticket. It is **not** an admission system, so A's approval of B still sits on top.
- It runs unprivileged inside a container (userspace WireGuard, no TUN device).
- A relay (DERP) is on the path. Tailscale's are rate-limited; a self-run `derper` can be embedded in the address.
- Never use its no-auth shell service. Prefer `--allow` once B's key is known.
- No iOS build was found; the browser (WASM) build is relayed only.
- Not checked: licence, dependency supply chain, API stability (it is new).

## 2026-11-12, fourth message (voice): answers

| Question | Answer **(user)** |
| --- | --- |
| B's container | Both shapes should be possible, but "I want to think about this more carefully". (1) B connects to A's container and works inside A's runtime, optionally using A's agent. (2) A and B each work in their own container under the same project, and each can watch the other's container working. |
| Session or project | The **whole project**. |
| Where to start | B connects to A's container and works there, with B's own agent or A's agent. The user asked for advice. |

### Agent's advice (not decided)

- The starting point is right and matches the draft's first milestone.
- Two stages: (1) A's agent with B as a remote client (transport, admission, attribution, reconnect, revoke); (2) B's agent
  running tool calls in A's container (needs the cross-host operation protocol and its own design pass).
- Give B their own worktree in A's container, not A's working directory.
- "Whole project" exposes A's agent and spend: "B may run A's agent" is off by default, with a turn or spend limit. B's own
  agent still runs commands in A's container, so it needs approval too.
- Keep shape 2 possible without building it: a runtime has an owner, a project does not. Use "runtime host" and "member",
  not "host" and "guest", in names, tables and the protocol.

## 2026-11-12, fifth message: "write all this to the plan, will continue later"

Everything above is in [multiplayer.md](multiplayer.md) and this log. Nothing was built, and no other repo was touched.

### Where to pick up

1. **Shape 2** (A and B each in their own container, same project, watching each other): the user wants to think more. Open:
   how code moves between containers, what each side sees of the other's agent context (the timeline rule), what "same
   project" means when each cell holds a copy.
2. Confirm the staging (A's agent first, then B's agent) and the A-side switch and limit for A's agent.
3. tailcat choices: embed the Go library or ship the binary; Tailscale's relay or our own `derper`; one ephemeral address per
   invitation or a saved key with `--allow`.
4. Voice: transport, whether it shares the invitation, and how it appears (or not) in the timeline.
5. Joining from mobile: tailcat has no iOS build found, and mobile additions need the user's approval.
6. Check tailcat's licence and dependencies before relying on it.
