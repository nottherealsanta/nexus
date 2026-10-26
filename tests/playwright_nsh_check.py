"""Deterministic real-WASM browser journey through the authenticated host routes."""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nexus.host.transports.http_sse import HTTPSSEServer
from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from playwright.async_api import async_playwright, expect


class Facade:
    def __init__(self):
        self.commands = []
        self.sessions = {}
        self.changed = asyncio.Event()
        self.seq = 0
        self.streams = 0

    async def handle(self, command):
        self.commands.append(command)
        if isinstance(command, p.SessionList):
            return p.SessionListResult(sessions=list(self.sessions.values()))
        if isinstance(command, p.SessionOpen):
            if command.session == "reject":
                return p.ErrorResult(message="Open rejected")
            assert command.create and command.recover
            self.sessions[command.session] = SessionSummary(id=command.session)
            return p.SessionOpenResult(session=self.sessions[command.session])
        if isinstance(command, p.SessionEnqueue):
            assert command.session in self.sessions
            assert command.mode == "steer"
            if command.content == "fail":
                return p.ErrorResult(message="Send rejected")
            self.seq += 1
            self.changed.set()
            return p.SessionEnqueueResult(session=command.session, queued_id="q1")
        if isinstance(command, p.SessionCancel):
            assert not command.drop_queue
            self.seq += 1
            self.changed.set()
            return p.SessionCancelResult(session=command.session, cancelled=True)
        raise AssertionError(command)

    def web_snapshot(self, session):
        assert session in self.sessions
        return {
            "schema_version": 1,
            "session": session,
            "seq": self.seq,
            "view": {
                "phase": "idle" if self.seq == 2 else "running",
                "messages": [
                    {"role": "assistant", "text": f"Live revision {self.seq}"}
                ],
                "tools": [
                    {
                        "name": "shell",
                        "input": {"command": "pwd"},
                        "result": [{"text": "/safe/workspace"}],
                        "error": None,
                    }
                ],
            },
        }

    async def subscribe_web(self, session, from_seq):
        self.streams += 1
        while True:
            await self.changed.wait()
            self.changed.clear()
            yield {
                "schema_version": 1,
                "session": session,
                "seq": self.seq,
                "ops": [],
                "resync": True,
            }


async def main():
    facade = Facade()
    server = await HTTPSSEServer(facade, web_workspace="/test").start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(
                f"http://127.0.0.1:{server.port}/#ticket={server._web.issue_ticket()}"
            )
            await expect(
                page.get_by_text(
                    "Enter a session ID to open, or a new ID to create.", exact=True
                )
            ).to_be_visible()
            assert "ticket=" not in page.url
            session = page.get_by_placeholder("Session ID (new ID creates a session)")
            await session.fill("integration-001")
            await page.get_by_text("Open / create", exact=True).click()
            await expect(
                page.get_by_text("Active session: integration-001", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_text("Live revision 0", exact=False)
            ).to_be_visible()
            for _ in range(100):
                if facade.streams:
                    break
                await asyncio.sleep(0.02)
            assert facade.streams == 1
            # Editing an uncommitted target must never retarget send/stop.
            await session.fill("other-session")
            message = page.get_by_placeholder("Message", exact=True)
            await message.fill("hello")
            await page.get_by_text("Send", exact=True).click()
            await expect(
                page.get_by_text("Live revision 1", exact=False)
            ).to_be_visible()
            enqueue = next(
                c for c in facade.commands if isinstance(c, p.SessionEnqueue)
            )
            assert enqueue.session == "integration-001" and enqueue.content == "hello"
            await expect(page.get_by_text("command: pwd", exact=False)).to_be_visible()
            await expect(
                page.get_by_text("/safe/workspace", exact=False)
            ).to_be_visible()
            await page.get_by_text("Stop", exact=True).click()
            await expect(
                page.get_by_text("Live revision 2", exact=False)
            ).to_be_visible()
            await message.fill("fail")
            await page.get_by_text("Send", exact=True).click()
            await expect(page.get_by_text("Send rejected", exact=True)).to_be_visible()
            await expect(message).to_have_value("fail")
            await session.fill("reject")
            await page.get_by_text("Open / create", exact=True).click()
            await expect(page.get_by_text("Open rejected", exact=True)).to_be_visible()
            await expect(
                page.get_by_text("Active session: integration-001", exact=True)
            ).to_be_visible()
            assert not errors, errors
            await browser.close()
            print(
                "PASS: NSH create/open, send, SSE refresh, tools, stop, active-session isolation, HTTP-200 errors"
            )
    finally:
        await server.aclose()


if __name__ == "__main__":
    asyncio.run(main())
