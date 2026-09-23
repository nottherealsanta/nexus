"""The canonical terminal surface (PLAN sections 8c, 14.8, 14.11).

Every command here is a *client* of the workspace daemon. ``nexus run`` and
``nexus chat`` speak the canonical Unix-socket wire through
:mod:`nexus.ui.cli` and auto-start a daemon when no socket is listening; the
daemon owns the ``Runtime`` and there is deliberately no in-process fallback.
The inspection commands (``doctor``, ``models``, ``sessions``, ``ext``,
``agents``, ``daemon``) go over the same host protocol/facade.

Import discipline: ``nexus.cli`` must stay cheap. The heavy surface modules
(``nexus.ui.cli``, ``nexus.host.daemon``, ``nexus.ui.jsonl``) are imported
inside the command handler that needs them, so ``nexus --help`` and ``nexus
init`` never pull the runtime, the providers, or ``httpx``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, TextIO

DEFAULT_CONFIG = '''# Nexus reloads configuration before each turn. Unknown keys are errors.
# `models.default` may be a tier name ("medium"), "provider/model", or a bare id.
config_version = 2

[agent]
profile = "coding"
instructions_file = "SOUL.md"
memory_file = "MEMORY.md"

[models]
default = "medium"

[permissions]
mode = "ask"
allow = ["Read(**)", "Glob(**)", "Grep(**)", "LS(**)"]
deny = ["Bash(rm -rf*)", "Read(**/.env)"]
write_roots = ["./"]
on_unattended = "deny"
'''

DEFAULT_SOUL = '''# Agent instructions

Complete the user's request in this workspace. Read relevant project
instructions, use tools to inspect, edit, and verify your work, and report
outcomes and limitations.

Configuration and instructions reload at the start of every turn. Workspace
extensions under `.nexus/tools/`, `.nexus/hooks/`, `.nexus/skills/`, and
`.nexus/agents/` are hot and take effect without a restart. Core Nexus source
changes require restarting the daemon (`nexus daemon stop`).

Keep this harness small: prefer focused changes, verify them, and do not claim
an edit took effect when it did not.
'''

DEFAULT_MEMORY = "# Memory\n\n"


def initialize(workspace: Path) -> None:
    """Create editable configuration without overwriting anything present."""
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in {
        "nexus.toml": DEFAULT_CONFIG,
        "SOUL.md": DEFAULT_SOUL,
        "MEMORY.md": DEFAULT_MEMORY,
    }.items():
        try:
            with (workspace / name).open("x", encoding="utf-8") as handle:
                handle.write(content)
            print(f"Created {name}")
        except FileExistsError:
            print(f"Kept {name}")


# ---------------------------------------------------------------------------
# Daemon-backed commands
# ---------------------------------------------------------------------------


async def _run(
    workspace: Path,
    *,
    message: str,
    session: str,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
    approver: Any | None,
) -> int:
    from .ui.cli import open_client, run_once

    client = await open_client(workspace)
    try:
        return await run_once(
            client,
            session=session,
            content=message,
            stdout=stdout,
            stderr=stderr,
            json_output=json_output,
            approver=approver,
        )
    finally:
        await client.aclose()


async def _chat(
    workspace: Path,
    *,
    session: str,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    from .ui.cli import make_reader, open_client, run_chat

    client = await open_client(workspace)
    try:
        reader = make_reader(stdout=stdout)
        return await run_chat(
            client, session=session, reader=reader, stdout=stdout, stderr=stderr
        )
    finally:
        await client.aclose()


async def _session_command(
    workspace: Path,
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        return await _dispatch_sessions(client, args, stdout)
    finally:
        await client.aclose()


async def _dispatch_sessions(
    client: Any, args: argparse.Namespace, stdout: TextIO
) -> int:
    action = args.session_action
    if action == "list":
        stdout.writelines(
            _format_summary(summary) + "\n" for summary in await client.list_sessions()
        )
        return 0
    if action == "fork":
        summary = await client.fork(args.session_id, args.at_seq)
        child = getattr(summary, "id", args.session_id)
        stdout.write(f"{child}\n")
        return 0
    if action == "replay":
        view, _seq = await client.state(args.session_id)
        stdout.write(_render_view(view))
        return 0
    if action == "export":
        content = await client.export(args.session_id, format=args.format)
        stdout.write(content if content.endswith("\n") else content + "\n")
        return 0
    if action == "delete":
        trash_id, _after = await client.delete(
            args.session_id, force=args.force, reason="cli delete"
        )
        stdout.write(f"{trash_id}\n")
        return 0
    if action == "restore":
        restored = await client.restore(args.trash_id)
        stdout.write(f"{restored}\n")
        return 0
    raise ValueError(f"unknown session action {action!r}")


async def _ext_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        action = args.ext_action
        if action == "list":
            generation, extensions = await client.list_extensions()
            stdout.write(f"generation {generation}\n")
            stdout.writelines(
                f"  {row.get('name', '?')} [{row.get('origin', '?')}] "
                f"sha256={str(row.get('sha256', ''))[:12]} "
                f"{row.get('source') or row.get('path', '')}\n"
                for row in extensions
            )
            return 0
        if action == "reload":
            result = await client.reload_extensions(trigger="cli")
            stdout.write(
                f"generation {getattr(result, 'previous_generation', 0)} -> "
                f"{getattr(result, 'generation', 0)} "
                f"changed={getattr(result, 'changed', False)}\n"
            )
            stdout.writelines(
                f"  + {name}\n" for name in getattr(result, "loaded", ())
            )
            stdout.writelines(
                f"  - {name}\n" for name in getattr(result, "unloaded", ())
            )
            stdout.writelines(
                f"  ! {failure.get('name', '?')}: {failure.get('error', '')}\n"
                for failure in getattr(result, "failed", ())
            )
            return 0
        if action == "validate":
            result = await client.validate_extensions(args.target)
            stdout.write(
                f"generation {getattr(result, 'generation', 0)} "
                f"checked={getattr(result, 'checked', 0)} "
                f"valid={getattr(result, 'valid', True)}\n"
            )
            for row in getattr(result, "results", ()):
                marker = "ok" if row.get("ok") else "FAIL"
                stdout.write(
                    f"  [{marker}] {row.get('name', '?')} {row.get('path', '')} "
                    f"{row.get('detail', '')}\n"
                )
            return 0
        if action == "trash":
            result = await client.trash_extensions(
                args.target, reason=args.reason, force=args.force
            )
            stdout.write(
                f"trashed {getattr(result, 'source_path', args.target)}\n"
                f"  id: {getattr(result, 'trash_id', '?')}\n"
                f"  delete_after: {getattr(result, 'delete_after', 0.0)}\n"
                f"  generation {getattr(result, 'previous_generation', 0)} -> "
                f"{getattr(result, 'generation', 0)} "
                f"changed={getattr(result, 'changed', False)}\n"
            )
            return 0
        raise ValueError(f"unknown ext action {action!r}")
    finally:
        await client.aclose()


async def _model_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        action = args.model_action
        if action == "list":
            models = await client.list_models(
                provider=args.provider,
                tier=args.tier,
                selectable_only=args.selectable,
                search=args.search,
            )
            stdout.writelines(
                f"{model.get('provider', '?')}/{model.get('id', '?')} "
                f"[{model.get('tier', '?')}] ctx={model.get('context', '?')} "
                f"tools={model.get('tool_call', '?')}\n"
                for model in models
            )
            return 0
        if action == "show":
            result = await client.show_model(args.ref)
            if not getattr(result, "found", False):
                stdout.write(f"unknown model {args.ref!r}\n")
                return 1
            model = getattr(result, "model", None) or {}
            stdout.writelines(f"{key}: {model[key]}\n" for key in sorted(model))
            return 0
        if action == "refresh":
            result = await client.refresh_models()
            status = getattr(result, "status", None) or {}
            stdout.write(
                f"source={status.get('source', '?')} models={status.get('models', 0)} "
                f"stale={status.get('stale', False)}\n"
            )
            return 0
        if action == "tiers":
            result = await client.model_tiers()
            stdout.write(f"default: {getattr(result, 'default', '?')}\n")
            stdout.write(f"order: {', '.join(getattr(result, 'order', ()))}\n")
            stdout.writelines(
                f"  {name} = {ref} (override)\n"
                for name, ref in sorted((getattr(result, "overrides", None) or {}).items())
            )
            stdout.writelines(
                f"  {name} = {ref} (builtin)\n"
                for name, ref in sorted((getattr(result, "builtin", None) or {}).items())
            )
            return 0
        raise ValueError(f"unknown model action {action!r}")
    finally:
        await client.aclose()


async def _agents_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        rows = await client.list_agents()
        for row in rows:
            kind = "read-only" if row.get("read_only") else "write"
            stdout.write(
                f"{row.get('name', '?')} [{kind}] "
                f"model={row.get('model') or 'inherit'} "
                f"{row.get('description', '')}\n"
            )
        return 0
    finally:
        await client.aclose()


async def _tools_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        rows = await client.list_tools()
        for row in rows:
            kind = "write" if row.get("mutates") else "read"
            stdout.write(
                f"{row.get('name', '?')} [{row.get('bundle', '?')}/{kind}] "
                f"{row.get('description', '')}\n"
            )
        return 0
    finally:
        await client.aclose()


async def _doctor(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        result = await client.doctor(explain_reload=args.explain_reload)
        report = getattr(result, "report", None) or {}
        if args.json:
            stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
            return 0
        _print_doctor(report, stdout)
        return 0 if getattr(result, "ok", True) else 1
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Local daemon commands (no facade)
# ---------------------------------------------------------------------------


async def _daemon_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    from .host import daemon as daemon_mod

    action = args.daemon_action
    if action == "status":
        report = await daemon_mod.status(workspace)
        if args.json:
            stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
        elif report.get("running"):
            stdout.write(
                f"running pid={report.get('pid', '?')} socket={report.get('socket')} "
                f"sessions={report.get('sessions', 0)} "
                f"running_turns={report.get('running', 0)}\n"
            )
        else:
            stdout.write(f"not running socket={report.get('socket')}\n")
        return 0 if report.get("running") else 1
    if action == "stop":
        stopped = await daemon_mod.stop(workspace)
        stdout.write("stopped\n" if stopped else "not running\n")
        return 0
    if action == "logs":
        text = daemon_mod.logs(workspace, lines=args.lines)
        stdout.write(text if text.endswith("\n") or not text else text + "\n")
        return 0
    raise ValueError(f"unknown daemon action {action!r}")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _format_summary(summary: Any) -> str:
    sid = getattr(summary, "id", "?")
    state = getattr(summary, "state", "idle")
    last_seq = getattr(summary, "last_seq", 0)
    viewers = getattr(summary, "viewers", 0)
    title = getattr(summary, "title", "")
    return f"{sid} [{state}] seq={last_seq} viewers={viewers} {title}".rstrip()


def _render_view(view: Any) -> str:
    """Render a folded ``ConversationView`` (or its dict) as a transcript."""
    if isinstance(view, dict):
        return _render_view_dict(view)
    to_dict = getattr(view, "to_dict", None)
    if callable(to_dict):
        return _render_view_dict(to_dict())
    return str(view)


def _render_view_dict(view: dict[str, Any]) -> str:
    """Render a JSON view snapshot as a transcript.

    Prefers the flat computed ``messages`` list the reducer emits; falls back to
    walking each turn's messages. Tools are rendered from the turns.
    """
    lines: list[str] = []
    messages = view.get("messages")
    if not isinstance(messages, list):
        messages = [
            message
            for turn in view.get("turns", ()) or ()
            if isinstance(turn, dict)
            for message in (turn.get("messages") or ())
        ]
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        for block in message.get("blocks", ()) or ():
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not text:
                continue
            if block.get("kind") == "thinking":
                lines.append(f"[thinking] {text}")
            else:
                lines.append(text)
    for turn in view.get("turns", ()) or ():
        if not isinstance(turn, dict):
            continue
        for tool in turn.get("tools", ()) or ():
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "?")
            status = tool.get("status", "?")
            lines.append(f"[tool {name} {status}]")
        phase = turn.get("phase")
        if phase:
            lines.append(f"[turn {phase}]")
    return "\n".join(lines) + ("\n" if lines else "")


def _print_doctor(report: dict[str, Any], stdout: TextIO) -> None:
    stdout.write(f"workspace: {report.get('workspace', '?')}\n")
    providers = report.get("providers", []) or []
    stdout.write(f"providers: {len(providers)}\n")
    stdout.writelines(
        f"  {row.get('name', '?')} ({row.get('kind', '?')})\n" for row in providers
    )
    registry = report.get("registry")
    if isinstance(registry, dict):
        stdout.write(
            f"registry: source={registry.get('source', '?')} "
            f"models={registry.get('models', 0)} stale={registry.get('stale', False)}\n"
        )
    extensions = report.get("extensions")
    if isinstance(extensions, dict):
        stdout.write(
            f"extensions: generation={extensions.get('generation', 0)} "
            f"loaded={extensions.get('loaded', 0)} "
            f"diagnostics={len(extensions.get('diagnostics', []) or [])}\n"
        )
        stdout.writelines(
            f"  ! {row.get('name', '?')}: {row.get('error', row.get('message', ''))}\n"
            for row in extensions.get("diagnostics", []) or []
        )
    mcp = report.get("mcp")
    if isinstance(mcp, dict):
        servers = mcp.get("servers", []) or []
        stdout.write(f"mcp: servers={len(servers)}\n")
        for row in servers:
            health = row.get("health", "?")
            name = row.get("name", "?")
            tools = row.get("tool_count", 0)
            error = row.get("last_error", "")
            suffix = f" error={error}" if error else ""
            stdout.write(f"  {name} [{health}] tools={tools}{suffix}\n")
    reload_info = report.get("reload")
    if isinstance(reload_info, dict):
        stdout.write("reload boundary:\n")
        stdout.write("  hot:\n")
        stdout.writelines(f"    {item}\n" for item in reload_info.get("hot", []))
        stdout.write("  restart only:\n")
        stdout.writelines(
            f"    {item}\n" for item in reload_info.get("restart_only", [])
        )
        note = reload_info.get("note")
        if note:
            stdout.write(f"  {note}\n")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus", description="Nexus: a provider-agnostic agent harness"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init",
        help="Create nexus.toml, SOUL.md, and MEMORY.md without overwriting",
    )

    doctor = sub.add_parser("doctor", help="Validate config, providers, registry, extensions")
    doctor.add_argument(
        "--explain-reload",
        action="store_true",
        help="Explain what is hot vs. requires a daemon restart",
    )
    doctor.add_argument("--json", action="store_true", help="Emit the report as JSON")

    run = sub.add_parser("run", help="Run one turn over the daemon")
    run.add_argument("message", help="Prompt, or - to read from stdin")
    run.add_argument("--session", default="default")
    run.add_argument("--json", action="store_true", help="Stream JSONL event envelopes")

    chat = sub.add_parser("chat", help="Open an interactive line-mode prompt")
    chat.add_argument("--session", default="default")

    replay = sub.add_parser("replay", help="Re-render a session from its log")
    replay.add_argument("session_id")
    replay.add_argument("--json", action="store_true", help="Emit the view model as JSON")

    daemon = sub.add_parser("daemon", help="Manage the workspace daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_action", required=True)
    daemon_status = daemon_sub.add_parser("status", help="Report daemon liveness")
    daemon_status.add_argument("--json", action="store_true")
    daemon_sub.add_parser("stop", help="Gracefully stop the daemon")
    daemon_logs = daemon_sub.add_parser("logs", help="Tail the daemon log")
    daemon_logs.add_argument("--lines", type=int, default=200)

    sessions = sub.add_parser("sessions", help="List, fork, replay, export, delete, restore")
    sessions_sub = sessions.add_subparsers(dest="session_action", required=True)
    sessions_sub.add_parser("list", help="List sessions")
    sessions_fork = sessions_sub.add_parser("fork", help="Branch a session")
    sessions_fork.add_argument("session_id")
    sessions_fork.add_argument("--at-seq", type=int, default=None)
    sessions_replay = sessions_sub.add_parser("replay", help="Re-render a session")
    sessions_replay.add_argument("session_id")
    sessions_export = sessions_sub.add_parser("export", help="Export a consistent prefix")
    sessions_export.add_argument("session_id")
    sessions_export.add_argument(
        "--format", choices=("json", "markdown", "jsonl"), default="markdown"
    )
    sessions_delete = sessions_sub.add_parser("delete", help="Move a session to trash")
    sessions_delete.add_argument("session_id")
    sessions_delete.add_argument("--force", action="store_true")
    sessions_restore = sessions_sub.add_parser("restore", help="Restore a trashed session")
    sessions_restore.add_argument("trash_id")

    ext = sub.add_parser("ext", help="Inspect, reload, or validate extensions")
    ext_sub = ext.add_subparsers(dest="ext_action", required=True)
    ext_sub.add_parser("list", help="List live external modules")
    ext_sub.add_parser("reload", help="Run one extension rebuild")
    ext_validate = ext_sub.add_parser("validate", help="Quarantine-check without swapping")
    ext_validate.add_argument("target", nargs="?", default=None)
    ext_trash = ext_sub.add_parser(
        "trash", help="Move a managed extension file to trash and reload"
    )
    ext_trash.add_argument(
        "target", help="Extension path (workspace-relative or absolute)"
    )
    ext_trash.add_argument("--reason", default="cli trash")
    ext_trash.add_argument(
        "--force",
        action="store_true",
        help="Keep the trashed file even if the reload fails",
    )

    models = sub.add_parser("models", help="Inspect the model registry and tiers")
    models_sub = models.add_subparsers(dest="model_action", required=True)
    models_list = models_sub.add_parser("list", help="List reachable models")
    models_list.add_argument("--provider", default=None)
    models_list.add_argument("--tier", default=None)
    models_list.add_argument("--selectable", action="store_true")
    models_list.add_argument("--search", default=None)
    models_show = models_sub.add_parser("show", help="Show one model")
    models_show.add_argument("ref")
    models_sub.add_parser("refresh", help="Force a catalogue refresh")
    models_sub.add_parser("tiers", help="List tiers")

    agents = sub.add_parser("agents", help="List discovered subagent definitions")
    agents_sub = agents.add_subparsers(dest="agents_action", required=True)
    agents_sub.add_parser("list", help="List subagent definitions")

    tools = sub.add_parser("tools", help="List the model-facing tool catalog")
    tools_sub = tools.add_subparsers(dest="tools_action", required=True)
    tools_sub.add_parser("list", help="List available tools")

    return parser


def _json_error(args: argparse.Namespace, exc: BaseException) -> None:
    from .events import Event

    payload = Event("error", {"message": str(exc)}).to_dict()
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    stdout = sys.stdout
    stderr = sys.stderr
    try:
        if args.command == "init":
            initialize(workspace)
            return 0
        if args.command == "run":
            message = sys.stdin.read() if args.message == "-" else args.message
            approver = None
            if not args.json:
                from .ui.cli import Approver, make_reader

                approver = Approver(make_reader(stdout=stdout), stderr=stderr)
            return asyncio.run(
                _run(
                    workspace,
                    message=message,
                    session=args.session,
                    json_output=args.json,
                    stdout=stdout,
                    stderr=stderr,
                    approver=approver,
                )
            )
        if args.command == "chat":
            return asyncio.run(_chat(workspace, session=args.session, stdout=stdout, stderr=stderr))
        if args.command == "replay":
            return asyncio.run(_replay(workspace, args, stdout))
        if args.command == "daemon":
            return asyncio.run(_daemon_command(workspace, args, stdout, stderr))
        if args.command == "sessions":
            return asyncio.run(
                _session_command(workspace, args, stdout, stderr)
            )
        if args.command == "ext":
            return asyncio.run(_ext_command(workspace, args, stdout))
        if args.command == "models":
            return asyncio.run(_model_command(workspace, args, stdout))
        if args.command == "agents":
            return asyncio.run(_agents_command(workspace, args, stdout))
        if args.command == "tools":
            return asyncio.run(_tools_command(workspace, args, stdout))
        if args.command == "doctor":
            return asyncio.run(_doctor(workspace, args, stdout))
        parser.error(f"unknown command {args.command!r}")
        return 2
    except KeyboardInterrupt:
        stderr.write("Cancelled. Workspace changes may already have occurred.\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI boundary reports, never tracebacks
        if getattr(args, "json", False):
            _json_error(args, exc)
        else:
            stderr.write(f"Error: {exc}\n")
        return 1


async def _replay(workspace: Path, args: argparse.Namespace, stdout: TextIO) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        view, seq = await client.state(args.session_id)
        if getattr(args, "json", False):
            stdout.write(json.dumps({"seq": seq, "view": view}, sort_keys=True) + "\n")
        else:
            stdout.write(_render_view(view))
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main())
