"""Add a fixed conversation to a session of an isolated Nexus home (no provider
calls). Run with the daemon stopped:
  NEXUS10_HOME=... uv run --project ../../../nexus-core python seed_messages.py <session id>
Only used by the visual harness (tools/webterm/seed.sh). The last turn is a
successful two-file `patch` edit: it gives the shots a split and a unified diff
(D09), with a CJK comment and a long line that wraps.

It also seeds what the sidebars need and the API alone cannot write: a `usage`
event after the turn (the composer's context meter reads the latest one), and a
child session launched by the `session` tool from the seeded turn (the `2 ↳`
count and the child line). Rows are written straight to the store, as Core does
when it publishes them, so this must run while the daemon is stopped."""
import sys
import json
import time
from nexus.store import Store
from nexus.llmhub.types import Message, Text, Reasoning, ToolCall, ToolResult
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
 Message("tool", (ToolResult("call_4", "updated nexus-core/nexus/loader.py (1 hunks)\nupdated nexus-core/tests/test_loader.py (1 hunks)"),))
]
for m in ms:
    st.append_message(sid, m)

def usage(session_id, input_tokens, cached, output, reasoning):
    st.append_event({"session_id": session_id, "seq": 1, "time": time.time(), "type": "usage",
                     "data": {"input_tokens": input_tokens, "cached_input_tokens": cached,
                              "output_tokens": output, "reasoning_tokens": reasoning}})

parent = st.session(sid)
usage(sid, 48_200, 36_000, 2_150, 640)

child = st.create_session(st.project(parent.project_id), title="Survey parse_manifest callers",
                          agent="scout", parent=parent, parent_call_id="call_5", launched_by="agent",
                          disabled_tools=sorted(parent.disabled()), snapshot=st.snapshot(sid))
for m in (
 Message("user", (Text("List every call site of parse_manifest and say which ones run per request."),)),
 Message("assistant", (Text("Three call sites: `Loader.load` runs per request, the CLI check in `cli.py` runs once, and `test_loader.py` only in tests."),)),
):
    st.append_message(child.id, m)
usage(child.id, 9_400, 6_100, 380, 0)
print("seeded", len(ms), "messages; child", child.id)
