"""Read-only ownership menus shared by the Ratatui and desktop host bridges."""
from __future__ import annotations

import json

from ...ui_support.text import escape_controls


def bounded(value, limit=240):
    return escape_controls(str(value if value is not None else "unknown"))[:limit]


def tree_menu(tree):
    rows = []
    lines = ["Ownership (not fork ancestry) · read only · Refresh to update"]
    if tree.get("unknown_ownership"):
        lines.append("Legacy children with unknown ownership are listed separately")

    def visit(node, depth=0):
        identity, namespace = node["id"], node["namespace"]
        status = bounded(node.get("status"))
        name = bounded(node.get("agent_type") or node.get("title") or identity, 100)
        facts = [f"{namespace}/{identity}", status]
        for key in ("task", "description", "current_tool", "elapsed_s", "tokens", "cost_usd", "usage_bounded", "branch", "worktree", "result", "error", "kind", "resolved_model", "tier", "owner_id", "owner_turn_id", "running_children", "handoff_count", "last_activity", "idle_seconds", "logical_session_id", "parent_id", "fork_seq"):
            if node.get(key) is not None:
                facts.append(f"{key}: {bounded(node[key], 2000 if key in {'task', 'result'} else 240)}")
        missing = [key for key in ("task", "resolved_model", "tier", "tokens", "cost_usd") if node.get(key) is None]
        if missing:
            facts.append("Unavailable evidence: " + ", ".join(missing))
        rows.append(("  " * depth + f"{name} · {status}", {
            "kind": "ownership_node", "id": identity, "namespace": namespace,
            "facts": facts,
        }))
        for child in node.get("children", []):
            visit(child, depth + 1)

    for root in tree.get("nodes", []):
        visit(root)
    if tree.get("unattached"):
        lines.append("Legacy children with unknown ownership (not inferred)")
        for child in tree["unattached"]:
            visit(child)
    if tree.get("truncated"):
        lines.append("Tree truncated at host bounds")
    return rows, lines


def transcript_lines(result):
    lines = [f"Read-only durable transcript · {result.namespace}/{result.session}",
             "Request unavailable" if not result.request else "Last durable request available via Request details",
             "Bounded tail; earlier records may be omitted" if result.truncated else "Durable replay"]
    for turn in result.view.get("turns", []):
        for message in turn.get("messages", []):
            lines.append(f"{message.get('role', 'unknown')}:")
            for block in message.get("blocks", []):
                if block.get("text"):
                    lines.append(bounded(block["text"], 16_000))
        for tool in turn.get("tools", []):
            lines.append(f"Tool: {bounded(tool.get('name'))} · {bounded(tool.get('status'))}")
            for key in ("input", "result", "display", "error"):
                if tool.get(key):
                    lines.append(bounded(tool[key], 4000))
        if turn.get("error"):
            lines.append(bounded(turn["error"], 2000))
    return "\n".join(lines[:1000]).splitlines()


def request_lines(result):
    return "\n".join(["Last durable request (not a live context estimate)",
            bounded(json.dumps(result.request, indent=2, ensure_ascii=False), 64_000)
            if result.request else "No persisted request is available"]).splitlines()
