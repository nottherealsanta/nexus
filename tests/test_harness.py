import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import tempfile
import unittest

from nexus import Agent, Config, Event, SessionBusy
from nexus.context import Exchange, build_context
from nexus.store import SessionStore


class FakeProvider:
    def __init__(self):
        self.prompts = []
        self.configs = []
        self.fail = False
        self.closed = False

    async def stream(self, prompt, *, workspace, config):
        self.prompts.append(json.loads(prompt))
        self.configs.append(config)
        try:
            yield Event("message", {"text": "answer"})
            if self.fail:
                raise RuntimeError("failed")
        finally:
            self.closed = True


class ContextTests(unittest.TestCase):
    def test_retains_contiguous_complete_suffix_and_pins_input(self):
        history = [Exchange("old" * 300, "old answer"), Exchange("recent", "reply")]
        context = build_context("rules", "notes", history, "new", 250)
        data = json.loads(context.prompt)
        self.assertEqual(data["history"], [{"user": "recent", "assistant": "reply"}])
        self.assertEqual(context.omitted_exchanges, 1)
        self.assertEqual(data["instructions"], "rules")
        self.assertEqual(data["memory"], "notes")
        self.assertEqual(data["user"], "new")
        self.assertLessEqual(len(context.prompt), 250)

    def test_pinned_overflow_and_empty_input_are_errors(self):
        with self.assertRaises(ValueError):
            build_context("x" * 200, "", [], "new", 100)
        with self.assertRaises(ValueError):
            build_context("", "", [], " ", 1000)

    def test_escaped_content_counts_against_budget(self):
        history = [Exchange('"' * 100, "\\" * 100)]
        context = build_context("", "", history, "👋", 200)
        self.assertLessEqual(len(context.prompt), 200)
        self.assertEqual(context.omitted_exchanges, 1)

    def test_config_validation(self):
        for kwargs in ({"context_chars": True}, {"timeout_seconds": float("nan")},
                       {"sandbox": "danger-full-access"}, {"model": 123}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Isolate Config.load from the developer's real ~/.nexus.
        self._previous_home = os.environ.get("HOME")
        os.environ["HOME"] = self.temp.name
        self.addCleanup(self._restore_home)
        self.provider = FakeProvider()
        self.agent = Agent(self.root, provider=self.provider)

    def _restore_home(self):
        if self._previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._previous_home

    async def test_persistence_and_hot_reload(self):
        self.assertEqual(await self.agent.run("first"), "answer")
        (self.root / "nexus.toml").write_text('model = "custom"\n')
        (self.root / "MEMORY.md").write_text("remember this")
        another = Agent(self.root, provider=self.provider)
        await another.run("second")
        self.assertEqual(self.provider.prompts[-1]["history"], [{"user": "first", "assistant": "answer"}])
        self.assertEqual(self.provider.prompts[-1]["memory"], "remember this")
        self.assertEqual(self.provider.configs[-1].model, "custom")

    async def test_failure_does_not_commit_and_releases_lock(self):
        self.provider.fail = True
        with self.assertRaises(RuntimeError):
            await self.agent.run("bad")
        self.assertEqual(self.agent.store.load("default"), [])
        self.provider.fail = False
        await self.agent.run("good")
        self.assertEqual(len(self.agent.store.load("default")), 1)

    async def test_early_close_closes_provider_and_releases_lock(self):
        async with aclosing(self.agent.stream("stop")) as stream:
            await anext(stream)
            await anext(stream)
        self.assertTrue(self.provider.closed)
        self.assertEqual(self.agent.store.load("default"), [])
        await self.agent.run("next")

    async def test_session_isolation_and_busy_detection(self):
        async with aclosing(self.agent.stream("one")) as stream:
            await anext(stream)
            with self.assertRaises(SessionBusy):
                await self.agent.run("two")
            await self.agent.run("parallel", session="other")
        self.assertEqual(len(self.agent.store.load("other")), 1)
        self.assertEqual(self.agent.store.load("default"), [])

    async def test_invalid_settings_fail_before_provider(self):
        (self.root / "nexus.toml").write_text("typo = 1")
        with self.assertRaisesRegex(ValueError, "Unknown"):
            await self.agent.run("hello")
        self.assertEqual(self.provider.prompts, [])

    async def test_context_path_cannot_escape(self):
        (self.root / "nexus.toml").write_text('memory_file = "../outside.md"')
        with self.assertRaisesRegex(ValueError, "inside workspace"):
            await self.agent.run("hello")

    async def test_cancellation_releases_session(self):
        entered = asyncio.Event()
        class WaitingProvider:
            async def stream(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
                yield Event("message", {"text": "unreachable"})
        task = asyncio.create_task(Agent(self.root, provider=WaitingProvider()).run("wait"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.agent.run("recovered")

    def test_session_ids_and_corrupt_history(self):
        with self.assertRaises(ValueError):
            self.agent.store.load("../escape")
        with self.agent.store.lock("broken"):
            (self.agent.store.directory / "broken.json").write_text('{"version": 9}')
            with self.assertRaises(ValueError):
                self.agent.store.load("broken")
