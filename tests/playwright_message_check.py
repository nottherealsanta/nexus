"""Offline browser check for queue/steer/interrupt (plan section 4)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def check_mode(browser, mode, key):
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), TextDelta(text="Working now"),
         Wait(gate), MessageStop(stop_reason="end_turn")],
        text_response("Direction followed"), text_response("Later completed"),
    )
    config = Config(model="scripted/m", version=2,
                    v2=ConfigV2(model=ModelSection(default="scripted/m")))
    with tempfile.TemporaryDirectory(prefix="nxmsg-", dir="/tmp") as tmp:
        path = Path(tmp)
        daemon = Daemon(path, socket_path=path / "d.sock",
                        runtime_factory=lambda workspace, **kw: Runtime(
                            workspace, config=config, providers={"scripted": provider}))
        task = asyncio.create_task(daemon.serve_forever())
        page = await browser.new_page(viewport={"width": 1000, "height": 800})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await wait_for(lambda: daemon.started or task.done())
            facade = daemon.facade
            facade.open_session("messages")
            await facade.start_turn("messages", "original")
            await wait_for(lambda: provider.calls == 1)
            await page.goto(await daemon.web_launch())
            await page.get_by_text("Working now", exact=False).wait_for()
            editor = page.locator("#composer-input")
            assert await editor.is_enabled()
            await editor.fill("later")
            await editor.press("Enter")
            await page.locator(".queued").filter(has_text="later").wait_for()
            assert await editor.input_value() == ""
            await page.reload()
            await page.locator(".queued").filter(has_text="later").wait_for()
            await editor.fill("direction")
            await editor.press(key)
            await wait_for(lambda: any(e.type == "input.queued" and e.data.get("mode") == mode
                                      for e in facade.runtime.session("messages").events))
            if mode != "interrupt":
                assert provider.calls == 1
                await page.locator(".queued").filter(has_text="direction").wait_for()
                gate.set()
            await facade.wait_idle(timeout=5)
            await page.locator("#timeline").get_by_text("Later completed", exact=False).wait_for()
            events = facade.runtime.session("messages").events
            assert sum(e.type == "turn.started" for e in events) == (2 if mode == "steer" else 3)
            assert sum(e.type == "turn.cancelled" for e in events) == (1 if mode == "interrupt" else 0)
            assert not any(e.type == "turn.failed" for e in events)
            assert not errors, errors
            print(f"{mode}: passed")
        finally:
            await page.close()
            daemon.request_stop("message check complete")
            await task


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for mode, key in [("queue", "Enter"), ("steer", "Control+Enter"),
                              ("interrupt", "Alt+Enter")]:
                await check_mode(browser, mode, key)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
