# Nexus for iPhone and iPad

A SwiftUI remote client for the opt-in [Nexus gateway](../docs/gateway.md).
The daemon runs on your Mac or Linux machine; the phone connects over HTTPS
inside your Tailscale network. No Nexus core changes are needed for this client.

## Connect your device

1. Connect Tailscale on the Nexus machine and iPhone/iPad.
2. Allow a workspace: `nexus gateway allow /absolute/path/to/workspace`.
3. Configure Tailscale Serve to proxy your private HTTPS address to
   `http://127.0.0.1:8765`. Keep it private to your tailnet.
4. Start `nexus gateway serve --port 8765 --public-url https://your-machine.your-tailnet.ts.net`.
5. Run `nexus gateway pair --name phone`; enter the HTTPS URL and one-use code
   in the app within two minutes. Session viewing needs `read`; creating sessions
   and sending messages also need `prompt`. Tokens stay in device-only Keychain.
6. Choose an allowed workspace, open a session, or tap New session.

The app supports pairing, reconnecting, workspace selection, session search,
session creation, message submission, per-session drafts, stopping a turn, one-time
approval/denial (with `approve` scope), and structured conversation inspection. Messages appear as cards; expandable details retain all snapshot fields,
including tools and context. Connection settings are available from the network button.
Read-only devices cannot create sessions or send messages.

Conversations load a complete snapshot, then apply the host’s live SSE `ops`
frames. Reconnects fetch a fresh snapshot before resubscribing; failed reads retry
after three seconds while foregrounded. Backgrounding closes the stream, and
returning to the app refreshes it. Stale responses cannot replace another session.
Failed writes keep the draft and are never automatically retried. Stop retains
queued messages. Approval actions allow or deny once; permanent rules are not
created by this UI. Lists announce truncation; search covers loaded active sessions.

Follow latest can be paused while reading older messages. All tool arguments,
outputs, context, errors and snapshot fields remain available in labelled details.
Dictation, live calls, question-answer controls, attachments, persistent approval
rules and background notifications are not integrated yet. Voice source files
remain outside the application build until that integration is ready.

## Build and verify

Open `ios/Nexus.xcodeproj`, select the Nexus scheme and an iOS 17+ destination.
Choose your signing team to install on a physical device.

```sh
xcodebuild -project ios/Nexus.xcodeproj -scheme Nexus -sdk iphonesimulator \
  -derivedDataPath /tmp/nexus-ios-build CODE_SIGNING_ALLOWED=NO build
swift test --package-path ios/NexusKit
swift test --package-path ios
# Compile both the app and UI tests without running a simulator:
xcodebuild -project ios/Nexus.xcodeproj -scheme Nexus \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/nexus-ios-build CODE_SIGNING_ALLOWED=NO build-for-testing
```

App and UI-test compilation, nine NexusKit tests and eleven app-state tests
are verified. App-state tests run on macOS with a mock gateway, without network
or real Keychain credentials. They cover session creation, live patches, resync,
foreground/background stream cancellation, draft retention, stale responses,
failed-write behavior, stop/approval requests and revoked access.

Runtime UI and real-device/Tailscale connectivity are **not verified**: the
verification machine has no installed iOS simulator runtime. The shared Nexus
scheme includes `NexusUITests`; run it in Xcode after installing a runtime. Debug
UI tests start unpaired without accessing developer Keychain credentials. Device
background/suspension behavior, accessibility and network handoff still need manual
verification. See [testing](docs/TESTING.md).

The remaining architecture and roadmap files under [docs/](docs/) include
historical proposals. The implementation, this README, [UX](docs/UX.md) and
[testing](docs/TESTING.md) describe the current session client. See the main [gateway guide](../docs/gateway.md)
for pairing scopes, revocation and server configuration.
