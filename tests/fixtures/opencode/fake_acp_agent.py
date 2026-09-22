"""Offline fake ``opencode acp`` server for the OpenCode adapter tests.

Speaks newline-delimited JSON-RPC 2.0 on stdio and implements just enough of the
Agent Client Protocol to exercise the adapter: ``initialize``, ``session/new``,
``session/prompt``, ``session/close``, and an optional permission request.
Behaviour is chosen with ``--scenario``; there is no network and no model.

Optional flags:

* ``--trace PATH`` writes one JSON object at startup with ``argv``, ``cwd``,
  ``pid`` and the full child ``env`` (used to prove the credential allowlist).
* ``--prompt-trace PATH`` appends each ``session/prompt`` params object as a JSON
  line (used to inspect the rendered prompt).
* ``--method-trace PATH`` appends the name of each inbound request method as a
  JSON line (used to observe close/cancel behaviour).

Scenarios: ``text`` (default), ``thinking``, ``tools``, ``permission``, ``park``,
``crash``, ``malformed``, ``auth``, ``badversion``, ``close_error``,
``close_hang``, ``unknown_stop``, ``nocap``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

SESSION_ID = "sess_fake"

_METHOD_TRACE: str | None = None
_CONFORMANCE_PLAN: str | None = None


def _conformance(ident: object, plan_b64: str) -> None:
    """Replay a normalized event plan as ACP ``session/update`` chunks.

    The plan is a base64-encoded JSON object: ``{"chunks": [{"kind", "text"}],
    "stop_reason", "usage"}``. It lets the conformance harness drive arbitrary
    text/refusal/stop cases through the real adapter without a model.
    """
    try:
        decoded = json.loads(base64.b64decode(plan_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        decoded = {}
    chunks = decoded.get("chunks") if isinstance(decoded, dict) else None
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            kind = chunk.get("kind")
            text = chunk.get("text") or ""
            if kind == "text":
                send_chunk(text)
            elif kind == "thinking":
                send_update(
                    {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": text},
                    }
                )
            elif kind == "tool":
                send_update(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call_conf",
                        "title": str(chunk.get("name") or "tool"),
                        "status": "pending",
                    }
                )
    result: dict[str, Any] = {}
    if isinstance(decoded, dict):
        stop_reason = decoded.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            result["stopReason"] = stop_reason
        usage = decoded.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage
    if "stopReason" not in result:
        result["stopReason"] = "end_turn"
    send_result(ident, result)


def _trace_method(method: str) -> None:
    if not _METHOD_TRACE:
        return
    with open(_METHOD_TRACE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method}) + "\n")


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def send_result(ident: object, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": ident, "result": result})


def send_error(ident: object, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})


def send_update(update: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": update},
        }
    )


def send_chunk(text: str) -> None:
    send_update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


def read_message() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None


def _option(args: list[str], name: str, default: str | None = None) -> str | None:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def _write_trace(path: str, args: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "argv": args,
                "cwd": os.getcwd(),
                "pid": os.getpid(),
                "env": dict(os.environ),
            },
            handle,
        )


def _prompt(
    scenario: str,
    ident: object,
    params: dict[str, Any],
    prompt_trace: str | None,
) -> None:
    if prompt_trace:
        with open(prompt_trace, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(params) + "\n")
    if scenario == "conformance":
        _conformance(ident, _CONFORMANCE_PLAN or "")
        return
    if scenario == "crash":
        sys.stderr.write("boom key=sk-ant-CONFORMANCE-SECRET-0123456789abcdef\n")
        sys.stderr.flush()
        sys.exit(3)
    if scenario == "malformed":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        return
    if scenario == "permission":
        _permission(ident)
        return
    if scenario == "park":
        send_chunk("parked")
        _wait_for_cancel(ident)
        return
    if scenario == "thinking":
        send_update(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "pondering"},
            }
        )
    if scenario == "tools":
        send_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_1",
                "title": "Read",
                "kind": "read",
                "status": "pending",
            }
        )
        send_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_1",
                "status": "completed",
            }
        )
    send_chunk("hello ")
    send_chunk("world")
    send_update({"sessionUpdate": "usage_update", "used": 12, "size": 100})
    result: dict[str, Any] = {"stopReason": "end_turn"}
    if scenario == "unknown_stop":
        result["stopReason"] = "wat"
    if scenario in ("text", "thinking", "tools"):
        result["usage"] = {"inputTokens": 5, "outputTokens": 7}
    send_result(ident, result)


def _permission(ident: object) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": 9001,
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {"toolCallId": "call_1", "title": "Run"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    response = read_message()
    outcome: dict[str, Any] = {}
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, dict) and isinstance(result.get("outcome"), dict):
            outcome = result["outcome"]
    send_chunk(f"permission={json.dumps(outcome, sort_keys=True)}")
    send_result(ident, {"stopReason": "end_turn"})


def _wait_for_cancel(ident: object) -> None:
    while True:
        message = read_message()
        if message is None:
            return
        if message.get("method") == "session/cancel":
            _trace_method("session/cancel")
            send_result(ident, {"stopReason": "cancelled"})
            return


def main() -> int:
    global _METHOD_TRACE, _CONFORMANCE_PLAN
    args = sys.argv[1:]
    scenario = _option(args, "--scenario", "text") or "text"
    trace = _option(args, "--trace")
    prompt_trace = _option(args, "--prompt-trace")
    _METHOD_TRACE = _option(args, "--method-trace")
    _CONFORMANCE_PLAN = _option(args, "--conformance-plan")
    if trace:
        _write_trace(trace, args)
    while True:
        message = read_message()
        if message is None:
            return 0
        method = message.get("method")
        ident = message.get("id")
        if method:
            _trace_method(str(method))
        if method == "initialize":
            caps: dict[str, Any] = {}
            if scenario != "nocap":
                caps["sessionCapabilities"] = {"close": {}}
            send_result(
                ident,
                {
                    "protocolVersion": 2 if scenario == "badversion" else 1,
                    "agentCapabilities": caps,
                    "agentInfo": {"name": "fake-acp", "version": "0"},
                    "authMethods": [],
                },
            )
        elif method == "session/new":
            if scenario == "auth":
                send_error(ident, -32000, "Authentication required")
            else:
                send_result(ident, {"sessionId": SESSION_ID})
        elif method == "session/close":
            if scenario == "close_hang":
                pass
            elif scenario == "close_error":
                send_error(ident, -32603, "close failed")
            else:
                send_result(ident, {})
        elif method == "session/prompt":
            _prompt(scenario, ident, message.get("params") or {}, prompt_trace)


if __name__ == "__main__":
    sys.exit(main())
