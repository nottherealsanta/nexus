#!/usr/bin/env python3
"""Read/verify terminal resources over the tokenless loopback API.

Usage:
  probe.py list   --home HOME --project ID [--out FILE]
  probe.py create --home HOME --project ID --workspace DIR --client NAME
  probe.py check  --home HOME --project ID --workspace DIR [--lan-ip IP]
  probe.py delete --home HOME --id TERMINAL_ID
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def info(home: str) -> dict:
    return json.loads((Path(home) / "daemon.json").read_text())


def call(url: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw.decode(errors="replace")}
        return exc.code, payload


def terminals(url: str, project: str) -> tuple[int, list]:
    status, payload = call(url, "GET", f"/api/terminal?project_id={project}")
    if status != 200:
        return status, []
    return status, payload if isinstance(payload, list) else payload.get("terminals", [])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["list", "create", "check", "delete"])
    parser.add_argument("--home", required=True)
    parser.add_argument("--project")
    parser.add_argument("--workspace")
    parser.add_argument("--client", default="probe")
    parser.add_argument("--id")
    parser.add_argument("--lan-ip")
    parser.add_argument("--out")
    args = parser.parse_args()

    daemon = info(args.home)
    assert daemon.get("token") == "", "probing expects the tokenless scratch Core"
    url = daemon["url"]

    if args.command == "list":
        status, rows = terminals(url, args.project)
        payload = {"status": status, "terminals": [
            {k: t.get(k) for k in ("id", "name", "status", "creator", "workspace", "session_id", "rows", "cols")}
            for t in rows
        ]}
        if args.out:
            Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload))
        return 0 if status == 200 else 1

    if args.command == "create":
        status, payload = call(url, "POST", "/api/terminal", {
            "project_id": args.project,
            "workspace": args.workspace,
            "client_id": args.client,
            "rows": 24,
            "cols": 80,
        })
        print(json.dumps({"status": status, "body": payload}))
        return 0 if status == 201 else 1

    if args.command == "delete":
        status, payload = call(url, "DELETE", f"/api/terminal/{args.id}")
        print(json.dumps({"status": status, "body": payload}))
        return 0 if status == 200 else 1

    result: dict = {"loopback": {}, "non_loopback": {}}
    status, payload = call(url, "POST", "/api/terminal", {
        "project_id": args.project,
        "workspace": args.workspace,
        "client_id": args.client + "-loopback",
        "rows": 24,
        "cols": 80,
    })
    result["loopback"] = {"status": status, "terminal": payload.get("id"), "error": payload.get("error")}
    if status == 201 and payload.get("id"):
        result["loopback"]["delete_status"] = call(url, "DELETE", f"/api/terminal/{payload['id']}")[0]

    if args.lan_ip:
        lan_url = f"http://{args.lan_ip}:{url.rsplit(':', 1)[1]}"
        probe = socket.socket()
        probe.settimeout(5)
        probe.connect((args.lan_ip, int(url.rsplit(":", 1)[1])))
        source = probe.getsockname()[0]
        probe.close()
        status, payload = call(lan_url, "POST", "/api/terminal", {
            "project_id": args.project,
            "workspace": args.workspace,
            "client_id": args.client + "-lan",
            "rows": 24,
            "cols": 80,
        })
        result["non_loopback"] = {
            "status": status,
            "source_address": source,
            "terminal": payload.get("id"),
            "error": payload.get("error"),
        }
        if status == 201 and payload.get("id"):
            call(lan_url, "DELETE", f"/api/terminal/{payload['id']}")

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["loopback"]["status"] == 201 else 1


if __name__ == "__main__":
    sys.exit(main())
