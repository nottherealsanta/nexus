#!/usr/bin/env python3
"""Seed one scratch Core with a project and a terminal-toggle session.

No provider is called: the session is created idle, so only terminal HTTP/PTY
paths run. Core is tokenless (wildcard listener), so every request here omits
Authorization on purpose.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

TITLE = "Terminal toggle parity"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    home = Path(args.home)
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("# Terminal toggle parity\n\nSeeded, no model calls.\n")
    info_path = home / "daemon.json"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            info = json.loads(info_path.read_text())
            status, _ = call(info["url"], "GET", "/api/info")
            if status == 200:
                break
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    else:
        raise SystemExit("Core did not answer /api/info")

    assert info["token"] == "", "expected a tokenless Core"
    assert info["url"].startswith("http://127.0.0.1:"), info["url"]

    status, projects = call(info["url"], "GET", "/api/project")
    assert status == 200, (status, projects)
    rows = projects if isinstance(projects, list) else projects.get("projects", [])
    project = next((p for p in rows if Path(p["path"]).resolve() == workspace), None)
    if project is None:
        status, project = call(info["url"], "POST", "/api/project", {"path": str(workspace), "name": "Terminal parity"})
        assert status in (200, 201), (status, project)

    status, sessions = call(info["url"], "GET", "/api/session")
    assert status == 200, (status, sessions)
    rows = sessions if isinstance(sessions, list) else sessions.get("sessions", [])
    session = next(
        (s for s in rows if s.get("project_id") == project["id"] and s.get("title") == TITLE),
        None,
    )
    if session is None:
        status, session = call(info["url"], "POST", "/api/session", {"project": project["id"]})
        assert status in (200, 201), (status, session)
        status, session = call(info["url"], "PATCH", f"/api/session/{session['id']}", {"title": TITLE})
        assert status == 200, (status, session)

    payload = {
        "core_url": info["url"],
        "core_pid": info["pid"],
        "core_token": info["token"],
        "project_id": project["id"],
        "project_path": str(workspace),
        "session_id": session["id"],
        "session_title": session.get("title", TITLE),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
