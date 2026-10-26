# iOS verification

## Verified in this change

- `swift test --package-path ios/NexusKit`: nine tests pass. JSON round trip,
  HTTPS enforcement, request payload/authentication/path, canonical host `ops`
  replay, cursor handling, atomic patch failure, SSE Unicode/multiline/bounds,
  array insertion and gateway address validation.
- `swift test --package-path ios`: eleven real AppStore tests pass against an
  in-process URLProtocol mock. Session creation, selection/draft preservation,
  late-response isolation, live host patch updates, automatic resync/reconnect,
  background stream cancellation, read-only devices, failed-send draft/no retry,
  navigating during a successful send, forget/revocation, and stop/approval payloads.
  The tests use isolated preferences and injected credential functions, never
  developer Keychain credentials or a real gateway.
- Xcode simulator app build and `build-for-testing` for the shared `Nexus` scheme
  succeed. This compiles the `NexusUITests` onboarding/HTTPS-validation tests.

Run the commands from [the app README](../README.md#build-and-verify).

## Not verified

No simulator runtime is installed on the verification machine. Xcode UI tests
have been **compiled, not executed**; there are no runtime screenshots. Install an
iOS 17+ simulator runtime, select its device in Xcode, and run the shared Nexus
scheme tests. Debug UI tests pass `--ui-testing` to start unpaired with isolated
preferences and no real Keychain access. Release builds ignore that argument.

Physical-device pairing, Keychain behavior, TLS certificates, Tailscale Serve,
network handoff, appearance and accessibility remain unverified. A mock transport
is not evidence of successful device networking or OS suspension behavior.

## Manual device checklist

| Area | Check |
| --- | --- |
| Pairing | One-use code, expiry, wrong code, revoked device, clear HTTPS error; no credential disclosure. |
| Network | Same Wi-Fi, cellular over Tailscale, Wi-Fi/cellular handoff, airplane mode, Tailscale off, host asleep. |
| Sessions | Multiple authorized workspaces, empty workspace, creation, title search, truncated lists, fast switching, iPhone back and iPad selection. |
| Conversation | Streamed text, tools/context, long text, queue, reconnect, approvals answered from another client, stop retaining queue. |
| Drafts | Failed send, uncertain timeout, switching sessions during send, relaunch, forgetting device. |
| Lifecycle | Foreground/background, screen lock, suspension, force quit, reconnect with draft. |
| Accessibility | Small iPhone, iPad, dark mode, VoiceOver, largest Dynamic Type, hardware keyboard. |

Dictation, Live and attachment flows remain future integration work; no tests or
latency claims are made for them by this change.
