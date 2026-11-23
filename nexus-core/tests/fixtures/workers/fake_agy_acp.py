"""Test-only fake agy ACP v1 server for the Antigravity ACP worker tests.

Scenario and request log are files in the current workspace (`acp-scenario.txt`,
`acp-log.jsonl`). No network, credentials or real agy code is used.
"""
import copy
import json
import sys
from pathlib import Path

SCENARIO = Path("acp-scenario.txt").read_text(encoding="utf-8").strip() if Path("acp-scenario.txt").exists() else "text"
LOG = Path("acp-log.jsonl")
SESSION = "acp-1"
LIVE_SCENARIOS = ("live", "live-wrong")
LIVE = json.loads((Path(__file__).with_name("agy-1.5.14-acp") / "live.json").read_text(encoding="utf-8")) \
    if (Path(__file__).with_name("agy-1.5.14-acp") / "live.json").exists() else {}
ACTIVE_SESSION = LIVE.get("session", {}).get("sessionId", SESSION) if SCENARIO in LIVE_SCENARIOS else SESSION
RESUMED = False
MODES = {"currentModeId": "default",
         "availableModes": [{"id": "default", "name": "Default"}, {"id": "accept-edits", "name": "Accept edits"},
                            {"id": "auto", "name": "Auto"}, {"id": "plan", "name": "Plan"}]}


def log(value):
    with LOG.open("a", encoding="utf-8") as out:
        out.write(json.dumps(value) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def fail(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def update(session_update, **fields):
    notify("session/update", {"sessionId": ACTIVE_SESSION, "update": {"sessionUpdate": session_update, **fields}})


def read():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    value = json.loads(line)
    if isinstance(value, dict) and "method" in value:
        log({"method": value["method"], "params": value.get("params")})
    return value


def thought_level():
    return {"id": "thought_level", "name": "Thinking level", "category": "thought_level", "type": "select",
            "currentValue": "low", "options": [{"value": "low", "name": "Low"}, {"value": "medium", "name": "Medium"},
                                               {"value": "high", "name": "High"}]}


def model_option():
    return {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "gemini-test",
            "options": [{"value": "gemini-test", "name": "Test model"},
                        {"value": "gemini-other", "name": "Other model"}]}


def session_state():
    if SCENARIO in LIVE_SCENARIOS:
        return copy.deepcopy(LIVE["session"])
    modes = dict(MODES) if SCENARIO not in ("no-modes", "mode-missing") else None
    if SCENARIO == "mode-missing":
        modes = {"currentModeId": "accept-edits",
                 "availableModes": [{"id": "accept-edits", "name": "Accept edits"},
                                    {"id": "auto", "name": "Auto"}, {"id": "plan", "name": "Plan"}]}
    config = []
    if SCENARIO == "model-config":
        config.append(model_option())
    if SCENARIO == "effort":
        config.append(thought_level())
    return {"sessionId": SESSION, "modes": modes, "configOptions": config}


def prompt_result(message):
    if SCENARIO in LIVE_SCENARIOS:
        if RESUMED:
            update("agent_message_chunk", content={"type": "text", "text": LIVE["resume_text"]})
            reply(message["id"], {"stopReason": "end_turn", "usage": LIVE["resume_usage"]})
            return
        for item in LIVE["tool_updates"]:
            notify("session/update", {"sessionId": ACTIVE_SESSION, "update": item})
        send({"jsonrpc": "2.0", "id": "1", "method": "session/request_permission",
              "params": LIVE["permission"]})
        while True:
            incoming = read()
            if incoming is None:
                return
            if incoming.get("id") == "1":
                outcome = (incoming.get("result") or {}).get("outcome") or {}
                log({"permission": outcome})
                chosen = outcome.get("optionId") or outcome.get("outcome")
                failed = copy.deepcopy(LIVE["failed_update"])
                if chosen == "allow":
                    failed["status"] = "completed"
                notify("session/update", {"sessionId": ACTIVE_SESSION, "update": failed})
                reply(message["id"], LIVE["prompt_result"])
                return
    if SCENARIO == "long-text":
        update("agent_message_chunk", content={"type": "text", "text": "x" * 66_000})
        reply(message["id"], {"stopReason": "end_turn"})
        return
    if SCENARIO == "tool":
        update("tool_call", toolCallId="t1", title="Read a.txt", name="read_file", kind="read", status="pending",
               rawInput={"path": "a.txt"})
        update("tool_call_update", toolCallId="t1", status="in_progress")
        update("tool_call_update", toolCallId="t1", status="completed",
               content=[{"type": "content", "content": {"type": "text", "text": "contents"}}])
        update("agent_message_chunk", content={"type": "text", "text": "done"})
        reply(message["id"], {"stopReason": "end_turn"})
        return
    if SCENARIO == "permission-abandoned":
        send({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission", "params": {
            "sessionId": SESSION,
            "toolCall": {"toolCallId": "t1", "title": "Read a.txt", "name": "read_file", "kind": "read",
                         "rawInput": {"path": "a.txt"}},
            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}],
        }})
        update("agent_message_chunk", content={"type": "text", "text": "ended"})
        reply(message["id"], {"stopReason": "end_turn"})
        return
    if SCENARIO in ("permission", "permission-async", "permission-persistent", "permission-reject-only"):
        if SCENARIO in ("permission", "permission-async", "permission-reject-only"):
            update("tool_call", toolCallId="t1", title="Read a.txt", name="read_file", kind="read",
                   status="in_progress", rawInput={"path": "a.txt"})
        if SCENARIO == "permission-async":
            update("tool_call", toolCallId="t0", title="Working", name="working", kind="think", status="in_progress")
        if SCENARIO == "permission-persistent":
            options = [{"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                       {"optionId": "reject-always", "name": "Always reject", "kind": "reject_always"}]
        elif SCENARIO == "permission-reject-only":
            options = [{"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
        else:
            options = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                       {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
        send({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission", "params": {
            "sessionId": SESSION,
            "toolCall": {"toolCallId": "t1", "title": "Read a.txt", "name": "read_file", "kind": "read",
                         "rawInput": {"path": "a.txt"}},
            "options": options,
        }})
        while True:
            incoming = read()
            if incoming is None:
                return
            if incoming.get("id") == 100:
                outcome = (incoming.get("result") or {}).get("outcome") or {}
                log({"permission": outcome})
                chosen = outcome.get("optionId") or outcome.get("outcome")
                if SCENARIO == "permission-async":
                    update("tool_call_update", toolCallId="t0", status="completed")
                    update("tool_call_update", toolCallId="t1", status="completed")
                    update("agent_message_chunk", content={"type": "text", "text": "done"})
                elif SCENARIO in ("permission", "permission-reject-only"):
                    update("tool_call_update", toolCallId="t1",
                           status="completed" if chosen == "allow-once" else "failed")
                    update("agent_message_chunk", content={"type": "text", "text": f"permission:{chosen}"})
                else:
                    update("agent_message_chunk", content={"type": "text", "text": f"permission:{chosen}"})
                reply(message["id"], {"stopReason": "end_turn"})
                return
    if SCENARIO == "cancel":
        Path("acp-marker.txt").write_text("started", encoding="utf-8")
        while True:
            incoming = read()
            if incoming is None:
                return
            if incoming.get("method") == "session/cancel":
                Path("acp-marker.txt").write_text("cancelled", encoding="utf-8")
                reply(message["id"], {"stopReason": "cancelled"})
                return
    if SCENARIO == "unknown":
        update("future_update", value={"x": 1})
        update("agent_message_chunk", content={"type": "text", "text": "ok"})
        reply(message["id"], {"stopReason": "end_turn"})
        return
    if SCENARIO == "usage":
        update("agent_message_chunk", content={"type": "text", "text": "usage"})
        reply(message["id"], {"stopReason": "end_turn", "usage": {
            "inputTokens": 11, "outputTokens": 3, "cachedReadTokens": 5,
            "cachedWriteTokens": 0, "thoughtTokens": 2, "totalTokens": 21}})
        return
    text = "resumed" if SCENARIO == "resume" else "answer"
    update("agent_message_chunk", content={"type": "text", "text": text})
    reply(message["id"], {"stopReason": "end_turn"})


def serve():
    global RESUMED
    while True:
        message = read()
        if message is None:
            return
        method, request_id = message.get("method"), message.get("id")
        if method is None:
            continue
        params = message.get("params") or {}
        if method == "initialize":
            if SCENARIO == "init-auth":
                fail(request_id, -32000, "Authentication required")
            else:
                capabilities = {} if SCENARIO == "no-resume" else {"sessionCapabilities": {"resume": {}}}
                reply(request_id, {"protocolVersion": 1, "agentCapabilities": capabilities,
                                   "agentInfo": {"name": "fake-agy-acp", "version": "1.5.14"}})
        elif method == "session/new":
            if SCENARIO == "new-error":
                fail(request_id, -32000, "Authentication required")
            elif SCENARIO == "stderr-auth":
                sys.stderr.write("Authentication required\n")
                sys.stderr.flush()
                return
            else:
                reply(request_id, session_state())
        elif method == "session/resume":
            if SCENARIO == "resume-error":
                fail(request_id, -32002, "Resource not found")
            elif SCENARIO in LIVE_SCENARIOS:
                RESUMED = True
                reply(request_id, copy.deepcopy(LIVE["session"]))
            else:
                reply(request_id, {"modes": MODES, "configOptions": []})
        elif method == "session/load":
            if SCENARIO in LIVE_SCENARIOS:
                for item in LIVE["load_updates"]:
                    notify("session/update", {"sessionId": ACTIVE_SESSION, "update": item})
                reply(request_id, copy.deepcopy(LIVE["session"]))
            else:
                reply(request_id, {})
        elif method == "session/set_model":
            if SCENARIO == "model-error":
                fail(request_id, -32601, "Method not found")
            elif SCENARIO in LIVE_SCENARIOS:
                reply(request_id, {})
                model_id = params.get("modelId") if isinstance(params.get("modelId"), str) else ""
                thoughts = [option["value"] for option in
                            next(opt for opt in LIVE["session"]["configOptions"] if opt.get("id") == "thought_level")["options"]]
                model_values = [option["value"] for option in
                                next(opt for opt in LIVE["session"]["configOptions"] if opt.get("id") == "model")["options"]]
                suffix = next((value for value in thoughts if model_id.endswith(f"-{value}")), None)
                base = model_id[:-len(suffix) - 1] if suffix and model_id[:-len(suffix) - 1] in model_values else model_id
                update_config = copy.deepcopy(LIVE["config_update"])
                for option in update_config["configOptions"]:
                    if option.get("id") == "model":
                        option["currentValue"] = base
                    elif option.get("id") == "thought_level":
                        option["currentValue"] = suffix or "high"
                if SCENARIO == "live-wrong":
                    for option in update_config["configOptions"]:
                        if option.get("id") == "model":
                            option["currentValue"] = "wrong-model"
                notify("session/update", {"sessionId": ACTIVE_SESSION,
                                          "update": {"sessionUpdate": "config_option_update", **update_config}})
            else:
                reply(request_id, {"modelId": params.get("modelId")})
        elif method == "session/set_mode":
            if SCENARIO == "mode-wrong":
                reply(request_id, {"modes": {"currentModeId": "accept-edits",
                                              "availableModes": MODES["availableModes"]}})
            else:
                reply(request_id, {})
        elif method == "session/set_config_option":
            if params.get("configId") == "thought_level":
                option = thought_level()
                option["currentValue"] = params.get("value")
            elif params.get("configId") == "model":
                option = model_option()
                option["currentValue"] = params.get("value")
            else:
                fail(request_id, -32602, "unknown config option")
                continue
            reply(request_id, {"configOptions": [option]})
        elif method == "session/prompt":
            prompt_result(message)
        else:
            reply(request_id, {})


def main():
    if "--version" in sys.argv:
        print("1.5.14")
        return
    serve()


if __name__ == "__main__":
    main()
