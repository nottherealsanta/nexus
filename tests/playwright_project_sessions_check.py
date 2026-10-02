"""Offline two-project sidebar, navigation, and responsive visual checks."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ModelSection
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


async def main():
    artifacts = Path(__file__).resolve().parents[1] / "artifacts" / "project-sessions"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nxp-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        config = Config(model="scripted/m", version=2, v2=ConfigV2(
            model=ModelSection(default="scripted/m"), agent=AgentSection(profile="coding"),
        ))
        def factory(path, **kwargs):
            return Runtime(path, home=home, config=config, providers={"scripted": ScriptedProvider(text_response("hello"))})
        daemons = []
        try:
            for prefix in ("one", "two"):
                workspace = root / prefix / "demo"
                workspace.mkdir(parents=True)
                daemon = Daemon(workspace, home=home, runtime_factory=factory)
                await daemon.start()
                daemon.facade.open_session("same", create=True)
                await daemon.facade.start_turn("same", f"{prefix} project message")
                await daemon.facade.wait_idle(timeout=10)
                daemons.append(daemon)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page()
                await page.emulate_media(reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(await daemons[0].web_launch())
                await page.locator("#session-list .session-project").nth(1).wait_for()
                headings = await page.locator("#session-list .session-project").all_text_contents()
                assert headings == [str(root / "two/demo"), str(root / "one/demo")], headings
                assert await page.locator("#session-list .session-day").all_text_contents() == ["Today", "Today"]
                assert await page.locator("#session-list .session-row.active").count() == 1
                await page.locator("#session-filter").fill("/two/")
                assert await page.locator("#session-list .session-row").count() == 1
                await page.locator("#session-filter").fill("")
                for theme in ("dark", "light"):
                    await page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                    for width in (1440, 1024, 400):
                        await page.set_viewport_size({"width": width, "height": 900})
                        if width < 960:
                            await page.wait_for_function("matchMedia('(max-width:959px)').matches")
                            await page.keyboard.press("Control+b")
                            await page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().left >= 0")
                        await page.screenshot(path=str(artifacts / f"{theme}-{width}.png"))
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        if width < 960:
                            await page.keyboard.press("Escape")
                            await page.wait_for_function("!document.querySelector('#app').classList.contains('sidebar-open')")
                await page.set_viewport_size({"width": 1440, "height": 900})
                await daemons[1].web_launch()
                await page.locator("#session-list .session-item:not(:has(.session-delete)) .session-row").click()
                await page.wait_for_url(f"http://127.0.0.1:{daemons[1]._http.port}/s/same")
                await page.locator("#timeline").get_by_text("two project message", exact=True).wait_for()
                await page.reload()
                await page.locator("#session-list .session-project").nth(1).wait_for()
                assert await page.locator("#session-list .session-row.active").count() == 1
                assert not errors, errors
                await browser.close()
        finally:
            for daemon in reversed(daemons):
                await daemon.aclose()
    print("Project session browser checks passed")


if __name__ == "__main__":
    asyncio.run(main())
