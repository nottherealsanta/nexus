# iOS security plan (proposed)

All of this is **proposed** and needs security review before implementation.
Existing boundaries are in [security.md](../../docs/security.md); they are not weakened.

## Threat model in one paragraph

A paired phone can direct the agent, and the agent can run `bash` on the host
under the normal permission engine with **no OS sandbox** (**current**). So
pairing is granting remote operator access, and the design protects the pairing,
the channel and the blast radius of a lost phone. "Inside the tailnet" is not
authentication: **device authentication is required even on a tailnet.**

## Exposure

- Gateway on loopback; remote reach via `tailscale serve` TLS ([GATEWAY](GATEWAY.md#exposure-default-proposal)).
- No Tailscale Funnel, no public exposure, no direct tailnet bind of daemons or
  the gateway in v1, no unauthenticated tunnel to a raw daemon.
- Deployment must be validated (headers, trust, proxy isolation on the host,
  availability): **unverified**.

## Pairing

1. User runs a sketch `nexus gateway pair` on the host (local, interactive). The
   gateway mints a **single-use secret, ~120 s lifetime** and shows a QR code
   encoding a URL (gateway address + secret + gateway identity hint). The
   browser ticket today is one-use, 60 s ([security.md](../../docs/security.md))—same
   spirit, longer for camera scanning.
2. The user also picks the device's workspace allowlist and capabilities (default:
   P1 command set, no voice/Live).
3. The app generates a **device key pair** and redeems the secret over TLS,
   sending the public key. The secret is burned on first use or expiry; failed
   attempts are rate-limited and logged without the secret.
4. Afterward the app proves possession per session (challenge/response or signed
   requests) and receives **short-lived, audience-bound access tokens** (bound
   to this gateway and device).

Crypto algorithms, token format, challenge design and any pin policy are
**proposed pending security review**; do not code a choice before that.

## Device secrets on iOS

- Private key and refresh material in the Keychain, `ThisDeviceOnly`
  accessibility class, no iCloud sync, no backup.
- Prefer a Secure Enclave–backed key where available. Where it is not, the
  **documented fallback** is a software-backed Keychain key protected by the
  OS and `ThisDeviceOnly` policies. This is a **weaker** guarantee: the key is
  not hardware-bound, and its extraction resistance is **not equivalent** to
  Secure Enclave. Whether the fallback is acceptable is a review item; the app
  must not describe it as hardware-protected. Do not promise Secure Enclave in
  the Simulator (not available there) or on every device.
- No secrets in logs, crash reports, screenshots of app switcher (blur on
  background), pasteboard or URL schemes.
- **No indiscriminate certificate pinning.** Tailscale-issued certificates rotate;
  pinning leaf certs would break the app. Trust relies on system TLS plus the
  pairing-time gateway identity; any pin policy (e.g. on the gateway identity key
  rather than the leaf) is a review item.

## Authorization

- Per-device capability set and workspace allowlist (opaque ids), **re-evaluated
  on every write** and on stream attach; changes apply to open streams.
- Command allowlist, never denylist ([GATEWAY](GATEWAY.md#command-allowlist-per-phase-proposed)).
- Approvals: the phone can resolve `PermissionResolve`/`QuestionAnswer` only if
  granted that capability. Approval UI shows full context (tool, labelled
  arguments, target) from the host, never a summary the model wrote.
- GPT Live: approvals are resolved **on screen only**, never by audio
  ([security.md](../../docs/security.md#gpt-live-data-sent-to-openai-and-consent)).

## Revocation and lifecycle

- Revoking a device (host CLI sketch, or from another already-trusted device if
  allowed) deletes its public key, invalidates tokens, and **closes its open
  streams immediately**.
- Tokens are short-lived and audience-bound; the gateway rejects tokens minted for
  another audience or device.
- App-side "forget this host" wipes keys and cached data.
- Lost phone: host-side revoke is the control; Presence leases held by the dead
  view are released as today (**current**).

## Abuse limits

Pairing attempts, token requests, command rate, request/body sizes, stream count
and queue depth are bounded per device and globally; replayed signed requests are
rejected (nonce/timestamp window); every error is redacted. Negative tests are
listed in [TESTING](TESTING.md).

## Data on the phone

Cache only what the user can already see; mark transcripts as sensitive (file
protection class "complete unless open"); no raw audio retained by default
([VOICE_AND_LIVE](VOICE_AND_LIVE.md)); a clear "clear cache" action. Host text is
untrusted: render as text, never as HTML or executable links
([surfaces.md](../../docs/surfaces.md)).

## Review checklist before P1 code

Gateway threat review; algorithm and token design; Tailscale deployment
validation; identity/cert plan; capability model; revocation semantics.
