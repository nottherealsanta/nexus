# iOS decisions (proposed defaults and blockers)

Scoped to this folder. When a decision ships, record it in the repo's
[decisions.md](../../docs/decisions.md). Status: **default** = proposed choice
awaiting approval; **blocker** = must be resolved before the dependent phase.

## Defaults

| # | Decision | Default | Why / rejected alternative |
| --- | --- | --- | --- |
| D1 | UI toolkit | SwiftUI native, iOS/iPadOS 17 baseline, Swift concurrency | Rejected: wrapping the web client (browser surface is marked to be deprecated, cookie/CSRF model, no native audio) |
| D2 | Code organization | one `NexusKit` Swift package + app; XcodeGen **later** | no project files until P1 prerequisites land |
| D3 | Where logic lives | host keeps reducer, storage, tools, credentials; app renders host projection | Rejected: Swift reducer (drift risk); gateway reducer (second authority) |
| D4 | Remote access | separate opt-in gateway over UDS | Rejected: widening HTTP/SSE beyond loopback; direct tailnet binds in v1; raw UDS tunnel |
| D5 | Network exposure | gateway on loopback + `tailscale serve` TLS | Rejected: Funnel/public exposure |
| D6 | Authentication | per-device keys, short-lived audience-bound tokens, even inside tailnet | Rejected: "tailnet membership is auth"; a shared static bearer token |
| D7 | Pairing | QR URL + single-use ~120 s secret | algorithm/pin policy deferred to security review |
| D8 | Device secrets | Keychain, this-device-only; Secure Enclave when available, else a software-backed Keychain key (weaker, not hardware-bound; pending review) | no promise in Simulator; no blanket cert pinning (Tailscale cert rotation) |
| D16 | Gateway topology | one gateway per host, routing to each workspace daemon over UDS | one gateway avoids a second routing authority per host; lifecycle, discovery and pairing-store location stay open (see [GATEWAY](GATEWAY.md#open-questions)) |
| D17 | P1 session creation | P1 includes `SessionStart` (create) as well as opening existing sessions; the first slice is "create or open" | creation is in the P1 allowlist ([GATEWAY](GATEWAY.md#command-allowlist-per-phase-proposed)); workspace must be on the device allowlist |
| D9 | Command policy | per-phase allowlist, rechecked on every write | Rejected: denylist; read-only framing (agent can run `bash`) |
| D10 | Workspace addressing | opaque stable ids + per-device allowlist | Rejected: client-supplied paths |
| D11 | Stream cursor | snapshot `base_seq`, patch `base_seq → seq`, resync; never infer loss from seq gaps | needs a versioned spec |
| D12 | Writes | no auto-retry; idempotency keys later | |
| D13 | Dictation upload | gateway binary upload route reusing 8 MiB web voice cap | Rejected: base64 in generic 1 MiB command |
| D14 | Live | local WebRTC, daemon sideband, on-screen approvals only, experimental in P4 | no always-on promise; no APNs in first release |
| D15 | Scope of "all sessions" | all authorized registered workspaces; archived/trash explicit; truncation shown | |

## Blockers

| # | Blocker | Blocks |
| --- | --- | --- |
| B1 | Gateway design security review (process model, pairing store, trust of proxy headers) | P1 |
| B2 | Host presentation commands/stream over UDS and versioned cursor spec | P1 |
| B3 | Identity/cert plan: how the phone learns and verifies gateway identity under Tailscale-issued certs; crypto and pin policy | P1 |
| B4 | Tailscale Serve deployment validation (headers, loopback isolation, HTTPS enablement, sleep behavior) | P1 |
| B5 | Detail-load command for clipped tool content, or an accepted "clip is final" UX | P2 |
| B6 | WebRTC library distribution (build/size/license/App Review) | P4 |
| B7 | Real GPT Live service and iOS background audio behavior | P4 claims |

## Open questions

Does the gateway live in the Nexus Python package or beside it? Who owns the
phone-facing schema version? Should a trusted device be able to revoke others?
(Settled as defaults above: one gateway per host (D16); P1 includes session
creation (D17).)
