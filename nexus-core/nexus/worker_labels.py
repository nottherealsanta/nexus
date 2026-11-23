"""Display labels for external worker children.

Kept outside the transport package so storage, runtime and the registry can share it without an import cycle.
"""
from __future__ import annotations

from collections.abc import Mapping

LABELS = {"claude-code": "PTY · Claude Code", "agy": "ACP · Antigravity"}
LEGACY_LABEL = "CLI · Antigravity"
_OLD = ("ZMX · Claude Code", "ZMX · Antigravity")
_LABEL_PROVIDERS = {
    "ZMX · Claude Code": "claude-code", "PTY · Claude Code": "claude-code",
    "ZMX · Antigravity": "agy", "ACP · Antigravity": "agy", "CLI · Antigravity": "agy",
}


def _provider(worker: Mapping) -> str | None:
    pid = worker.get("provider")
    if isinstance(pid, str) and pid in LABELS:
        return pid
    label = str(worker.get("label") or "")
    return next((known for prefix, known in _LABEL_PROVIDERS.items() if label.startswith(prefix)), None)


def display_label(worker: Mapping) -> str:
    """The label a stored worker shows now. A legacy Antigravity history keeps the neutral CLI label."""
    pid = _provider(worker)
    raw = str(worker.get("label") or "")
    if pid == "agy":
        marker = worker.get("transport")
        if marker == "acp-v1":
            return LABELS["agy"]
        if marker is not None:
            return LEGACY_LABEL
        if raw.startswith("ACP · "):
            return LABELS["agy"]
        if raw.startswith(("CLI · ", "ZMX · ")):
            return LEGACY_LABEL
        # A brand-new child has no transport marker yet; existing history without one is the legacy transport.
        if worker.get("status") in (None, "not_started") and not worker.get("session_id"):
            return LABELS["agy"]
        return LEGACY_LABEL
    if pid == "claude-code":
        return LABELS["claude-code"]
    return raw


def display_worker(worker: Mapping | None) -> dict | None:
    """A display copy of a frozen worker row: current labels, stored facts untouched."""
    if not isinstance(worker, Mapping):
        return worker
    out = dict(worker)
    label = display_label(out)
    if label:
        out["label"] = label
    disclosure = out.get("disclosure")
    if isinstance(disclosure, str):
        for old in _OLD:
            if disclosure.startswith(old):
                out["disclosure"] = label + disclosure[len(old):]
                break
    return out
