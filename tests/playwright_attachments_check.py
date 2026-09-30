"""Real browser and daemon: upload, clipboard, drop, preview, submit, reconnect.

Run with .venv/bin/python tests/playwright_attachments_check.py.
"""

import asyncio
import base64
import os
import json
import tempfile
from pathlib import Path

from attachment_fixtures import PNG, pdf_bytes
from playwright.async_api import async_playwright

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.host.daemon import Daemon
from nexus.model.capabilities import Capabilities
from nexus.model.message import Image, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


async def main():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="nxa-") as temporary:
        root = Path(temporary)
        os.environ["NEXUS_HOME"] = str(root / "home")
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        workspace = root / "workspace"
        workspace.mkdir()
        provider = ScriptedProvider(
            *[text_response(f"Attachment received {i}") for i in range(5)],
            capabilities=Capabilities(
                vision=True,
                streaming=True,
                max_context_tokens=2000000,
                max_output_tokens=8192,
            ),
        )
        config = Config(
            model="scripted/m",
            version=2,
            v2=ConfigV2(model=ModelSection(default="scripted/m")),
        )
        daemon = Daemon(
            workspace,
            socket_path=root / "d.sock",
            runtime_factory=lambda path, **kwargs: Runtime(
                path, config=config, providers={"scripted": provider}
            ),
        )
        task = asyncio.create_task(daemon.serve_forever())
        try:
            while not daemon.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
            daemon.facade.open_session("files")
            launch = await daemon.web_launch()
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.set_default_timeout(8000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def skip_setup(route):
                    if route.request.post_data_json.get("type") == "SetupStatus":
                        await route.fulfill(
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "type": "SetupStatusResult",
                                    "required": False,
                                    "effective_model": "scripted/m",
                                }
                            ),
                        )
                    else:
                        await route.continue_()

                await page.route("**/v1/web/command", skip_setup)
                await page.goto(launch)
                await page.locator("#composer-input").wait_for()
                await page.wait_for_function(
                    "document.querySelector('#composer-input').disabled===false"
                )
                await page.locator("#attachment-picker").set_input_files(
                    [
                        {"name": "photo.png", "mimeType": "image/png", "buffer": PNG},
                        {
                            "name": "report.pdf",
                            "mimeType": "application/pdf",
                            "buffer": pdf_bytes(),
                        },
                        {
                            "name": "large.md",
                            "mimeType": "text/markdown",
                            "buffer": b"# First line\n"
                            + b"body\n" * 3000
                            + b"Last line",
                        },
                    ]
                )
                await page.locator("#file-attachments details").nth(2).wait_for()
                assert (
                    await page.locator("#file-attachments").inner_text()
                    == "photo.png · image\nreport.pdf · markdown\nlarge.md · markdown"
                )
                await (
                    page.locator("#file-attachments details")
                    .nth(1)
                    .locator("summary")
                    .click()
                )
                assert (
                    "Attachment PDF works"
                    in await page.locator("#file-attachments pre").first.inner_text()
                )
                await (
                    page.locator("#file-attachments details")
                    .nth(2)
                    .locator("summary")
                    .click()
                )
                assert (
                    await page.locator("#file-attachments pre").nth(1).inner_text()
                ).endswith("Last line")
                await page.locator("#composer-input").fill("Inspect attachments")
                await page.locator("#composer-input").press("Enter")
                await (
                    page.locator("#timeline")
                    .get_by_text("Attachment received 0", exact=False)
                    .wait_for()
                )
                assert not await page.locator("#file-attachments details").count()
                await page.locator(".message-images img").wait_for()
                assert await page.locator(".message-images img").evaluate(
                    "(img)=>img.complete&&img.naturalWidth===1"
                )
                assert "Last line" in await page.locator("#timeline").inner_text()
                await page.reload()
                await page.locator(".message-images img").wait_for()
                await (
                    page.locator("#timeline")
                    .get_by_text("Attachment PDF works", exact=False)
                    .first.wait_for()
                )
                blocks = provider.requests[0].messages[-1].content
                assert any(isinstance(b, Image) and b.data == PNG for b in blocks)
                assert any(
                    isinstance(b, Text) and "Attachment PDF works" in b.text
                    for b in blocks
                )
                # Browser clipboard images and drag/drop both use the real route.
                for event_name in ("paste", "drop"):
                    await page.evaluate(
                        """({data,eventName})=>{const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0));const transfer=new DataTransfer();transfer.items.add(new File([bytes],eventName+'.png',{type:'image/png'}));const target=document.querySelector(eventName==='paste'?'#composer-input':'#composer-form');target.dispatchEvent(eventName==='paste'?new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true}):new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true}));}""",
                        {
                            "data": base64.b64encode(PNG).decode(),
                            "eventName": event_name,
                        },
                    )
                    await page.locator("#file-attachments summary").wait_for()
                    await page.locator("#file-attachments summary").click()
                    await page.locator("#file-attachments button").click()
                    assert not await page.locator("#file-attachments details").count()
                # A file larger than the normal 1 MiB command limit is accepted only
                # on the authenticated attachment route.
                await page.locator("#attachment-picker").set_input_files(
                    {
                        "name": "big.txt",
                        "mimeType": "text/plain",
                        "buffer": b"x" * (1024 * 1024 + 20),
                    }
                )
                await page.locator("#file-attachments summary").wait_for()
                await page.wait_for_function(
                    "document.querySelector('#attach-file').disabled===false"
                )
                await page.locator("#composer-input").press("Enter")
                await (
                    page.locator("#timeline")
                    .get_by_text("Attachment received 1", exact=False)
                    .wait_for()
                )
                await page.locator(".attachment-text summary").click()
                await page.wait_for_function(
                    "length=>document.querySelector('.attachment-full-text').value.endsWith('x'.repeat(length))",
                    arg=1024 * 1024 + 20,
                )
                await page.locator("#attachment-picker").set_input_files(
                    {
                        "name": "unsupported.bin",
                        "mimeType": "application/octet-stream",
                        "buffer": b"\x00\xffbinary",
                    }
                )
                await page.get_by_text(
                    "Could not attach unsupported.bin:", exact=False
                ).wait_for()
                assert not await page.locator("#file-attachments details").count()
                # Large upload route never permits arbitrary host commands.
                status = await page.evaluate(
                    """async()=>{const api=await import('/js/api.js');return (await fetch('/v1/web/attachment',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':api.getCsrf()},body:JSON.stringify({type:'Shutdown'})})).status;}"""
                )
                assert status == 400
                host = launch.split("://", 1)[1].split("/", 1)[0]
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", int(host.split(":")[1])
                )
                writer.write(
                    f"POST /v1/web/attachment HTTP/1.1\r\nHost: {host}\r\nContent-Length: 1048577\r\n\r\n".encode()
                )
                await writer.drain()
                assert b"401" in await asyncio.wait_for(reader.readline(), 2)
                writer.close()
                await writer.wait_closed()
                for width in (1440, 400):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth<=innerWidth"
                    )
                assert not errors, errors
                await browser.close()
                print(
                    "Browser attachment checks passed: images, PDF, long text, paste, drop, removal, image-only submit, reconnect, upload auth, limits, and responsive layout."
                )
        finally:
            if "browser" in locals():
                await browser.close()
            daemon.request_stop("attachment test complete")
            await asyncio.wait_for(task, 10)


if __name__ == "__main__":
    asyncio.run(main())
