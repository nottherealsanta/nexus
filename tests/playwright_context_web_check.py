"""Real-browser parity for session extension controls through the daemon."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from test_agent_selection import _config
from test_context_extension_selection import skill, server

ROOT = Path(__file__).resolve().parents[1]


async def main():
    artifacts = ROOT / "artifacts/context-controls"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nx-context-web-", dir="/tmp") as directory:
        root = Path(directory)
        workspace, home = root / "project", root / "home"
        workspace.mkdir()
        skill(home / ".nexus", "global-skill")
        skill(workspace / ".agents", "project-skill")
        (home / ".nexus/mcp.json").write_text(json.dumps({"servers": {"global-server": server()}}))
        (workspace / ".agents/mcp.json").write_text(json.dumps({"servers": {"project-server": server()}}))
        provider = ScriptedProvider(text_response("Done."), text_response("Done."))
        runtime = Runtime(workspace, home=home, config=_config(), providers={"scripted": provider})
        daemon = Daemon(workspace, socket_path=root / "d.sock", runtime_factory=lambda *_args, **_kwargs: runtime)
        task = asyncio.create_task(daemon.serve_forever())
        try:
            while not daemon.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    for width in (1440, 900):
                        session = f"web-{width}"
                        daemon.facade.open_session(session)
                        page = await browser.new_page(viewport={"width": width, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(await daemon.web_launch())
                        await page.locator("#composer-input").wait_for()
                        await page.wait_for_timeout(1500)
                        await page.evaluate("""id => { history.pushState({session:id}, '', `/s/${id}`); dispatchEvent(new PopStateEvent('popstate')); }""", session)
                        header = page.locator("#timeline > .context-header")
                        try:
                            await header.get_by_text("Project 1 | Global 1", exact=False).first.wait_for(timeout=10000)
                        except Exception:
                            await page.screenshot(path=str(artifacts / "web-failure.png"))
                            print(await page.locator("body").inner_text(), errors)
                            raise
                        for category, names in (("Skills", ("global-skill", "project-skill")), ("MCP", ("global-server", "project-server"))):
                            await header.locator(".context-block").filter(has=page.get_by_text(category, exact=True)).click()
                            await page.locator("#text-overlay").wait_for(state="visible")
                            for name in names:
                                for before, after in (("On", "Off"), ("Off", "On"), ("On", "Off")):
                                    button = page.locator("#text-body button").filter(has_text=f"{before} · {name} ·")
                                    await button.click()
                                    await page.locator("#text-body button").filter(has_text=f"{after} · {name} ·").wait_for()
                            await page.keyboard.press("Escape")
                            await page.locator("#text-overlay").wait_for(state="hidden")
                        handle = runtime.session(session)
                        assert handle.disabled_extensions == {"skills": {"global-skill", "project-skill"}, "mcp": {"global-server", "project-server"}}
                        await page.locator("#composer-input").fill("hello")
                        await page.locator("#composer-input").press("Enter")
                        await page.get_by_text("Done.", exact=True).first.wait_for()
                        for category in ("Skills", "MCP"):
                            await header.locator(".context-block").filter(has=page.get_by_text(category, exact=True)).click()
                            await page.locator("#text-overlay").wait_for(state="visible")
                            assert await page.locator("#text-body button:disabled").count() == 2
                            assert "prompt cache" in await page.locator("#text-body").inner_text()
                            await page.screenshot(path=str(artifacts / f"web-{category.lower()}-locked-{width}.png"))
                            await page.keyboard.press("Escape")
                        assert not errors, errors
                        await page.close()
                        print(f"PASS web parity and first-turn lock at {width}px")
                finally:
                    await browser.close()
        finally:
            daemon.request_stop("context controls test complete")
            await asyncio.wait_for(task, 10)


if __name__ == "__main__":
    asyncio.run(main())
