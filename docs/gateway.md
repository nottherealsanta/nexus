# Gateway (opt-in)

The gateway exposes selected local workspaces to paired native clients. It is
off by default and is a separate loopback HTTP service, not the host's existing
web service. It does not bind a public interface. Use a private-network TLS
proxy such as Tailscale Serve when devices need to connect remotely.

## Commands

The CLI is the source of truth; inspect it with `python -m nexus gateway --help`
and `python -m nexus gateway ACTION --help`.

```sh
python -m nexus gateway allow /path/to/workspace
python -m nexus gateway status
python -m nexus gateway serve --port 8765 --public-url https://machine.example.ts.net
```

Allow/disallow operate on paths/host IDs on the gateway machine. Serving is
explicit opt-in. The `--port` default is 8765; the listener remains loopback.
`--public-url` is the HTTPS URL devices use, and TLS must be terminated by a
trusted proxy. For example, configure `tailscale serve` manually to proxy that
HTTPS name to the gateway's loopback port before starting `serve`. Do not use
Tailscale Funnel or expose the listener publicly.

In another terminal, mint a one-use six-digit code and enter it in the native app:

```sh
python -m nexus gateway pair --name phone
python -m nexus gateway devices
python -m nexus gateway revoke DEVICE_ID
```

Pairing defaults to the scopes printed by `--help` (currently all scopes,
including `live`), a seven-day device lifetime, and a 120-second code lifetime.
Use `--scopes` to grant a comma-separated subset, `--ttl-days` to set device
lifetime (1–90 days), `--code-ttl` to set code lifetime in seconds, and repeat
`--host HOST_ID` to restrict the device to selected allowed workspaces. Without
`--host`, it may access all workspaces allowed at pairing time. Review scopes
carefully: omit `live` to prevent live voice calls. `disallow` removes access to
a workspace; `revoke` immediately disables a device.

Supported scopes are `read`, `prompt`, `approve`, `voice`, and `live`. The
`--code-ttl` default is 120 seconds.

## How the bridge works

The gateway asks the local workspace daemon for a one-use web launch ticket
over its Unix-domain socket. It accepts only an HTTP URL with a literal
loopback IP, redeems the ticket at that origin, and uses the resulting host
cookie and CSRF token only for that same loopback origin. The gateway forwards
the host's session list, snapshot, events, voice and allowed command protocol;
it does not translate or invent host state. A command is not retried after it
may have reached the host.

See [security notes](security.md#gateway) for trust boundaries and
[host documentation](host.md) for the unchanged local host service.

## iOS client

The SwiftUI client in [`ios/`](../ios/README.md) pairs against this gateway's
private HTTPS address. It lists authorized workspaces and active sessions, creates
sessions with `SessionOpen`, sends with `SessionEnqueue`, stops turns without
dropping the queue, and answers one-time approvals using existing host commands.
It makes no changes to the core or gateway contract. Credentials stay in Keychain;
the client refuses redirects and gateway addresses containing embedded credentials,
queries or fragments.

The client follows a validated snapshot with the host's canonical SSE `ops`
frames, resnapshotting on a resync or dropped connection. Streams close in the
background; returning foreground refreshes state. Writes are never automatically
retried. Drafts persist per gateway/workspace/session and survive failed sends.
Twenty Swift transport/state tests and app/UI-test compilation are verified;
runtime UI, real-device/Tailscale connection and suspension are not verified.
See the iOS README for setup and remaining limitations.
