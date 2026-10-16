"""Labelled rows for an external worker: Claude Code (plan CLAUDE_CODE_ZMX_PLAN 5C) or Antigravity ``agy``.

``ConversationView.external`` (the merged ``external.started`` and
``external.finished`` payloads) becomes two native transcript blocks: a header
that states the launch authority (Claude ran its own tools; Nexus permissions
did not mediate them) and a footer with the terminal status, transcript path
and usage, which is reported as *unknown*, never as zero. ``needs_attention``
is its own state and shows the manual instruction in full. Values are read
from the durable reduction only, so live and replay render the same.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .text import escape_controls

#: Terminal status -> (headline, block status). ``block status`` drives colour.
_STATUS = {
    "completed": ("completed", "done"),
    "needs_attention": ("NEEDS ATTENTION - Nexus stopped it and approved nothing", "attention"),
    "failed": ("failed", "failed"),
    "interrupted": ("interrupted (partial work; not a success)", "failed"),
    "cancelled": ("cancelled", "failed"),
}
_FIELD_LIMIT = 4000  # the manual instruction must stay whole; anything longer is announced


def _clip(value: object, limit: int = _FIELD_LIMIT) -> str:
    # One line per labelled row: an embedded newline would start an unlabelled row.
    text = escape_controls(re.sub(r"[\r\n]+", " ", str(value)))
    return text if len(text) <= limit else text[:limit] + f"… [clipped, {len(text) - limit} more characters]"


_CLAUDE = {"name": "Claude Code", "session": "Claude session", "calls": "Claude tool calls",
           "default_tools": "Claude Code default set",
           "authority": "Claude Code ran its own tools; Nexus permissions did not mediate them",
           "provenance": "Claude Code (own tools)", "waiting": "Claude Code is waiting for you"}
_AGY = {"name": "Antigravity (agy)", "session": "Antigravity session", "calls": "Antigravity tool calls",
        "default_tools": "agy default set (inherited)",
        "authority": "agy ran its own tools; Nexus permissions did not mediate them",
        "provenance": "Antigravity (agy) own tools", "waiting": "Antigravity (agy) is waiting for you"}
#: Terminal token keys -> label; other integer keys are shown under their own (escaped) name.
_TOKEN_LABELS = (("input_tokens", "input"), ("prompt_tokens", "input"), ("output_tokens", "output"),
                 ("completion_tokens", "output"), ("cache_read_tokens", "cache read"),
                 ("cache_creation_tokens", "cache write"), ("cache_write_tokens", "cache write"),
                 ("cached_tokens", "cached"), ("thinking_tokens", "thinking"), ("reasoning_tokens", "thinking"),
                 ("total_tokens", "total"))


def _profile(external: Mapping[str, Any]) -> dict[str, str]:
    # An unknown or absent provider is a legacy Claude Code payload.
    return _AGY if str(external.get("provider") or "").lower() in ("antigravity", "agy") else _CLAUDE


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _token_usage(usage: object) -> str:
    """Reported terminal tokens as labelled counts, or ``""`` when none were reported."""
    if not isinstance(usage, Mapping):
        return ""
    known = {key for key, _ in _TOKEN_LABELS}
    parts = [f"{label} {usage[key]:,}" for key, label in _TOKEN_LABELS if _count(usage.get(key))]
    parts += [f"{escape_controls(str(key))[:40]} {value:,}" for key, value in usage.items()
              if key not in known and _count(value)]
    return ", ".join(parts) + " tokens" if parts else ""


def _csv(value: object) -> str:
    return ", ".join(str(item) for item in value) if isinstance(value, (list, tuple)) and value else ""


def is_external(external: Mapping[str, Any] | None) -> bool:
    return isinstance(external, Mapping) and bool(external)


def header_rows(external: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Authority rows from ``external.started``; absent fields are omitted."""
    tools = external.get("tools")
    prof = _profile(external)
    agy = prof is _AGY
    rows: list[tuple[str, str]] = [
        ("Provider", prof["name"] + " (external worker)"),
        ("Model", str(external.get("model") or "unknown")),
        ("Workspace", str(external.get("workspace") or "unknown")),
        ("Permission mode", str(external.get("permission_mode") or "unknown")),
        ("Tools", _csv(tools) or (prof["default_tools"] if "tools" in external else "unknown")),
    ]
    for label, key in (("Allowed tools", "allowed_tools"), ("Disallowed tools", "disallowed_tools")):
        if _csv(external.get(key)):
            rows.append((label, _csv(external[key])))
    if "restricted" in external:
        rows.append(("Restricted", "yes" if external["restricted"] else "no"))
    if agy:
        inherits = external.get("inherits_agy_settings", True)
        rows.append(("Settings sources", "inherited from agy (user/global settings and plugins)" if inherits
                     else str(external.get("setting_sources") or "none")))
        rows.append(("MCP", "inherited from agy (servers and plugins)" if external.get("mcp") == "agy-inherited"
                     else "explicit config" if external.get("mcp") == "explicit" else "none"))
    else:
        rows.append(("Settings sources", str(external.get("setting_sources") or "none")))
        rows.append(("MCP", "explicit config" if external.get("mcp") == "explicit" else "none"))
    if external.get("read_only"):
        rows.append(("Role", "read-only role"))
    if external.get("timeout_seconds"):
        rows.append(("Timeout", f"{external['timeout_seconds']:g}s"))
    if external.get("session"):
        rows.append((prof["session"], str(external["session"])))
    rows.append(("Authority", str(external.get("authority") or prof["authority"])))
    rows.append(("Tool rows", "marked " + str(external.get("provenance") or prof["provenance"])
                 + "; Nexus did not execute them"))
    return rows


def footer_rows(external: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Terminal rows from ``external.finished``."""
    status = str(external.get("status") or "unknown")
    prof = _profile(external)
    rows: list[tuple[str, str]] = [("Status", _STATUS.get(status, (status, ""))[0])]
    if external.get("attention"):
        # The worker's reason already names the exact manual command. No resume
        # hint: a startup block never created a session, and resuming outside
        # Nexus would drop the worker's permission policy.
        rows.append(("Needs attention", str(external["attention"])))
    if external.get("error") and not external.get("attention"):
        rows.append(("Error", str(external["error"])))
    if external.get("stop_event"):
        rows.append(("Stop event", str(external["stop_event"])))
    if external.get("tool_calls") is not None:
        rows.append((prof["calls"], str(external["tool_calls"])))
    if prof is _AGY:
        if external.get("agy_session"):
            rows.append((prof["session"], str(external["agy_session"])))
        if external.get("agy_permission_mode"):
            rows.append(("Effective permission mode", str(external["agy_permission_mode"])))
        if external.get("mode_note"):
            rows.append(("Mode fallback", str(external["mode_note"])))
        if _csv(external.get("denied_actions")):
            rows.append(("Denied by agy", _csv(external["denied_actions"])))
        if _csv(external.get("agy_tools")):
            rows.append(("Tools", _csv(external["agy_tools"])))
        rows.append(("Usage", _token_usage(external.get("usage"))
                     or "unknown (agy reported no tokens to Nexus; this is not zero)"))
        rows.append(("Cost", "unknown (not estimated)"))
    else:
        rows.append(("Usage", "unknown (Claude Code reports none to Nexus; this is not zero)"))
    if external.get("elapsed_seconds") is not None:
        rows.append(("Elapsed", f"{external['elapsed_seconds']:g}s"))
    rows.append(("Transcript", str(external.get("transcript_path") or "unavailable")))
    for note in external.get("clip_notes") or ():
        rows.append(("Clipped", str(note)))
    return rows


def _text(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label}: {_clip(value)}" for label, value in rows)


def external_blocks(agent_id: str, external: Mapping[str, Any] | None) -> tuple[list[dict], list[dict]]:
    """``(header, footer)`` transcript blocks (kind ``external``); empty for Nexus sessions."""
    if not is_external(external):
        return [], []
    model = str(external.get("model") or "")
    name = _profile(external)["name"]
    header = {"id": f"{agent_id}:external", "kind": "external", "gap": 1, "status": "running" if not external.get("finished") else "done",
              "title": f"{name} worker" + (f" · {model}" if model else "") + " · independent tools",
              "text": _text(header_rows(external))}
    footer: list[dict] = []
    if external.get("finished"):
        status = str(external.get("status") or "unknown")
        headline, block_status = _STATUS.get(status, (status, "failed"))
        header["status"] = "attention" if block_status == "attention" else header["status"]
        footer.append({"id": f"{agent_id}:external:end", "kind": "external", "gap": 1, "status": block_status or "failed",
                       "title": f"{name} " + headline, "text": _text(footer_rows(external))})
    return [header], footer


def task_note(external: Mapping[str, Any] | None) -> str:
    """One compact line for the parent's Task card: attention or unknown usage."""
    if not is_external(external) or not external.get("finished"):
        return ""
    if external.get("status") == "needs_attention":
        return "Needs attention: " + _clip(external.get("attention") or _profile(external)["waiting"], 300)
    return ""


__all__ = ["external_blocks", "footer_rows", "header_rows", "is_external", "task_note"]
