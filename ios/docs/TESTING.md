# Test plan (proposed)

**No test for the iOS app, NexusKit or the gateway has been written or executed.**
Repo test conventions live in [testing.md](../../docs/testing.md); the gateway and
fixture exporter would follow them (offline, no credentials, fakes).

## 1. Python: gateway and host additions

| Area | Cases |
| --- | --- |
| Pairing | secret single-use; expires at ~120 s; replay after success; wrong secret rate-limited; secret absent from logs/errors |
| Device auth | missing/expired/replayed/forged token; token for another audience or device; clock skew window; revoked device rejected on next request |
| Revocation | open SSE streams closed immediately; in-flight writes re-checked and refused |
| Authorization | every non-allowlisted command type denied (iterate the full `Command` union and assert only the allowlist passes); denied set (`Shutdown`, `WebLaunch`, `Provider*`, `Settings*`, `Extensions*`, `Mock*`); unknown workspace id; workspace outside the device allowlist; user-supplied path rejected |
| Bounds | header/body/route-specific size, connection caps, per-device rate, subscription count, slow-consumer drop does not stall a turn, upload cap enforced before buffering |
| Redaction | tokens, keys, provider credentials, absolute secret-bearing paths never in results, errors, logs |
| Presence | gateway viewer counts as a view; closing it frees approval leases; last-viewer unattended policy |
| Replay/resync | snapshot+subscribe race with concurrent events; `from_seq` older than retention; patch with wrong `base_seq` forces resync; empty-ops frame advances cursor; oversize frame becomes `resync` |
| Routing | aggregated listing reports per-workspace `truncated`; one daemon down does not fail the rest |
| Idempotency (if built) | duplicate key does not duplicate send; expired key semantics |
| Upload | WAV validation errors, oversize, wrong content type, cancel, no persisted audio |

Extend the style of `tests/test_web_transport.py` and
`tests/test_security_regressions.py` rather than inventing a new harness.

## 2. Fixture export (Python → Swift)

A script (proposed) writes golden files from the real reducer/projection:
snapshots (including reconciliation ids), patch sequences with `add`/`remove`/
`replace`/`append`, JSON Pointer escaping (`~`, `/`), empty-ops frames, resync
frames, Live cards, truncated/clipped content, unknown event types. Swift tests
fail if a fixture changes without regenerating. Fixtures contain no credentials.

## 3. Swift unit tests (NexusKit)

Decode every fixture; patch applier: all ops, escaping, bounded depth/op count,
**atomicity** (a bad op in a frame leaves state unchanged and triggers resync),
final state equals a fresh snapshot; SSE parser (partial chunks, keepalives,
`retry`, error frames); reconnect/resync state machine; Keychain wrapper with an
injectable store; no-secret-in-description checks for model types.

## 4. UI tests against a mock gateway

Scripted mock gateway: sessions list (including truncated, archived scope),
streaming conversation, tool detail sections, clipping labels, approvals (answered
elsewhere), connection loss and recovery, revoked device, draft persistence and
no auto-retry of writes. Accessibility audits: Dynamic Type extremes, VoiceOver
labels, dark mode, iPad keyboard.

## 5. Real-device matrix (manual until automated)

| Dimension | Values |
| --- | --- |
| Network | same Wi-Fi, cellular via Tailscale, Wi-Fi↔cellular handoff, airplane toggle, host asleep, Tailscale off on phone |
| Lifecycle | foreground, background, suspended, killed, relaunch with open draft |
| Permissions | mic granted/denied/revoked mid-use, local-network prompt if relevant |
| Audio | speaker, AirPods, route changes, incoming call, Siri, other-audio interruption |
| Live | start, mute, stop, background, lock, deadline, daemon restart, `live_busy`, consent missing |
| Devices | one small iPhone, one large iPhone, one iPad; Simulator is **not** a substitute for Keychain/Secure Enclave, mic or WebRTC checks |

Real GPT Live and real dictation latency stay **unverified** until this matrix is
run and recorded.

## 6. Exit evidence

Each phase in [ROADMAP](ROADMAP.md) lists the tests that must pass; results are
recorded as dated entries in the repo docs when features ship, not in this folder.
