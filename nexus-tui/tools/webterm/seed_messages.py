"""Add a fixed conversation to a session of an isolated Nexus home (no provider
calls). Run with the daemon stopped:
  NEXUS10_HOME=... uv run --project ../../../nexus-core python seed_messages.py <session id>
Only used by the visual harness (tools/webterm/seed.sh)."""
import sys
import json
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
 Message("assistant", (Text('A small example:\n\n```python\nprint("hello")\n```\n\n```sh\nprintf done\n```\n\n```mermaid\ngraph TD\n  A[Start] --> B[Finish]\n```'),)),
]
for m in ms:
    st.append_message(sid, m)
print("seeded", len(ms))
