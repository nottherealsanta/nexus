"""Test-only ACP v1 stdio agent for `nexus.workers.acp`; never used at runtime.

Usage: fake_acp_agent.py <scenario> [marker]

Every message is one NDJSON line on stdin/stdout. Scenarios deliberately exercise
interleaving, errors, malformed output, cancellation and death.
"""
import json
import os
import signal
import sys
import time

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "echo"
MARKER = sys.argv[2] if len(sys.argv) > 2 else ""


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def fail(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    send({"jsonrpc": "2.0", "id": request_id, "error": error})


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def read():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line)


def initialize_result():
    return {"protocolVersion": 1, "agentInfo": {"name": "fake-acp", "version": "1"}}


def permission_choice(response):
    outcome = (response.get("result") or {}).get("outcome") or {}
    return outcome.get("optionId") if outcome.get("outcome") == "selected" else "cancelled"


def update(text):
    notify("session/update", {"sessionId": "s1",
                              "update": {"sessionUpdate": "agent_message_chunk",
                                         "content": {"type": "text", "text": text}}})


def prompt_with_permission(message):
    update("first")
    send({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission", "params": {
        "sessionId": "s1",
        "toolCall": {"toolCallId": "t1", "title": "Run the tests", "kind": "execute"},
        "options": [
            {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
        ],
    }})
    handled = []
    choice = "cancelled"
    while True:
        incoming = read()
        if incoming is None:
            return
        if "method" in incoming and "id" in incoming:
            handled.append(incoming["method"])
            reply(incoming["id"], {"method": incoming["method"]})
        elif incoming.get("id") == 100:
            choice = permission_choice(incoming)
            break
    update(f"permission:{choice}")
    update("second")
    reply(message["id"], {"stopReason": "end_turn", "handled": handled})


def unknown_request(message):
    send({"jsonrpc": "2.0", "id": 77, "method": "vendor/unknown", "params": {}})
    error = None
    while True:
        incoming = read()
        if incoming is None:
            return
        if incoming.get("id") == 77:
            error = incoming.get("error")
            break
    reply(message["id"], {"stopReason": "end_turn", "unknown": error})


def slow_request(message):
    with open(MARKER, "w", encoding="utf-8") as handle:
        handle.write("started")
    while True:
        incoming = read()
        if incoming is None:
            return
        if (incoming.get("method") == "$/cancel_request"
                and (incoming.get("params") or {}).get("requestId") == message["id"]):
            with open(MARKER, "w", encoding="utf-8") as handle:
                handle.write("cancelled")
            fail(message["id"], -32800, "Request cancelled")
            return


def main():
    out_of_order_first = None
    while True:
        message = read()
        if message is None:
            return
        method, request_id = message.get("method"), message.get("id")
        if method is None:
            if SCENARIO == "permission-late" and request_id == 200:
                update(f"permission:{permission_choice(message)}")
            continue
        if method == "initialize":
            if SCENARIO == "linger":
                with open(MARKER, "w", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if SCENARIO == "wrong-version":
                reply(request_id, {"protocolVersion": 2})
            else:
                reply(request_id, initialize_result())
            if SCENARIO == "linger":
                time.sleep(60)
            continue
        if SCENARIO == "echo":
            reply(request_id, {"echo": message.get("params")})
        elif SCENARIO == "out-of-order":
            if out_of_order_first is None:
                out_of_order_first = message
            else:
                reply(request_id, {"which": "second"})
                reply(out_of_order_first["id"], {"which": "first"})
                out_of_order_first = None
        elif SCENARIO == "interleaved" and method == "session/prompt":
            prompt_with_permission(message)
        elif SCENARIO == "permission-late" and method == "session/prompt":
            update("before")
            send({"jsonrpc": "2.0", "id": 200, "method": "session/request_permission", "params": {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "t1", "title": "Wait for approval", "kind": "execute"},
                "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}],
            }})
            update("during")
            reply(request_id, {"stopReason": "end_turn", "permissionPending": True})
        elif SCENARIO == "error":
            if method == "boom":
                fail(request_id, -32602, "invalid params for api_key=SECRET", {"field": "x"})
            else:
                reply(request_id, {"echo": message.get("params")})
        elif SCENARIO == "malformed":
            sys.stdout.write("not json\n")
            sys.stdout.flush()
            return
        elif SCENARIO == "oversize":
            sys.stdout.write("x" * 1000)
            sys.stdout.flush()
            time.sleep(60)
        elif SCENARIO == "death":
            sys.stderr.write("api_key=SECRET\n")
            sys.stderr.flush()
            os._exit(7)
        elif SCENARIO == "slow":
            slow_request(message)
        elif SCENARIO == "unknown" and method == "session/prompt":
            unknown_request(message)
        elif SCENARIO == "hold":
            continue
        else:
            reply(request_id, {"unhandled": method})


if __name__ == "__main__":
    main()
