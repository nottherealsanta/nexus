"""``nexus gateway``: serve, allow/disallow workspaces, pair and manage devices.

Everything here is local to the machine that runs the gateway: allowed
workspaces come from a path on this machine, never from the network, and pairing
codes are minted only by this command. Heavy modules (``httpx``, the server) are
imported by ``serve`` alone so ``nexus --help`` stays cheap.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import TextIO

from .store import (
    DEFAULT_CODE_TTL,
    DEFAULT_TTL_DAYS,
    MAX_TTL_DAYS,
    SCOPES,
    GatewayError,
    GatewayStore,
)

DEFAULT_PORT = 8765


def add_parser(sub: argparse._SubParsersAction) -> None:
    gateway = sub.add_parser(
        "gateway", help="Opt-in remote gateway for paired native devices (docs/gateway.md)"
    )
    actions = gateway.add_subparsers(dest="gateway_action", required=True)

    serve = actions.add_parser("serve", help="Serve the gateway on a loopback port")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Loopback port (default {DEFAULT_PORT})")
    serve.add_argument(
        "--public-url", default="",
        help="HTTPS name devices use, e.g. https://host.tailnet.ts.net (TLS is terminated by a proxy such as `tailscale serve`)",
    )

    allow = actions.add_parser("allow", help="Allow an existing local workspace directory")
    allow.add_argument("path")

    disallow = actions.add_parser("disallow", help="Remove an allowed workspace (host id or path)")
    disallow.add_argument("target", metavar="HOST_ID_OR_PATH")

    pair = actions.add_parser("pair", help="Mint a single-use pairing code for a new device")
    pair.add_argument("--name", default="device", help="Label for the device")
    pair.add_argument(
        "--scopes", default=",".join(SCOPES),
        help=f"Comma-separated subset of {','.join(SCOPES)} (default: all, including live)",
    )
    pair.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS, help=f"Device lifetime, 1 to {MAX_TTL_DAYS} days")
    pair.add_argument(
        "--host", action="append", default=None, metavar="HOST_ID",
        help="Allow this host id (repeatable; default: every workspace allowed now)",
    )
    pair.add_argument("--code-ttl", type=float, default=DEFAULT_CODE_TTL, help="Seconds the code stays valid")

    actions.add_parser("devices", help="List paired devices")
    revoke = actions.add_parser("revoke", help="Revoke a device immediately")
    revoke.add_argument("device_id")
    actions.add_parser("status", help="Show allowed workspaces, devices and whether a gateway is serving")


def run(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    action = args.gateway_action
    store = GatewayStore()
    try:
        if action == "serve":
            from .runner import run_gateway

            return asyncio.run(
                run_gateway(store, port=args.port, public_url=args.public_url, stdout=stdout, stderr=stderr)
            )
        if action == "allow":
            entry = store.allow(Path(args.path))
            stdout.write(f"allowed {entry.host_id} {entry.workspace}\n")
            return 0
        if action == "disallow":
            entry = store.disallow(args.target)
            if entry is None:
                stderr.write("Error: no such allowed workspace\n")
                return 1
            stdout.write(f"disallowed {entry.host_id} {entry.workspace}\n")
            return 0
        if action == "pair":
            return _pair(store, args, stdout)
        if action == "devices":
            now = time.time()
            for device in store.devices():
                days = max(0, int((device.expires_at - now) // 86400))
                stdout.write(
                    f"{device.device_id}  {device.name}  scopes={','.join(device.scopes)}  "
                    f"hosts={','.join(device.host_ids) or '-'}  expires_in={days}d\n"
                )
            return 0
        if action == "revoke":
            if not store.revoke(args.device_id):
                stderr.write("Error: no such device\n")
                return 1
            stdout.write(f"revoked {args.device_id}\n")
            return 0
        if action == "status":
            return _status(store, stdout)
    except GatewayError as exc:
        stderr.write(f"Error: {exc}\n")
        return 1
    raise ValueError(f"unknown gateway action {action!r}")


def _pair(store: GatewayStore, args: argparse.Namespace, stdout: TextIO) -> int:
    code, expires_at = store.create_pairing(
        name=args.name, scopes=args.scopes, ttl_days=args.ttl_days,
        host_ids=args.host, code_ttl=args.code_ttl,
    )
    from .store import parse_scopes

    scopes = parse_scopes(args.scopes)
    stdout.write(f"pairing code: {code}\n")
    stdout.write(f"valid for {int(expires_at - time.time())}s, single use; scopes: {','.join(scopes)}\n")
    stdout.write("Enter it in the app with this gateway's https URL. It is shown once and not stored.\n")
    if "live" in scopes:
        stdout.write(
            "Note: the live scope lets this device start GPT Live calls and record the once-per-machine "
            "consent to send microphone audio and work summaries to OpenAI. Omit `live` from --scopes to refuse.\n"
        )
    return 0


def _status(store: GatewayStore, stdout: TextIO) -> int:
    hosts = store.hosts()
    stdout.write(f"serving: {'yes' if store.serving() else 'no'}\n")
    stdout.write(f"devices: {len(store.devices())}  pending codes: {store.pending_count()}\n")
    stdout.writelines(f"host {h.host_id}  {h.workspace}\n" for h in hosts)
    if not hosts:
        stdout.write("no workspace is allowed; run `nexus gateway allow PATH`\n")
    return 0
