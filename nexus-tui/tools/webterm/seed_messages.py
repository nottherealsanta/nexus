"""Add a fixed conversation to a session of an isolated Nexus home (no provider
calls). Run with the daemon stopped:
  NEXUS10_HOME=... uv run --project ../../../nexus-core python seed_messages.py <session id>
Only used by the visual harness (tools/webterm/seed.sh). The last turn is a
successful two-file `patch` edit: it gives the shots a split and a unified diff
(D09), with a CJK comment and a long line that wraps. It ends with an assistant
reply, so the turn is complete and the composer shows the context meter.

It also seeds what the sidebars need and the API alone cannot write: turn
records for each prompt (`turn`, `turn_request` with model and usage, attempts
and tool timings), which the timeline footers, the composer's context meter and
the sidebar totals read through `GET /api/session/{id}/turn`, and a child
session launched by the `session` tool from the seeded turn (the `2 ↳` count
and the child line). Rows are written straight to the store, as Core does
when it publishes them, so this must run while the daemon is stopped."""
import sys
import json
import time
from nexus.store import Store
from nexus.llmhub.types import Message, Text, Reasoning, ToolCall, ToolResult, Usage
st = Store()
sid = sys.argv[1]
ms = [
 Message("user", (Text("The loader re-reads the manifest on every call. Can you cache it and add a test?"),)),
 Message("assistant", (
   Reasoning("Check where the manifest is parsed first; a module-level cache is probably enough."),
   Text("I'll start by finding where the manifest is parsed."),
   ToolCall("call_1", "bash", json.dumps({"command": "rg -n 'parse_manifest' nexus-core/nexus"})),
   ToolCall("call_2", "read", json.dumps({"path": "nexus-core/nexus/loader.py"})),
 )),
 Message("tool", (
   ToolResult("call_1", "nexus-core/nexus/loader.py:41:def parse_manifest(path):\nnexus-core/nexus/loader.py:88:    data = parse_manifest(self.path)\nnexus-core/tests/test_loader.py:12:    assert parse_manifest(p)"),
   ToolResult("call_2", "\n".join(f"{i:>3}  line {i} of loader.py" for i in range(1, 25))),
 )),
 Message("assistant", (
   Text("`parse_manifest` is called from `Loader.load` on every request. I'll memoize it on the file's mtime so edits still show up, and add a regression test that counts parses."),
   ToolCall("call_3", "edit", json.dumps({"path": "nexus-core/nexus/loader.py", "old": "data = parse_manifest(self.path)", "new": "data = self._cached(self.path)"})),
 )),
 Message("tool", (ToolResult("call_3", "edit failed: old text not found", True),)),
 Message("assistant", (Text("The edit did not apply because the line changed. Re-reading the file before trying again."),)),
 Message("user", (Text("Go ahead, but keep the change small. 日本語のコメントも残してください。"),)),
 Message("assistant", (
   ToolCall("call_5", "session", json.dumps({"agent": "scout", "prompt": "List every call site of parse_manifest and say which ones run per request."})),
 )),
 Message("tool", (ToolResult("call_5", "Three call sites: Loader.load (per request), the CLI check in cli.py (once), and test_loader.py (test only)."),)),
 Message("assistant", (Text('A small example:\n\n```python\nprint("hello")\n```\n\n```sh\nprintf done\n```\n\n```mermaid\ngraph TD\n  A[Start] --> B[Finish]\n```'),)),
 Message("assistant", (
   ToolCall("call_4", "edit", json.dumps({"patch": '*** Begin Patch\n*** Update File: nexus-core/nexus/loader.py\n@@ def load(self, name: str) -> Manifest:\n     path = self.root / name\n-    return parse_manifest(path)\n+    key = path.stat().st_mtime_ns\n+    hit = self._cache.get(name)\n+    if hit and hit[0] == key:\n+        return hit[1]\n+    # キャッシュはファイルの更新時刻で無効になります。This line is deliberately long enough to wrap inside a split column of the diff.\n+    manifest = parse_manifest(path)\n+    self._cache[name] = (key, manifest)\n+    return manifest\n*** Update File: nexus-core/tests/test_loader.py\n@@ def test_load_returns_manifest(tmp_path):\n     assert loader.load("a")\n+\n+def test_cache_hit_skips_parse(tmp_path, monkeypatch):\n+    calls = []\n+    assert len(calls) == 1\n*** End Patch'})),
 )),
 Message("tool", (ToolResult("call_4", "updated nexus-core/nexus/loader.py (1 hunks)\nupdated nexus-core/tests/test_loader.py (1 hunks)"),)),
 # A closing reply: a conversation that ends on a tool message reads as cut
 # short, and the composer then shows `continue` instead of the context meter.
 Message("assistant", (Text("Done: the loader caches the manifest on its mtime, and the new test checks that a cache hit skips the parse."),)),
]
MODEL = "codex/gpt-6.1-terra"


def record(session_id, messages, usages):
    """Store `messages` as Core does while running them: one turn per prompt,
    one request per assistant message (with the next of `usages`), tool calls
    started and finished, every turn completed."""
    usages = iter(usages)
    turn, step = None, 0

    def close():
        st.mark_turn_ended(turn["id"], "end_turn", step)
        st.finish_turn(turn["id"], "completed", steps=step)
        st.set_turn_recovery(turn["id"], "none")

    for m in messages:
        if m.role == "user":
            if turn is not None:
                close()
            turn, step = st.start_turn(session_id), 0
            st.append_message(session_id, m)
        elif m.role == "assistant":
            step += 1
            request = st.start_request(turn["id"], session_id, step, model=MODEL, effort="medium")
            attempt = f"{request['id']}:1"
            st.start_attempt(turn["id"], session_id, request["id"], attempt, 1)
            st.mark_attempt_started(attempt)
            st.record_usage(request["id"], attempt, Usage(*next(usages)))
            calls = [p for p in m.parts if isinstance(p, ToolCall)]
            st.finish_attempt(attempt, "completed")
            st.finish_request(request["id"], stop="tool_calls" if calls else "end_turn", response_id=None,
                              parts=[], reasoning_seconds=1.4 if any(isinstance(p, Reasoning) for p in m.parts) else None)
            st.save_assistant_message(session_id, m, turn_id=turn["id"], request_id=request["id"])
            for call in calls:
                st.mark_tool_started(turn["id"], session_id, call)
        else:
            calls = {}
            for row in st.tools_for_turn(turn["id"]):
                calls[row["call_id"]] = ToolCall(row["call_id"], row["name"], row["arguments"])
            for result in m.parts:
                st.mark_tool_finished(turn["id"], session_id, calls[result.call_id], result)
            st.save_tool_message(session_id, m, turn_id=turn["id"])
    close()


parent = st.session(sid)
# input, cached input, output, reasoning per request; the last one is the
# context in use (48.2K).
record(sid, ms, [(12_400, 9_000, 410, 120), (18_900, 15_200, 520, 0), (21_300, 18_100, 160, 0),
                 (30_100, 24_000, 300, 80), (36_800, 30_500, 620, 0), (41_000, 33_900, 900, 0),
                 (48_200, 36_000, 2_150, 640)])

child = st.create_session(st.project(parent.project_id), title="Survey parse_manifest callers",
                          agent="scout", parent=parent, parent_call_id="call_5", launched_by="agent",
                          disabled_tools=sorted(parent.disabled()), snapshot=st.snapshot(sid))
record(child.id, (
 Message("user", (Text("List every call site of parse_manifest and say which ones run per request."),)),
 Message("assistant", (Text("Three call sites: `Loader.load` runs per request, the CLI check in `cli.py` runs once, and `test_loader.py` only in tests."),)),
), [(9_400, 6_100, 380, 0)])
print("seeded", len(ms), "messages; child", child.id)
