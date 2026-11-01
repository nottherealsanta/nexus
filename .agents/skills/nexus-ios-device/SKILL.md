---
name: nexus-ios-device
description: Build, sign, install, launch, connect and screenshot the Nexus NSH app on a physical iPhone over a LAN or Tailscale network. Use when testing the iOS client on a device, debugging "Nexus Core is not reachable", wiring a native NSH build to Core without a token or pair code, or capturing iPhone screenshots and logs with devicectl.
---

# Nexus on a physical iPhone

This is the end-to-end recipe used to get the NSH client onto an iPhone, connect it directly to Nexus Core on a trusted network (no token, no pairing), and inspect the running app with screenshots.

Everything below uses placeholders. Never put a real Apple ID, Team ID, device UDID, provisioning-profile UUID, tailnet name or machine IP into a skill, a commit or a shared document.

| Placeholder | Meaning |
| --- | --- |
| `<identity>` | Full code-signing identity, e.g. `Apple Development: Name (TEAMID)` |
| `<team-id>` | The 10-character Apple team |
| `<profile>` | Path to a development `.mobileprovision` |
| `<profile-app-id>` | The profile's `Entitlements.application-identifier`, which must cover the app's bundle id |
| `<bundle-id>` | The app's bundle id (the profile's App ID, or `dev.nexus10.nsh`) |
| `<udid>` | The connected iPhone's UDID (or its devicectl name) |
| `<host-ip>` | The Mac's LAN IP (`192.168.x.y`) or Tailscale IP (`100.x.y.z`) |
| `<tailnet-host>` | The Mac's MagicDNS name, e.g. `machine.tailnet.ts.net` |

The project also documents this in [`nexus-nsh/README.md` → Install on an iPhone](../../../nexus-nsh/README.md#install-on-an-iphone). Keep that page and this skill in sync when the flow changes.

## 1. Start Core on the network

A native NSH build has no host proxy, so it talks to Core directly. Core's token rule is tied to its **bind address**:

- bound to loopback (`127.0.0.1`, the default): token required;
- bound off loopback (`NEXUS_HOST` / `nexus serve --host`): anonymous `/api/*`, so a phone needs only an address.

```sh
nexus daemon stop
NEXUS_HOST=0.0.0.0 nexus daemon start
```

The foreground equivalent is `NEXUS_HOST=0.0.0.0 nexus serve` (add `--host 0.0.0.0 --port 4747` if not using the env vars). A restart is required: an existing loopback daemon keeps its old bind and token.

Verify the bind actually changed:

```sh
lsof -nP -iTCP:4747 -sTCP:LISTEN
# *:4747   good; 127.0.0.1:4747 means it is still loopback-only
curl -s http://<host-ip>:4747/api/info      # JSON, no Authorization header
```

Only do this on a trusted LAN, Tailscale or another private network: everyone who can reach the address can use the API. `NEXUS_HOST` is opt-in; never make it the default.

Useful addresses:

```sh
ipconfig getifaddr en0        # Wi-Fi LAN IP (en1 for Ethernet)
tailscale ip -4               # Tailscale IP, when the CLI is installed
# App Store Tailscale lives at:
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

## 2. Find the signing identity and profile

```sh
security find-identity -v -p codesigning
```

Xcode keeps downloaded profiles in `~/Library/Developer/Xcode/UserData/Provisioning Profiles/`. Print each profile's App ID to find the one that covers the device and the app:

```sh
for p in "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"/*.mobileprovision; do
  security cms -D -i "$p" > /tmp/profile.plist 2>/dev/null || continue
  printf '%s | %s | %s\n' \
    "$(plutil -extract Name raw /tmp/profile.plist)" \
    "$(plutil -extract Entitlements.application-identifier raw /tmp/profile.plist)" "$p"
done
```

The profile must list the iPhone's UDID (check `plutil -extract ProvisionedDevices json -o - /tmp/profile.plist`). Profiles already installed on the phone are listed with:

```sh
xcrun devicectl device profile list --device <udid>
```

If no matching profile exists, create an iOS App Development profile in the Apple developer portal for the app's bundle id and the device UDID, or let Xcode create one by running a throwaway project with the same bundle id on the device once. Then download it and repeat this step.

## 3. Build the app unsigned

The ATS/local-network patch edits `Info.plist`, so it must happen **before** signing. Package without `--sign`:

```sh
cd nexus-nsh
npm run gen
nsh package App.tsx --target ios --out .nsh/out-ios [--id <profile-app-id>]
node scripts/native-info.mjs .nsh/out-ios/Nexus.app
```

- `--id` overrides `nsh.json`'s bundle id when the available profile covers a different App ID. The installed app's storage/id changes with it.
- Output is `.nsh/out-ios/<Name>.app`; it is unsigned at this point and must not be installed.
- `native-info.mjs` adds `NSAllowsArbitraryLoads` and (on iOS) `NSLocalNetworkUsageDescription`, then ad-hoc signs the copy. It does not make a device build installable.

Why arbitrary loads and not the "local networking" exception: iOS's `NSAllowsLocalNetworking` does **not** cover plain HTTP to a Tailscale `100.x` address, and when present it takes precedence over `NSAllowsArbitraryLoads`. A plain-HTTP request then fails with:

```text
The resource could not be loaded because the App Transport Security policy requires the use of a secure connection.
```

`NSAllowsArbitraryLoads` alone allows plain HTTP to any address. That is fine for a self-installed development client; for App Store distribution prefer an `https://` Core address (for example through `tailscale serve`) and remove the exception.

## 4. Sign it

Decode the profile's entitlements and sign with the identity found in step 2. Copy the profile into the app as `embedded.mobileprovision`; `codesign` reads it from the bundle.

```sh
PROFILE="<profile>"
IDENTITY="<identity>"

security cms -D -i "$PROFILE" > /tmp/profile.plist
plutil -extract Entitlements xml1 -o /tmp/entitlements.plist /tmp/profile.plist
cp "$PROFILE" .nsh/out-ios/Nexus.app/embedded.mobileprovision
codesign --force --deep --sign "$IDENTITY" \
  --entitlements /tmp/entitlements.plist --timestamp=none .nsh/out-ios/Nexus.app
```

Check it:

```sh
codesign -dv --verbose=2 .nsh/out-ios/Nexus.app
# Authority=... your identity; Signature=... not "adhoc"
codesign -d --entitlements :- .nsh/out-ios/Nexus.app
# application-identifier must equal <profile-app-id>
```

If the identity's team does not match the profile's `TeamIdentifier`, signing fails or the app will not launch. Generate the profile with that identity's team.

## 5. Install and launch

Connect the iPhone by USB, unlock it, and trust the computer.

```sh
xcrun devicectl list devices
xcrun devicectl device install app --device <udid> .nsh/out-ios/Nexus.app
xcrun devicectl device process launch --device <udid> --terminate-existing <bundle-id>
```

Xcode's **Window → Devices and Simulators → + under Installed Apps** installs the same `.app`. The device must be unlocked to launch from `devicectl`; a locked phone reports `BSErrorCodeDescription = Locked`.

If launch fails with `invalid code signature, inadequate entitlements or its profile has not been explicitly trusted`, check:

1. `codesign -dv` shows the identity, not adhoc;
2. the embedded profile includes the device UDID and covers the bundle id;
3. the developer certificate is trusted on the phone (next step).

## 6. Trust it on the phone

- **Settings → Privacy & Security → Developer Mode → on**, then restart.
- **Settings → General → VPN & Device Management → the Apple Development identity → Trust**.

Uninstalling and reinstalling can require the trust step again. A development profile expires; re-sign and reinstall when it does.

## 7. Connect to Core

On the Connect screen enter the address, not a token:

```text
<host-ip>:4747
# or explicitly: http://<host-ip>:4747
# or HTTPS:       https://<tailnet-host>[:port]
```

Notes:

- `:4747` is required; the app assumes port 80 when it is omitted.
- `localhost` is the phone itself. Use the Mac's LAN or tailnet address.
- The app adds `http://` when the scheme is missing and stores the address in NSH Storage under `nexus.core`.
- The header's **Core** button reopens the Connect screen while the app is running.
- `connectCore()` probes `GET /api/info` with an 8 s timeout before saving. Failures stay on the Connect screen with the actual reason, so the offline shell does not hide a setup mistake.
- Allow **Local Network** when iOS asks. If denied: **Settings → Privacy & Security → Local Network → Nexus**.

On success the header stops saying `offline`, the project/session data appears, and the composer's context meter shows a value.

## 8. Screenshots and visual checks

### Physical iPhone

`devicectl` captures the real device screen, including the app and system permission dialogs:

```sh
xcrun devicectl device capture screenshot \
  --device <udid> --destination /tmp/iphone.png
```

The phone must be unlocked. Read the PNG with the image tool; the pixel size is the device's resolution (for example 1206×2622), not points. This is the only way to see safe areas, the status bar, the keyboard and the real UIKit rendering.

### Simulator

```sh
cd nexus-nsh
npm run ios          # package, patch, install and launch the simulator build
```

Without an installed runtime, `npm run ios` exits with the message to run `xcodebuild -downloadPlatform iOS`. `npm run ios:catalyst` opens the same UIKit UI in a Mac window.

### iPhone-sized layout without a simulator

The web target accepts a viewport and safe-area insets, which is the fastest visual loop for layout (not for ATS/permissions):

```tsx
const app = await open("./App.tsx", {
  network, width: 390, height: 844, platform: "web",
  safeArea: { top: 47, bottom: 34 }, colorScheme: "dark",
});
await app.waitFor("id:header");
await app.screenshot("/tmp/ios-layout.png");
```

Use it for spacing, narrow breakpoints (`isNarrow()`), touch-sized rows (`platform.idiom === "phone"`), and light/dark checks. Do not use it to verify networking; the web host is same-origin and its `platform.os` is `web`, so native-only screens such as Connect never appear there.

### Native logic without a display

`nsh test` with no `--target` runs as iOS without a browser. It can assert the Connect flow (`id:connect`, type an address, submit, wait for `id:header`) but cannot screenshot. Use both modes: headless for the flow, web-at-390 for pixels, devicectl for the device.

## 9. Debugging "Nexus Core is not reachable"

Work from the Mac outward; each step rules out one layer.

1. **Is Core listening off loopback?**
   ```sh
   lsof -nP -iTCP:4747 -sTCP:LISTEN
   ```
   `*:4747` is required. If it is `127.0.0.1:4747`, Core was restarted without `NEXUS_HOST`; stop and start it again.
2. **Does Core answer from the Mac?**
   ```sh
   curl -i http://<host-ip>:4747/api/info
   ```
   200 with JSON and no token is the target. 401 means token mode (loopback bind). Connection refused/timeout means the address/interface is wrong.
3. **Does the phone reach it?** Open the same URL in the iPhone's Safari. Safari failing means a network problem (Wi-Fi, Tailscale, firewall), not the app. Safari succeeding but the app failing means ATS or Local Network permission.
4. **Read the app's own probe error.** Tap **Core**, re-enter the address, tap **Connect**. The screen prints:
   - `App Transport Security policy requires the use of a secure connection` → the build was patched after signing, or only `NSAllowsLocalNetworking` was used. Redo steps 3–4 in order with `NSAllowsArbitraryLoads`.
   - `The request timed out` / `could not connect to the server` → routing, Local Network permission, or Core not bound.
   - `This Core asks for a token` → restart Core with `NEXUS_HOST=0.0.0.0`.
5. **Tailscale specifics.** `tailscale status` lists peers; `tailscale ping <peer>` shows DERP/direct connectivity. A peer can be online via DERP and still have TCP blocked by a wrong ACL or a stopped app. On the Mac, `tailscale serve status` shows existing HTTPS handlers.
6. **Local Network permission.** iOS may prompt on the first request; if it was denied, toggle it in Settings for the app. Uninstalling and reinstalling resets app permissions (and app data).
7. **macOS firewall.** If enabled, allow incoming connections for the Python/`nexus` binary. `socketfilterfw --getglobalstate` reports the state.

### HTTPS fallback

If plain HTTP remains blocked, serve Core over TLS. `tailscale serve` gives a valid certificate on the tailnet:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:4747
# app address: https://<tailnet-host>:8443
```

Core still has to be anonymous for the app (no token): keep `NEXUS_HOST=0.0.0.0`. An HTTPS URL does not need the ATS patch, so a normal `nsh package ... --sign ... --profile ...` flow works for a device build; avoid reusing port 443 if something else already serves there.

## 10. Rebuilding after a code change

Repeat steps 3–5; do not reuse the previous signed app:

```sh
npm run gen
nsh package App.tsx --target ios --out .nsh/out-ios [--id <profile-app-id>]
node scripts/native-info.mjs .nsh/out-ios/Nexus.app
# sign (step 4)
xcrun devicectl device install app --device <udid> .nsh/out-ios/Nexus.app
xcrun devicectl device process launch --device <udid> --terminate-existing <bundle-id>
```

Installing over the existing app **preserves its data**, including the saved `nexus.core`. Uninstalling clears it and resets permissions. If a launch behaves oddly after reinstall, uninstall, reinstall, then unlock the device and trust the certificate.

## 11. Pitfalls

- Signing order: package → patch → sign. Patching a signed app invalidates the signature.
- `NSAllowsLocalNetworking` overrides `NSAllowsArbitraryLoads` for addresses iOS calls local; use arbitrary loads alone for a self-installed client.
- The profile's App ID must equal the app's `CFBundleIdentifier`; use `--id` or a matching profile.
- `codesign --deep` is fine for this simple bundle; verify with `codesign -dv` that the result is not adhoc before installing.
- A development profile expires; direct installs stop launching when it does.
- The device must be unlocked for `devicectl` launches and screenshots (and unlock it for installs too).
- The iPhone cannot use `localhost` or the Mac's loopback address.
- Never return a native build to loopback-token assumptions: the app sends no token by design.
- `NSAllowsArbitraryLoads` is for development/self-installed builds; App Store review wants a justification or HTTPS.

## 12. Quick command sheet

```sh
# Core for the network
nexus daemon stop && NEXUS_HOST=0.0.0.0 nexus daemon start
lsof -nP -iTCP:4747 -sTCP:LISTEN
curl -s http://<host-ip>:4747/api/info

# Signing assets
security find-identity -v -p codesigning
xcrun devicectl list devices
xcrun devicectl device profile list --device <udid>

# Build, patch, sign
cd nexus-nsh
npm run gen
nsh package App.tsx --target ios --out .nsh/out-ios [--id <profile-app-id>]
node scripts/native-info.mjs .nsh/out-ios/Nexus.app
security cms -D -i "<profile>" > /tmp/profile.plist
plutil -extract Entitlements xml1 -o /tmp/entitlements.plist /tmp/profile.plist
cp "<profile>" .nsh/out-ios/Nexus.app/embedded.mobileprovision
codesign --force --deep --sign "<identity>" --entitlements /tmp/entitlements.plist --timestamp=none .nsh/out-ios/Nexus.app
codesign -dv --verbose=2 .nsh/out-ios/Nexus.app

# Install, launch, screenshot
xcrun devicectl device install app --device <udid> .nsh/out-ios/Nexus.app
xcrun devicectl device process launch --device <udid> --terminate-existing <bundle-id>
xcrun devicectl device capture screenshot --device <udid> --destination /tmp/iphone.png
```
