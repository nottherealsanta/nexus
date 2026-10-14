# Nexus for iPhone and iPad (planning only)

> **Status: planning only.** This folder holds design documents. There is no
> Xcode project, no Swift code, no build, no gateway and no test for any of it.
> Nothing here is implemented or has been run on a device. The Nexus server
> **never runs on iOS**; the app is a remote client of a Nexus host (a Mac or
> Linux machine) that the user already runs.

The goal: an iPhone/iPad app that connects to the machine running Nexus
(typically over Tailscale), shows every authorized session across workspaces,
renders a session's structured conversation, lets the user send messages and
answer approvals, and later adds dictation and GPT Live voice. Almost all
behavior is routed to Nexus core; the app renders and relays.

## Source-of-truth rule

The repository docs and code are authoritative: see [docs/README.md](../docs/README.md),
[host.md](../docs/host.md), [security.md](../docs/security.md),
[events-and-view.md](../docs/events-and-view.md), [surfaces.md](../docs/surfaces.md),
[voice.md](../docs/voice.md), [live.md](../docs/live.md). These iOS docs are scoped
design. Where they disagree with the backend, **the backend wins** and these docs
get fixed.

## Labels used throughout

| Label | Meaning |
| --- | --- |
| **current** | Verified in this repo's code/docs when written |
| **proposed** | Design in this folder; nothing exists |
| **unverified** | An assumption about an external system or device, not tested |

Gateway command names, routes and file names in these docs are sketches, not existing commands.

## Documents

| Doc | Read it for |
| --- | --- |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | components, ownership, what stays on the host |
| [GATEWAY](docs/GATEWAY.md) | the proposed opt-in gateway, routing, allowlist, bounds |
| [SECURITY](docs/SECURITY.md) | pairing, device auth, Tailscale exposure, revocation |
| [CONTRACT_GAPS](docs/CONTRACT_GAPS.md) | what the host lacks today for a phone client |
| [UX](docs/UX.md) | sessions list, conversation, approvals, composer, accessibility |
| [VOICE_AND_LIVE](docs/VOICE_AND_LIVE.md) | dictation and GPT Live on iOS |
| [ROADMAP](docs/ROADMAP.md) | phases and acceptance criteria |
| [TESTING](docs/TESTING.md) | test plan; none executed yet |
| [DECISIONS](docs/DECISIONS.md) | defaults, rejected options, blockers |

Suggested order: ARCHITECTURE, CONTRACT_GAPS, GATEWAY, SECURITY, then the rest.

## Proposed layout (does not exist yet)

```
ios/
  README.md, docs/              # this planning set
  project.yml                   # XcodeGen spec (later)
  Packages/NexusKit/            # Swift package: transport, models, SSE + patch applier, Keychain
    Sources/NexusKit/  Tests/NexusKitTests/  Fixtures/   # fixtures exported from Python
  App/                          # SwiftUI shell
    Features/{Sessions,Conversation,Approvals,Composer,Dictation,Live}/
  UITests/                      # mock-gateway UI tests
```

Baseline: SwiftUI, iOS/iPadOS 17, Swift concurrency. No code until the host and
security contract in [ROADMAP](docs/ROADMAP.md) P1 is approved.
