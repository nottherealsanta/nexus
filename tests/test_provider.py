"""Exercise the real subprocess transport with an offline fake Codex executable."""
import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from nexus import CodexProvider, Config, ProviderError


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "codex"

    def script(self, body):
        self.exe.write_text(f"#!{sys.executable}\n" + body)
        self.exe.chmod(0o755)

    async def collect(self, timeout=3):
        return [event async for event in CodexProvider().stream(
            "hello", workspace=self.root,
            config=Config(executable=str(self.exe), timeout_seconds=timeout))]

    async def test_real_pipe_transport_and_arguments(self):
        self.script('''import sys, json
assert sys.stdin.read() == "hello"
assert "--ephemeral" in sys.argv and "workspace-write" in sys.argv
assert 'approval_policy="never"' in sys.argv
sys.stderr.write("diagnostic" * 30000)
print(json.dumps({"type":"thread.started", "thread_id":"test"}))
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"ok"}}))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":3}}))
''')
        events = await self.collect()
        self.assertEqual([e.type for e in events], ["provider", "message", "provider"])
        self.assertEqual(events[1].data["text"], "ok")
        self.assertEqual(events[2].data["usage"]["input_tokens"], 3)

    async def test_missing_completion_and_nonzero_exit(self):
        for body, expected in [('print(\'{"type":"thread.started"}\')', "without turn.completed"),
                               ('import sys; sys.stderr.write("bad auth"); sys.exit(7)', "bad auth")]:
            self.script(body)
            with self.assertRaisesRegex(ProviderError, expected):
                await self.collect()

    async def test_malformed_and_failed_events(self):
        for output in ("not json", "[]", '{"type":"turn.failed","error":{"message":"bad"}}',
                       '{"type":"item.completed","item":{"type":"agent_message"}}'):
            self.script(f"print({output!r})")
            with self.assertRaises(ProviderError):
                await self.collect()

    async def test_timeout_kills_process(self):
        self.script(f'import os, time\nopen({str(self.root / "pid")!r}, "w").write(str(os.getpid()))\ntime.sleep(30)')
        with self.assertRaisesRegex(ProviderError, "exceeded"):
            await self.collect(timeout=1)
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_closing_stream_kills_process(self):
        self.script('import os, json, time\nprint(json.dumps({"type":"pid", "pid":os.getpid()}), flush=True)\ntime.sleep(30)')
        async with aclosing(CodexProvider().stream("hello", workspace=self.root, config=Config(executable=str(self.exe)))) as stream:
            event = await anext(stream)
            pid = event.data["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
