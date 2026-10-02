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
import importlib.util
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

DEFAULT_CONFIG = '''# Nexus reloads configuration before each turn. Unknown keys are errors.
# `models.default` may be a tier name ("medium"), "provider/model", or a bare id.
config_version = 2

[agent]
name = "build"
profile = "coding"
instructions_file = "SOUL.md"
memory_file = "MEMORY.md"

[models]
default = "medium"

[permissions]
# Every tool runs without asking. Use mode = "ask" to approve each call, or
# add rules, e.g. deny = ["Bash(rm -rf*)", "Read(**/.env)"] or ask = ["Bash(git push*)"].
mode = "allow"
write_roots = ["./"]
on_unattended = "deny"
'''

DEFAULT_SOUL = '''# Agent instructions

Complete the user's request in this workspace. Read relevant project
instructions, use tools to inspect, edit, and verify your work, and report
outcomes and limitations.

Configuration and instructions reload at the start of every turn. Workspace
extensions under `.agents/tools/`, `.agents/hooks/`, `.agents/skills/`, and
`.agents/agents/` are hot and take effect without a restart. Core Nexus source
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


def _dev_env() -> bool:
    return os.environ.get("NEXUS_DEV", "").strip().lower() in {"1", "true", "yes", "on"}


def _enter_dev_mode(stderr: TextIO) -> Path:
    """Turn on dev mode for this process and every daemon it spawns (MOCK_PLAN §3.1).

    Sets ``NEXUS_DEV`` (inherited by the daemon), keeps state in an isolated home
    (``~/.nexus/dev`` unless ``NEXUS_HOME`` is already set) and returns a seeded
    sandbox workspace, so no real workspace is ever touched.
    """
    from .devtools.mock.sandbox import DEFAULT_DEV_HOME, ensure_sandbox

    os.environ["NEXUS_DEV"] = "1"
    os.environ.setdefault("NEXUS_HOME", str(DEFAULT_DEV_HOME.expanduser()))
    sandbox = ensure_sandbox(os.environ["NEXUS_HOME"])
    stderr.write(f"dev mode: workspace={sandbox} home={os.environ['NEXUS_HOME']}\n")
    return sandbox


async def _mock(workspace: Path, args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    from .ui.cli import open_client
    from .ui_support.mock_cli import run_mock

    client = await open_client(workspace)
    try:
        return await run_mock(
            client, action=args.mock_action, scenario=args.scenario, speed=args.speed,
            seed=args.seed, stdout=stdout, stderr=stderr, json_output=args.json,
        )
    finally:
        await client.aclose()


async def _chat(workspace: Path, *, session: str, renderer: str = "textual") -> int:
    """Launch the only interactive chat shell over the host client."""
    from .ui.cli import open_client
    if renderer == "ratatui":
        from .ui.ratatui.run import run
    else:
        from .ui.tui.run import run

    client = await open_client(workspace)
    try:
        async def reconnect():
            return await open_client(workspace)

        if renderer == "ratatui":
            return await run(client, session=session, reconnect=reconnect, workspace=workspace)
        return await run(client, session=session, reconnect=reconnect)
    finally:
        await client.aclose()


async def _web(workspace: Path, *, open_browser: bool) -> int:
    """Open the same workspace daemon in a local browser window."""
    from .host import protocol as p
    from .host.daemon import ensure_daemon

    client = await ensure_daemon(workspace, client="web-launch")
    try:
        result = await client.call(p.WebLaunch())
    finally:
        await client.close()
    if not isinstance(result, p.WebLaunchResult):
        raise RuntimeError(  # noqa: TRY004 - daemon protocol failure, not bad input
            getattr(result, "message", "daemon did not return a browser URL")
        )
    if open_browser:
        import webbrowser

        try:
            if webbrowser.open(result.url, new=2):
                print("Opened Nexus in your browser.")
                return 0
        except Exception:  # noqa: BLE001, S110 - browser launch is best-effort
            pass
    print(result.url)
    return 0


def _new_session_id() -> str:
    """A fresh id in the same shape the chat ``/new`` command mints."""
    return f"session-{uuid.uuid4().hex[:8]}"


def _chat_entry(workspace: Path, *, session: str, renderer: str = "textual") -> int:
    """KeyboardInterrupt boundary for Textual's guaranteed terminal restore."""
    try:
        return asyncio.run(_chat(workspace, session=session, renderer=renderer))
    except KeyboardInterrupt:
        return 130


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
        if action == "select":
            result = await client.select_model(args.session, args.ref)
            line = (
                f"{getattr(result, 'provider', '?')}/"
                f"{getattr(result, 'model', '?')}"
            )
            tier = getattr(result, "tier", "")
            if tier:
                line += f" [{tier}]"
            stdout.write(f"{line} (session {args.session}; applies next turn)\n")
            fallback = list(getattr(result, "fallback", ()) or ())
            if fallback:
                stdout.write(f"fallback: {', '.join(fallback)}\n")
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
        if args.agents_action == "current":
            result = await client.current_agent(args.session)
            stdout.write(f"{result.name} ({result.source})\n")
            return 0
        if args.agents_action == "select":
            result = await client.select_agent(args.session, args.name)
            stdout.write(f"{result.name} (session {args.session}; applies next turn)\n")
            return 0
        if args.agents_action == "reset":
            result = await client.reset_agent(args.session)
            stdout.write(f"{result.name} ({result.source}; applies next turn)\n")
            return 0
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


async def _worktrees_command(
    workspace: Path,
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        return await _dispatch_worktrees(client, args, stdout, stderr)
    finally:
        await client.aclose()


async def _dispatch_worktrees(
    client: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    action = args.worktrees_action
    if action == "list":
        result = await client.list_worktrees()
        stdout.write(f"status: {_worktree_text(result.status)}\n")
        stdout.writelines(
                f"{_worktree_text(row.get('child_id'))} "
                f"[{_worktree_text(row.get('lifecycle') or row.get('status'))}] "
                f"dirty={bool(row.get('dirty'))} "
                f"acknowledged={bool(row.get('acknowledged'))}\n"
                for row in result.worktrees
        )
        if result.has_more:
            stdout.write("partial: host worktree list has more entries\n")
        return 0 if result.status == "ok" and not result.has_more else 1

    if action == "inspect":
        result = await client.inspect_worktree(args.child_id)
        stdout.write(f"child: {_worktree_text(result.child_id)}\n")
        stdout.write(f"status: {_worktree_text(result.status)}\n")
        for field in (
            "lifecycle", "dirty", "review_id", "digest", "acknowledged",
            "created_at", "finished_at", "integrated_at",
        ):
            if field in result.record:
                value = result.record[field]
                rendered = _worktree_text(value) if isinstance(value, str) else str(value)
                stdout.write(f"{field}: {rendered}\n")
        return 0 if result.status not in {"partial", "error", "recovery_required"} else 1

    if action == "review":
        return await _worktree_review(client, args, stdout)

    if action == "acknowledge":
        result = await client.acknowledge_worktree(
            args.child_id, args.review_id, args.digest
        )
        stdout.write(
            f"status: {_worktree_text(result.status)}\n"
            f"review: {_worktree_text(result.review_id)}\n"
            f"digest: {_worktree_text(result.digest)}\n"
        )
        return 0 if result.status == "acknowledged" else 1

    if action in {"integrate", "discard"}:
        return await _worktree_mutation(client, args, stdout, stderr)

    raise ValueError(f"unknown worktrees action {action!r}")


async def _worktree_review(client: Any, args: argparse.Namespace, stdout: TextIO) -> int:
    cursor = args.cursor
    page_limit = args.limit
    pages: list[Any] = []
    pinned_review_id = args.review_id
    pinned_digest: str | None = None
    while True:
        page = await client.review_worktree(
            args.child_id,
            review_id=pinned_review_id,
            cursor=cursor,
            limit=page_limit,
        )
        if pinned_review_id is None:
            pinned_review_id = page.review_id
        if pinned_digest is None:
            pinned_digest = page.digest
        elif page.review_id != pinned_review_id or page.digest != pinned_digest:
            raise RuntimeError("review changed during paging; restart from cursor 0")
        pages.append(page)
        if not args.all or not page.has_more:
            break
        if len(pages) >= 500:
            break
        next_cursor = page.cursor + page_limit
        if next_cursor <= cursor:
            raise RuntimeError("host returned a non-advancing worktree review cursor")
        cursor = next_cursor
    final = pages[-1]
    next_cursor = final.cursor + page_limit if final.has_more else None
    payload = {
        "child_id": _worktree_text(final.child_id),
        "status": _worktree_text(final.status),
        "review_id": _worktree_text(final.review_id),
        "digest": _worktree_text(final.digest),
        "entries": [
            _safe_worktree_row(row, include_patch=False)
            for row in pages[0].entries
        ],
        "diff": [
            _safe_worktree_row(row, include_patch=True)
            for page in pages
            for row in page.diff
        ],
        "cursor": pages[0].cursor,
        "next_cursor": next_cursor,
        "has_more": final.has_more,
    }
    if args.json:
        stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        stdout.write(f"status: {payload['status']}\n")
        stdout.write(f"review: {payload['review_id']}\n")
        stdout.write(f"digest: {payload['digest']}\n")
        stdout.write("changed files:\n")
        stdout.writelines(f"  {row['change']} {row['path']}\n" for row in payload["entries"])
        for row in payload["diff"]:
            patch = row.get("patch", "")
            if patch:
                stdout.write(patch if patch.endswith("\n") else patch + "\n")
        if next_cursor is not None:
            stdout.write(f"next cursor: {next_cursor} (use --cursor {next_cursor})\n")
        if args.all and final.has_more:
            stdout.write(f"review paging capped; continue with --cursor {next_cursor}\n")
    incomplete = args.all and final.has_more
    return (
        1
        if incomplete or payload["status"] in {"partial", "error", "recovery_required"}
        else 0
    )


async def _worktree_mutation(
    client: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    operation = args.worktrees_action

    async def mutate(token: str = ""):
        if operation == "integrate":
            return await client.integrate_worktree(
                args.child_id,
                args.review_id,
                args.digest,
                confirmation_token=token,
            )
        return await client.discard_worktree(
            args.child_id,
            force=args.force,
            review_id=args.review_id,
            confirmation_token=token,
        )

    # The first request is always a fresh, token-free preview.
    preview = await mutate()
    if preview.status != "requires_confirmation":
        return _print_worktree_mutation(preview, stdout)

    changed_files: list[str] = []
    review_digest = args.digest if operation == "integrate" else None
    review_id = args.review_id
    force = bool(args.force) if operation == "discard" else False
    if operation == "discard" and not force and not review_id:
        inspected = await client.inspect_worktree(args.child_id)
        review_id = inspected.record.get("review_id")
    if operation == "integrate" or review_id:
        review = await client.review_worktree(
            args.child_id,
            review_id=review_id,
            cursor=0,
            limit=8,
        )
        changed_files = [
            _worktree_text(row.get("path")) for row in review.entries
            if isinstance(row, dict) and row.get("path")
        ]
        if operation == "integrate" and review.digest != args.digest:
            raise RuntimeError("review digest changed; request a fresh preview")
        if not review_digest:
            review_digest = _worktree_text(review.digest)

    stdout.write(f"operation: {_worktree_text(preview.operation or operation)}\n")
    if operation == "discard":
        stdout.write(f"force: {'yes' if force else 'no'}\n")
        if force:
            stdout.write("WARNING: force discard removes the child worktree, including dirty files.\n")
    stdout.write("changed files:\n")
    if changed_files:
        stdout.writelines(f"  {path}\n" for path in changed_files)
    elif operation == "discard" and preview.impact.get("child_dirty"):
        stdout.write("  dirty files are present; the host preview does not enumerate them\n")
    else:
        stdout.write("  (none reported by the host review)\n")
    stdout.write(f"digest: {_worktree_text(review_digest) if review_digest else '(none)'}\n")
    for key in ("parent_clean", "parent_head_matches_base", "child_dirty", "summary"):
        if key in preview.impact:
            value = preview.impact[key]
            rendered = _worktree_text(value) if isinstance(value, str) else str(value)
            stdout.write(f"{key}: {rendered}\n")

    if not args.confirm_token:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            stderr.write(
                "Error: worktree mutation needs an interactive typed confirmation; "
                "use --confirmation-token to authorize the fresh preview token.\n"
            )
            return 2
        confirmation_detail = (
            review_digest if operation == "integrate" else "force" if force else "clean"
        )
        phrase = f"confirm {operation} {args.child_id} {confirmation_detail}"
        prompt_phrase = _worktree_text(phrase)
        stderr.write(f"Type exactly: {prompt_phrase}\n> ")
        stderr.flush()
        try:
            answer = sys.stdin.readline()
        except (EOFError, OSError):
            stderr.write("Confirmation input closed; no changes made.\n")
            return 2
        if not answer:
            stderr.write("Confirmation input closed; no changes made.\n")
            return 2
        answer = answer.rstrip("\r\n")
        if answer != phrase:
            stderr.write("Confirmation did not match; no changes made.\n")
            return 2

    result = await mutate(preview.confirmation_token)
    return _print_worktree_mutation(result, stdout)


def _print_worktree_mutation(result: Any, stdout: TextIO) -> int:
    stdout.write(f"status: {_worktree_text(result.status)}\n")
    if result.operation:
        stdout.write(f"operation: {_worktree_text(result.operation)}\n")
    if result.digest:
        stdout.write(f"digest: {_worktree_text(result.digest)}\n")
    stdout.writelines(f"changed: {_worktree_text(path)}\n" for path in result.changed_paths)
    if result.error:
        stdout.write(f"error: {_worktree_text(result.error)}\n")
    if result.status != "committed":
        return 1
    return 0


def _worktree_text(value: Any) -> str:
    from .ui.cli.render import sanitize

    text = sanitize(value, 4096)
    # Protocol paths are workspace-relative; absolute service paths are never
    # useful in the CLI and may reveal daemon or user directory structure.
    return re.sub(r"(?<![\w])/(?:[^\s,;]+/)*[^\s,;]*", "[path]", text)


def _safe_worktree_row(row: Any, *, include_patch: bool) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    fields = ("path", "change", "binary", "old_mode", "new_mode", "old_sha256", "new_sha256")
    safe = {
        key: (_worktree_text(row[key]) if isinstance(row.get(key), str) else row.get(key))
        for key in fields
        if key in row
    }
    if include_patch and isinstance(row.get("patch"), str):
        patch = row["patch"]
        safe["patch"] = "\n".join(_worktree_text(line) for line in patch.splitlines())
    return safe


async def _doctor(
    workspace: Path, args: argparse.Namespace, stdout: TextIO
) -> int:
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        result = await client.doctor(explain_reload=args.explain_reload)
        report = dict(getattr(result, "report", None) or {})
        from .host_support.install import install_report

        report["install"] = await install_report()
        try:
            import msgspec

            report["update"] = msgspec.structs.asdict(await client.update_status())
        except Exception:  # noqa: BLE001 - the notice is advisory
            report["update"] = None
        if args.json:
            stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
            return 0
        _print_doctor(report, stdout)
        return 0 if getattr(result, "ok", True) else 1
    finally:
        await client.aclose()


async def _claude_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    """Save the claude-agent provider and default model through the daemon."""
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        result = await client.setup_save("claude-agent", args.model)
        stdout.write(f"claude-agent enabled; default model {result.global_model}\n")
        if result.restart_required:
            stdout.write("Restart the daemon to use it: nexus daemon restart\n")
        return 0
    finally:
        await client.aclose()


async def _voice_command(
    workspace: Path, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    """Run a voice lifecycle or transcription request through the daemon."""
    from .ui.cli import open_client

    client = await open_client(workspace)
    try:
        return await _dispatch_voice(client, args, stdout, stderr)
    finally:
        await client.aclose()


async def _dispatch_voice(
    client: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    """Dispatch the CLI voice subcommands using the public client contract."""
    from .client.protocol import FacadeError
    from .host.protocol import VoiceStatusResult, VoiceTranscribeResult

    action = args.voice_action
    try:
        if action == "status":
            result = await client.voice_status()
            if not isinstance(result, VoiceStatusResult):
                raise RuntimeError("daemon returned an unexpected voice status")
            _print_voice_status(result, stdout)
            return 0 if result.enabled else 1

        if action == "download":
            # Invoking `voice download` is the user's explicit consent to fetch
            # the local model. The daemon owns the cache and download lifecycle.
            result = await client.voice_prepare()
            if not isinstance(result, VoiceStatusResult):
                raise RuntimeError("daemon returned an unexpected voice status")
            return await _wait_voice_download(client, result, stdout, stderr)

        if action == "remove":
            result = await client.voice_remove()
            if not isinstance(result, VoiceStatusResult):
                raise RuntimeError("daemon returned an unexpected voice status")
            _print_voice_status(result, stdout)
            return 0 if result.state == "absent" else 1

        if action == "transcribe":
            # Status supplies the configured duration limit. This path must not
            # prepare the voice model: transcribe reports voice_not_ready when
            # the cache is absent, rather than silently downloading it.
            status = await client.voice_status()
            if not isinstance(status, VoiceStatusResult):
                raise RuntimeError("daemon returned an unexpected voice status")
            data = _read_voice_wav(args.file, status.max_seconds)
            result = await client.voice_transcribe(data, uuid.uuid4().hex)
            if not isinstance(result, VoiceTranscribeResult):
                raise RuntimeError("daemon returned an unexpected transcription")
            # Keep stdout machine-friendly: it contains only the transcript.
            stdout.write(result.text)
            if result.text and not result.text.endswith("\n"):
                stdout.write("\n")
            return 0

        raise ValueError(f"unknown voice action {action!r}")
    except FacadeError as exc:
        stderr.write(f"Error: {_voice_error_text(exc.message)}\n")
        return 1
    except (OSError, ValueError) as exc:
        # Do not echo filenames or raw OS exceptions, which may reveal paths.
        message = str(exc) if isinstance(exc, ValueError) else "could not read WAV file"
        stderr.write(f"Error: {_voice_error_text(message)}\n")
        return 1


_VOICE_DOWNLOAD_TIMEOUT = 10 * 60
_VOICE_POLL_SECONDS = 1.0
_VOICE_DOWNLOAD_TERMINAL = {"ready", "error", "unsupported", "disabled"}


async def _wait_voice_download(
    client: Any, initial: Any, stdout: TextIO, stderr: TextIO
) -> int:
    """Wait boundedly for the daemon's background voice preparation task."""
    from .host.protocol import VoiceStatusResult

    deadline = asyncio.get_running_loop().time() + _VOICE_DOWNLOAD_TIMEOUT
    result = initial
    last_progress: tuple[str, int] | None = None
    while result.state not in _VOICE_DOWNLOAD_TERMINAL:
        progress_bucket = int(max(0.0, min(1.0, float(result.progress))) * 10)
        marker = (result.state, progress_bucket)
        if marker != last_progress:
            stderr.write(
                f"Preparing local voice model: {_voice_error_text(result.state)} "
                f"({progress_bucket * 10}%)\n"
            )
            stderr.flush()
            last_progress = marker

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            _print_voice_status(result, stdout)
            stderr.write(
                f"Error: voice preparation did not finish within "
                f"{_VOICE_DOWNLOAD_TIMEOUT} seconds. Check `nexus voice status` and retry.\n"
            )
            return 1
        await asyncio.sleep(min(_VOICE_POLL_SECONDS, remaining))
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            continue
        try:
            result = await asyncio.wait_for(client.voice_status(), timeout=remaining)
        except TimeoutError:
            _print_voice_status(result, stdout)
            stderr.write(
                f"Error: voice preparation did not finish within "
                f"{_VOICE_DOWNLOAD_TIMEOUT} seconds. Check `nexus voice status` and retry.\n"
            )
            return 1
        if not isinstance(result, VoiceStatusResult):
            raise RuntimeError("daemon returned an unexpected voice status")

    _print_voice_status(result, stdout)
    if result.state == "unsupported":
        detail = _voice_error_text(result.message) if result.message else "Voice runtime is unavailable."
        stderr.write(
            f"Error: {detail}\n"
            if result.message and "Install the voice extra" in result.message
            else f"Error: {detail} Install the voice extra "
            "(`uv tool install --force 'nexus-harness[voice]'`). If Nexus was installed "
            "while the daemon was already running, restart it with `nexus daemon restart`.\n"
        )
        return 1
    return 0 if result.state == "ready" else 1


_VOICE_WAV_HARD_LIMIT = 4 * 1024 * 1024
_VOICE_WAV_HEADER_ALLOWANCE = 1024


def _read_voice_wav(path: str | Path, max_seconds: int) -> bytes:
    """Read a WAV only up to the configured audio duration and hard byte cap."""
    from .voice.audio import SAMPLE_RATE

    seconds = max(1, min(120, int(max_seconds)))
    max_bytes = min(
        _VOICE_WAV_HARD_LIMIT,
        _VOICE_WAV_HEADER_ALLOWANCE + SAMPLE_RATE * 2 * seconds,
    )
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise OSError("could not read WAV file") from None
    if len(data) > max_bytes:
        raise ValueError("WAV file exceeds the configured voice audio size limit")
    return data


def _voice_error_text(value: Any) -> str:
    """Bound and redact control characters and absolute paths in CLI errors."""
    text = "".join(char if char.isprintable() else " " for char in str(value))
    text = re.sub(r"(?<![\w])/(?:[^\s,;]+/)*[^\s,;]*", "[path]", text)
    return text[:240]


def _print_voice_status(result: Any, stdout: TextIO) -> None:
    stdout.write(
        f"state: {_voice_error_text(result.state)}\n"
        f"enabled: {'yes' if result.enabled else 'no'}\n"
        f"progress: {max(0.0, min(1.0, float(result.progress))):.0%}\n"
    )
    if result.bytes_total > 0:
        stdout.write(f"download: {max(0, result.bytes_done)}/{result.bytes_total} bytes\n")
    if result.device:
        stdout.write(f"device: {_voice_error_text(result.device)}\n")
    if result.message:
        stdout.write(f"message: {_voice_error_text(result.message)}\n")


# ---------------------------------------------------------------------------
# Local daemon commands (no facade)
# ---------------------------------------------------------------------------


#: ``daemon restart`` waits at most polls x seconds for the old process to exit.
_RESTART_POLLS = 100
_RESTART_POLL_SECONDS = 0.1


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
    if action == "stop" and args.all:
        from .host_support.install import stop_all_daemons

        count = await stop_all_daemons()
        stdout.write(f"stopped {count} daemon{'' if count == 1 else 's'}\n")
        return 0
    if action == "stop":
        stopped = await daemon_mod.stop(workspace)
        stdout.write("stopped\n" if stopped else "not running\n")
        return 0
    if action == "restart":
        await daemon_mod.stop(workspace)
        # Shutdown is acknowledged before the process exits; wait (bounded) so
        # the new client does not reconnect to the daemon that is going away.
        for _ in range(_RESTART_POLLS):
            if not (await daemon_mod.status(workspace)).get("running"):
                break
            await asyncio.sleep(_RESTART_POLL_SECONDS)
        else:
            stderr.write("Error: the daemon did not stop; try `nexus daemon stop`\n")
            return 1
        client = await daemon_mod.ensure_daemon(workspace, client="cli-restart")
        await client.close()
        report = await daemon_mod.status(workspace)
        stdout.write(f"restarted pid={report.get('pid', '?')}\n")
        return 0
    if action == "logs":
        text = daemon_mod.logs(workspace, lines=args.lines)
        stdout.write(text if text.endswith("\n") or not text else text + "\n")
        return 0
    raise ValueError(f"unknown daemon action {action!r}")


async def _update(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Upgrade through uv, then restart every daemon that was running.

    This process still holds the old code, so the restart runs the freshly
    installed ``nexus`` binary in child processes.
    """
    import shutil
    import subprocess

    from .host_support import install

    method = install.install_method()
    if method == "editable":
        stderr.write("Error: this is an editable install; update it with `git pull`.\n")
        return 1
    uv = install.find_uv()
    if uv is None or method == "pip":
        stderr.write(
            "Error: Nexus was not installed with uv. Reinstall with the one-line "
            "installer from the README, then `nexus update` works.\n"
        )
        return 1
    source = install.install_source()
    command = install.update_command(
        uv,
        source,
        extras=install.installed_extras(),
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        channel=args.channel,
        version=args.release,
        ref=args.ref,
    )
    if command is None:
        stderr.write(
            "Error: this install did not come from PyPI or git, so `nexus update` "
            "cannot pick a release. Re-run the installer from the README, or use "
            "`nexus update --channel git`.\n"
        )
        return 1
    before = install.package_version()
    running = await install.running_daemons()
    if source == "git" and args.channel == "stable" and not args.release:
        stdout.write(
            "Moving this install from git to PyPI releases "
            "(use --channel git to stay on git).\n"
        )
    stdout.write(f"Updating nexus (currently {before}) ...\n")
    stdout.flush()
    code = await asyncio.to_thread(install.run_update, command)
    if code != 0:
        stderr.write(f"Error: uv exited with status {code}; Nexus was not changed.\n")
        return 1
    binary = shutil.which("nexus") or "nexus"
    after = install.installed_version_after_update(binary) or before
    stdout.write(
        f"Nexus is already up to date ({after}).\n"
        if after == before
        else f"Updated nexus {before} -> {after}.\n"
    )
    if args.no_restart or not running:
        if running:
            stdout.write("Running daemons keep the old code; `nexus daemon stop --all`.\n")
        return 0
    await install.stop_all_daemons()
    failed = 0
    for entry in running:
        workspace = entry.get("workspace") or ""
        if not workspace:
            continue
        result = await asyncio.to_thread(
            subprocess.run,
            [binary, "--workspace", workspace, "daemon", "restart"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            stdout.write(f"Restarted daemon for {workspace}\n")
        else:
            failed += 1
            stderr.write(f"Could not restart the daemon for {workspace}\n")
    return 1 if failed else 0


async def _auth_command(args: argparse.Namespace, stdout: TextIO) -> int:
    """Credential operations are deliberately local: never open a daemon RPC."""
    from .auth.codex import CodexOAuthManager

    manager = CodexOAuthManager(profile=args.profile)
    if args.auth_action == "status":
        logged_in = await manager.status()
        stdout.write("logged in\n" if logged_in else "not logged in\n")
        return 0 if logged_in else 1
    if args.auth_action == "logout":
        await manager.logout()
        stdout.write("logged out\n")
        return 0
    if args.auth_action == "login":
        if args.headless:
            await manager.device_login(notify=stdout.write)
        else:
            await manager.browser_login(notify=stdout.write)
        stdout.write("\nlogin complete\n")
        return 0
    raise ValueError(f"unknown auth action {args.auth_action!r}")


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
    install = report.get("install")
    if isinstance(install, dict):
        stdout.write(
            f"install: nexus {install.get('version', '?')} via {install.get('method', '?')}"
            f" ({install.get('python', '?')})\n"
        )
        from .host_support.update_check import notice

        line = notice(report["update"]) if isinstance(report.get("update"), dict) else ""
        if line:
            stdout.write(f"  update: {line}\n")
        stdout.writelines(f"  warning: {w}\n" for w in install.get("warnings", ()) or ())
    database = report.get("database")
    if isinstance(database, dict):
        schema = database.get("schema_user_version")
        schema_text = str(schema) if type(schema) is int and schema >= 0 else "?"
        quick_check = database.get("quick_check")
        if quick_check not in ("ok", "issues", "unavailable"):
            quick_check = "?"
        size = database.get("size_bytes")
        size_text = str(size) if type(size) is int and size >= 0 else "?"
        wal_size = database.get("wal_size_bytes")
        wal_text = str(wal_size) if type(wal_size) is int and wal_size >= 0 else "?"
        path = database.get("path")
        if isinstance(path, str):
            path = "".join(char for char in path if char.isprintable())[:160]
        else:
            path = ""
        path_text = f" path={path}" if path else ""
        stdout.write(
            f"database: quick_check={quick_check} schema={schema_text} "
            f"size={size_text}B wal={wal_text}B{path_text}\n"
        )
    legacy_extensions = report.get("legacy_extensions_pending")
    if isinstance(legacy_extensions, list):
        documented = {
            "skills", "agents", "tools", "hooks", "providers", "mcp.json",
            "hooks.toml", "nexus.toml",
        }
        names = [
            name for name in legacy_extensions[:8]
            if isinstance(name, str) and name in documented
        ]
        if names:
            entries = ", ".join(f".nexus/{name}" for name in names)
            stdout.write(
                f"WARNING: legacy extensions found ({entries}); move them to "
                ".agents/ to keep them active.\n"
            )
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
    _print_registry_mismatches(report.get("registry_mismatches"), stdout)
    voice = report.get("voice")
    if isinstance(voice, dict):
        stdout.write(
            f"voice: state={_voice_error_text(voice.get('state', '?'))} "
            f"enabled={'yes' if voice.get('enabled') else 'no'}"
        )
        progress = voice.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            stdout.write(f" progress={max(0.0, min(1.0, float(progress))):.0%}")
        stdout.write("\n")
        if voice.get("device"):
            stdout.write(f"  device: {_voice_error_text(voice['device'])}\n")
        if voice.get("message"):
            stdout.write(f"  message: {_voice_error_text(voice['message'])}\n")
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


def _print_registry_mismatches(mismatches: Any, stdout: TextIO) -> None:
    """Render the aggregated catalogue defects, truthfully and bounded (PLAN §15.5).

    Counts and descriptors only; the raw provider detail is never in the report
    to render. ``truncated`` is stated plainly so a capped scan is never mistaken
    for a complete one.
    """
    if not isinstance(mismatches, dict):
        return
    count = mismatches.get("count", 0)
    if not count:
        stdout.write("registry mismatches: none\n")
        return
    scanned = mismatches.get("sessions_scanned", 0)
    sessions = mismatches.get("sessions_with_mismatches", 0)
    truncated = " (scan truncated)" if mismatches.get("truncated") else ""
    stdout.write(
        f"registry mismatches: {count} across {sessions}/{scanned} sessions"
        f"{truncated}\n"
    )
    stdout.writelines(
        f"  by {field}: {_format_tally(mismatches.get(f'by_{field}'))}\n"
        for field in ("provider", "model", "reason")
    )
    stdout.writelines(
        f"  - {_format_mismatch_sample(sample)}\n"
        for sample in mismatches.get("samples", []) or []
    )


def _format_tally(tally: Any) -> str:
    if not isinstance(tally, dict) or not tally:
        return "none"
    return ", ".join(f"{key}={tally[key]}" for key in sorted(tally))


def _format_mismatch_sample(sample: Any) -> str:
    if not isinstance(sample, dict):
        return "?"
    parts = [
        f"session={sample.get('session', '?')}",
        f"provider={sample.get('provider') or '(none)'}",
        f"model={sample.get('model') or '(none)'}",
        f"reason={sample.get('reason') or 'unknown'}",
    ]
    if sample.get("feature"):
        parts.append(f"feature={sample['feature']}")
    if sample.get("ts") is not None:
        parts.append(f"ts={sample['ts']}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus", description="Nexus: a provider-agnostic agent harness"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--version", action="store_true", help="Print the version and exit")
    parser.add_argument(
        "--dev", action="store_true",
        help="Dev mode: isolated home, sandbox workspace and /mock scenarios (also NEXUS_DEV=1)",
    )
    parser.add_argument("--session", default=None, help="Reopen this session in chat")
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(command="chat")

    sub.add_parser(
        "init",
        help="Create nexus.toml, SOUL.md, and MEMORY.md without overwriting",
    )

    claude = sub.add_parser("claude", help="Claude Pro/Max subscription provider")
    claude_sub = claude.add_subparsers(dest="claude_action", required=True)
    claude_init = claude_sub.add_parser(
        "init", help="Enable claude-agent in ~/.nexus/config.toml and make it the default"
    )
    claude_init.add_argument("--model", default="", help="Model id (newest when omitted)")

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

    chat = sub.add_parser("chat", help="Open the interactive Textual chat")
    chat.add_argument("--renderer", choices=("auto", "ratatui", "textual"), default="auto", help="Terminal renderer: auto uses the native Ratatui client when its executable is installed, otherwise Textual")
    chat.add_argument(
        "--session",
        default=None,
        help="Reopen this session id (default: start a new session)",
    )

    web = sub.add_parser("web", help="Open the workspace in a local browser")
    web.add_argument("--no-browser", action="store_true", help="Print the one-time launch URL")

    for dev_capable in (run, chat, web):  # `nexus chat --dev` as well as `nexus --dev chat`
        dev_capable.add_argument("--dev", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    mock = sub.add_parser("mock", help="Dev mode: run scripted mock scenarios headlessly")
    mock.add_argument("mock_action", choices=["list", "run", "all", "clean"])
    mock.add_argument("scenario", nargs="?", default="", help="Scenario name (for `run`)")
    mock.add_argument("--speed", type=float, default=0.0, help="Pacing multiplier (0 = instant)")
    mock.add_argument("--seed", type=int, default=0)
    mock.add_argument("--json", action="store_true", help="One JSON object per scenario")

    replay = sub.add_parser("replay", help="Re-render a session from its log")
    replay.add_argument("session_id")
    replay.add_argument("--json", action="store_true", help="Emit the view model as JSON")

    searchserver = sub.add_parser("searchserver", help="Manage the local SearXNG Docker service")
    searchserver.add_subparsers(dest="searchserver_action", required=True).add_parser("start", help="Start SearXNG with Docker Compose")

    restart = sub.add_parser("restart", help="Restart the workspace daemon (alias for daemon restart)")
    restart.set_defaults(command="daemon", daemon_action="restart")

    daemon = sub.add_parser("daemon", help="Manage the workspace daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_action", required=True)
    daemon_status = daemon_sub.add_parser("status", help="Report daemon liveness")
    daemon_status.add_argument("--json", action="store_true")
    daemon_stop = daemon_sub.add_parser("stop", help="Gracefully stop the daemon")
    daemon_stop.add_argument(
        "--all", action="store_true", help="Stop every workspace's daemon"
    )
    daemon_sub.add_parser("restart", help="Stop the daemon and start a fresh one")
    daemon_logs = daemon_sub.add_parser("logs", help="Tail the daemon log")
    daemon_logs.add_argument("--lines", type=int, default=200)

    update = sub.add_parser("update", help="Upgrade Nexus and restart running daemons")
    update.add_argument(
        "--channel",
        choices=("stable", "git"),
        default="stable",
        help="stable = PyPI releases (default); git = the newest code from GitHub",
    )
    update.add_argument("--ref", help="Branch, tag or commit for --channel git (default: main)")
    update.add_argument("--version", dest="release", help="Install exactly this release (e.g. 0.1.0)")
    update.add_argument(
        "--no-restart", action="store_true", help="Leave running daemons on the old version"
    )

    auth = sub.add_parser("auth", help="Manage local provider credentials")
    auth_sub = auth.add_subparsers(dest="auth_provider", required=True)
    codex_auth = auth_sub.add_parser("codex", help="Experimental ChatGPT OAuth")
    codex_sub = codex_auth.add_subparsers(dest="auth_action", required=True)
    login = codex_sub.add_parser("login", help="Log in locally (no daemon RPC)")
    login.add_argument("--profile", default="default")
    login.add_argument("--headless", action="store_true", help="Use device flow")
    status = codex_sub.add_parser("status", help="Check local credential presence")
    status.add_argument("--profile", default="default")
    logout = codex_sub.add_parser("logout", help="Delete local credential")
    logout.add_argument("--profile", default="default")

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
    models_select = models_sub.add_parser(
        "select", help="Set a session's model for its subsequent turns"
    )
    models_select.add_argument("ref", help="Tier name, provider/model, or model id")
    models_select.add_argument("--session", default="default")

    agents = sub.add_parser("agents", help="List discovered subagent definitions")
    agents_sub = agents.add_subparsers(dest="agents_action", required=True)
    agents_sub.add_parser("list", help="List subagent definitions")
    agent_select = agents_sub.add_parser("select", help="Select root agent for a session")
    agent_select.add_argument("name")
    agent_select.add_argument("--session", default="default")
    agent_current = agents_sub.add_parser("current", help="Show the session's root agent")
    agent_current.add_argument("--session", default="default")
    agent_reset = agents_sub.add_parser("reset", help="Clear root-agent override")
    agent_reset.add_argument("--session", default="default")

    tools = sub.add_parser("tools", help="List the model-facing tool catalog")
    tools_sub = tools.add_subparsers(dest="tools_action", required=True)
    tools_sub.add_parser("list", help="List available tools")

    voice = sub.add_parser("voice", help="Manage local voice transcription")
    voice_sub = voice.add_subparsers(dest="voice_action")
    voice.set_defaults(voice_action="status")
    voice_sub.add_parser("status", help="Show local voice model status")
    voice_sub.add_parser("download", help="Download and prepare the local voice model")
    voice_sub.add_parser("remove", help="Remove the cached local voice model")
    voice_transcribe = voice_sub.add_parser(
        "transcribe", help="Transcribe a WAV file with the cached local model"
    )
    voice_transcribe.add_argument("file", metavar="FILE.wav")

    worktrees = sub.add_parser("worktrees", help="Inspect and manage child worktrees")
    worktrees_sub = worktrees.add_subparsers(dest="worktrees_action", required=True)
    worktrees_sub.add_parser("list", help="List daemon-owned child worktrees")
    worktree_inspect = worktrees_sub.add_parser("inspect", help="Inspect one child worktree")
    worktree_inspect.add_argument("child_id")
    worktree_review = worktrees_sub.add_parser("review", help="Read a finalized worktree review")
    worktree_review.add_argument("child_id")
    worktree_review.add_argument("--review-id", default=None)
    worktree_review.add_argument("--cursor", type=int, default=0)
    worktree_review.add_argument("--limit", type=int, choices=range(1, 9), default=8)
    worktree_review.add_argument("--all", action="store_true", help="Read every bounded page")
    worktree_review.add_argument("--json", action="store_true", help="Emit review data as JSON")
    worktree_ack = worktrees_sub.add_parser("acknowledge", help="Acknowledge an exact review")
    worktree_ack.add_argument("child_id")
    worktree_ack.add_argument("review_id")
    worktree_ack.add_argument("digest")
    worktree_integrate = worktrees_sub.add_parser("integrate", help="Integrate an acknowledged review")
    worktree_integrate.add_argument("child_id")
    worktree_integrate.add_argument("review_id")
    worktree_integrate.add_argument("digest")
    worktree_integrate.add_argument(
        "--confirm-token", "--confirmation-token", dest="confirm_token",
        action="store_true",
        help="Authorize the token returned by this invocation's fresh host preview",
    )
    worktree_discard = worktrees_sub.add_parser("discard", help="Discard an owned child worktree")
    worktree_discard.add_argument("child_id")
    worktree_discard.add_argument("--force", action="store_true")
    worktree_discard.add_argument("--review", dest="review_id", default=None)
    worktree_discard.add_argument(
        "--confirm-token", "--confirmation-token", dest="confirm_token",
        action="store_true",
        help="Authorize the token returned by this invocation's fresh host preview",
    )

    return parser


def _json_error(args: argparse.Namespace, exc: BaseException) -> None:
    from .events import Event

    message = _worktree_text(str(exc)) if args.command == "worktrees" else str(exc)
    payload = Event("error", {"message": message}).to_dict()
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from .host_support.install import package_version

        from .host_support.update_check import notice, update_status

        line = notice(update_status(cached_only=True))
        print(f"nexus {package_version()}" + (f" ({line})" if line else ""))
        return 0
    workspace = args.workspace.resolve()
    if args.command == "mock" and args.mock_action == "run" and not args.scenario:
        parser.error("`nexus mock run` needs a scenario name")
    if args.dev or args.command == "mock" or _dev_env():
        workspace = _enter_dev_mode(sys.stderr)
    stdout = sys.stdout
    stderr = sys.stderr
    try:
        if args.command == "searchserver":
            from .host_support.searchserver import start

            return start(stdout, stderr)
        if args.command == "init":
            initialize(workspace)
            return 0
        if args.command == "run":
            message = sys.stdin.read() if args.message == "-" else args.message
            approver = None
            if not args.json:
                from .ui.cli.approve import Approver

                async def read_approval(prompt: str) -> str:
                    return await asyncio.to_thread(input, prompt)

                approver = Approver(read_approval, stderr=stderr)
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
            if not (
                sys.stdin.isatty()
                and sys.stdout.isatty()
                and os.environ.get("TERM", "dumb") != "dumb"
            ):
                stderr.write(
                    "Error: `nexus chat` requires an interactive terminal (stdin and stdout TTYs). "
                    "Use `nexus run <prompt>` for piped or non-interactive input.\n"
                )
                return 2
            renderer = getattr(args, "renderer", "auto")
            if renderer == "auto":
                from .ui.ratatui.run import available as native_available
                renderer = "ratatui" if native_available() else "textual"
            if renderer == "textual" and importlib.util.find_spec("textual") is None:
                stderr.write("Error: Textual is required for `nexus chat`; reinstall Nexus with its runtime dependencies.\n")
                return 1
            try:
                if renderer == "ratatui":
                    return _chat_entry(workspace, session=args.session or _new_session_id(), renderer=renderer)
                return _chat_entry(workspace, session=args.session or _new_session_id())
            except ModuleNotFoundError as exc:
                if exc.name == "textual":
                    stderr.write("Error: Textual is required for chat; reinstall Nexus with its runtime dependencies.\n")
                    return 1
                raise
        if args.command == "mock":
            return asyncio.run(_mock(workspace, args, stdout, stderr))
        if args.command == "web":
            return asyncio.run(_web(workspace, open_browser=not args.no_browser))
        if args.command == "replay":
            return asyncio.run(_replay(workspace, args, stdout))
        if args.command == "daemon":
            return asyncio.run(_daemon_command(workspace, args, stdout, stderr))
        if args.command == "auth":
            return asyncio.run(_auth_command(args, stdout))
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
        if args.command == "voice":
            return asyncio.run(_voice_command(workspace, args, stdout, stderr))
        if args.command == "worktrees":
            return asyncio.run(_worktrees_command(workspace, args, stdout, stderr))
        if args.command == "claude":
            return asyncio.run(_claude_command(workspace, args, stdout, stderr))
        if args.command == "doctor":
            return asyncio.run(_doctor(workspace, args, stdout))
        if args.command == "update":
            if args.ref and args.channel != "git":
                parser.error("--ref needs --channel git")
            if args.release and args.channel == "git":
                parser.error("--version cannot be combined with --channel git")
            return asyncio.run(_update(args, stdout, stderr))
        parser.error(f"unknown command {args.command!r}")
        return 2
    except KeyboardInterrupt:
        if args.command == "chat":
            return 130
        if args.command == "voice" and args.voice_action == "download":
            stderr.write("Voice download interrupted; preparation may continue in the daemon.\n")
            return 130
        stderr.write("Cancelled. Workspace changes may already have occurred.\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI boundary reports, never tracebacks
        if getattr(args, "json", False):
            _json_error(args, exc)
        elif args.command == "worktrees":
            stderr.write(f"Error: {_worktree_text(str(exc))}\n")
        elif args.command == "voice":
            stderr.write(f"Error: {_voice_error_text(str(exc))}\n")
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
