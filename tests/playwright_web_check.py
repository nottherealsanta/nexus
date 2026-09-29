"""Real-browser handoff check for the framework-free Nexus web client.

Run from the repository root with ``.venv/bin/python tests/playwright_web_check.py``.
It starts an offline scripted daemon, opens its one-use browser URL, switches
between a UDS terminal peer and Chromium, reloads during a streamed turn, and
writes light/dark/mobile screenshots under ignored ``artifacts/web-e2e``.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import protocol as p
from nexus.host.daemon import Daemon
from nexus.host.transports.uds import UDSClient
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime
from nexus.session.store import EventRecord, MessageRecord

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "web-e2e"


async def _wait_for(predicate, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("daemon did not become ready")


def _web_fixture(session: str) -> dict[str, object]:
    """Stable, projection-shaped data used by the browser's presentation tests."""
    def message(mid: str, seq: int, role: str, text: str, *, done: bool = True) -> dict[str, object]:
        return {"id": mid, "event_seq": seq, "role": role, "done": done,
                "model": "scripted/m", "blocks": [{"kind": "text", "text": text}]}

    def tool(cid: str, seq: int, name: str, status: str = "completed", **extra: object) -> dict[str, object]:
        return {"call_id": cid, "event_seq": seq, "name": name, "status": status,
                "input": extra.pop("input", {"path": f"src/{cid}.py", "query": "needle"}),
                "result": extra.pop("result", [{"type": "text", "text": f"Result for {cid}"}]), **extra}

    tools = [
        tool("read-1", 3, "Read", display="2 lines"),
        tool("read-2", 4, "Read", display="4 lines"),
        tool("grep-1", 5, "Grep", display="3 matches"),
        tool("bash-1", 6, "Bash", display="tests passed", input={"command": "pytest -q"}),
        tool("edit-1", 7, "Edit", display="1 replacement", input={"path": "src/a.py", "old_string": "old", "new_string": "new"}, diff={"path": "src/a.py", "added_lines": 1, "removed_lines": 1, "hunk": "@@ -1 +1 @@\n-old\n+new", "truncated": False}),
        tool("task-1", 8, "Task", display="Child agent completed", child_agent_ids=["child-1"], input={"description": "Summarize the related implementation briefly"}),
        tool("read-3", 9, "Read", display="5 lines"),
        tool("read-4", 10, "Read", display="6 lines"),
        tool("running-1", 13, "Read", "running", display="Still working"),
        tool("failed-1", 14, "Bash", "failed", error="Command failed safely", input={"command": "false"}),
    ]
    if session.endswith("task-done"):
        tools = tools[:6]
    if session.endswith("question"):
        tools.append(tool("question-1", 15, "question", "running", input={"question": "Which database should the new service use?", "options": ["Postgres", "SQLite", "Keep the current one"]}))
    approval_call = session.endswith(("approval", "targets-unavailable", "targets-incomplete", "targets-over-limit", "scalar-approval"))
    boundary_call = session.endswith("boundary")
    targets_unavailable = session.endswith("targets-unavailable")
    targets_incomplete = session.endswith("targets-incomplete")
    targets_over_limit = session.endswith("targets-over-limit")
    permissions: list[dict[str, object]] = ([{
        "id": "fixture-approval",
        "call_id": "task-1",
        "tool": "Task",
        "key": "child",
        "preview": "Spawn child agent",
        "status": "pending" if approval_call else "resolved",
        "decision": None if approval_call else "allow_once",
        "persistence_available": True,
        "ts": 105,
        "targets": (None if session.endswith("scalar-approval") else [] if targets_unavailable else [{"role": "write", "path": "src/incomplete.py"}] if targets_incomplete else [
            {"role": "write", "path": f"src/target-{index}.py",
             "reason": "<img src=x onerror=window.targetsPwned=true>" if index == 0 else f"Fixture target {index}"}
            for index in range(65 if targets_over_limit else 8)
        ] if approval_call else None),
        **({"targets_unavailable": True} if targets_unavailable else {}),
    }] if approval_call or boundary_call else [])
    disconnected=session.endswith("disconnect") or session.startswith(("ui-effort-", "ui-context-select"))
    task_fixture = session.endswith(("task-done", "task-live"))
    live_task = session.endswith("task-live")
    child_body = {"session_id": session, "phase": "running" if live_task else "completed", "turns": [
        {"id": "child-turn-old", "phase": "completed", "messages": [message("child-old-message", 5, "assistant", "Assistant prose must not replace live tool activity.")],
         "tools": [tool("child-newest-tool", 31, "Grep", "completed", input={"pattern": "needle", "path": "src/newest.py"}, display="2 matches")]},
        {"id": "child-turn", "phase": "active" if live_task else "completed", "started_ts": 101,
         "messages": [message("child-message", 22, "assistant", "Child agent found the concise answer. More details follow.")],
         "tools": [tool("child-read", 18, "Read", "running" if live_task else "completed", input={"path": "src/current.py"}, display="Reading current file"),
                   tool("nested-task", 19, "Task", "completed", child_agent_ids=["grandchild-1"], input={"prompt": "Nested child prompt must stay hidden"})]},
    ], "agents": [{"id": "grandchild-1", "type": "build", "task": "grandchild prompt hidden in rows",
                   "description": "Use the linked child description as the fallback phrase.",
                   "status": "completed", "spawned_ts": 102, "completed_ts": 103.5,
                   "body": {"turns": [{"id": "grandchild-turn", "phase": "completed",
                       "messages": [message("grandchild-message", 2, "assistant", "Nested child complete.")],
                       "tools": [tool("grandchild-read", 3, "Read", display="Read nested file")]}]}}],
        "permissions": [], "input_queue": []} if task_fixture else {"session_id": session, "phase": "completed", "turns": [{"id": "child-turn", "phase": "completed",
            "messages": [message("child-message", 2, "assistant", "Child agent transcript is available.")],
            "tools": [tool("child-read", 3, "Read", display="Child result")]}], "agents": [], "permissions": [], "input_queue": []}
    return {"schema_version": 1, "session": session, "seq": 20, "view": {
        "session_id": session, "phase": "idle" if disconnected else "running", "model": {"provider": "scripted", "model": "m"},
        "turns": ([] if session.startswith("ui-context-select") else [
            {"id": "turn-fixture", "phase": "completed" if disconnected else "active", "started_ts": 100,
              "messages": [message("progress-1", 2, "assistant", "Inspecting the project and preparing a careful change. " * 8 + "\n\n# Heading\n\n- One\n- Two\n\n`a*b` and **bold** with *emphasis*.\n\n```\n*x* and **y**\n```\n\n<script>alert(1)</script>"),
                            *([message("task-reply", 9, "assistant", "The compact child summary is complete.")] if session.endswith("task-done") else []),
                            message("streaming-1", 20, "assistant", "Current live response remains fully visible.", done=False)],
             "tools": tools, "permission_ids": (["fixture-approval"] if permissions else [])},
            {"id": "turn-done", "phase": "completed", "messages": [message("final-1", 12, "assistant", "The final answer remains fully visible.")], "tools": []},
        ]), "permissions": permissions, "pending_permissions": permissions, "input_queue": [], "agents": [{
            "id": "child-1", "type": "explore", "task": "Inspect a related module",
            "description": "Child description fallback must lose to the Task input description.", "status": "running" if live_task else "completed",
            "spawned_ts": 100, "completed_ts": None if live_task else 103.25,
            "body": child_body,
        }],
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }}


async def verify_context_main_pane() -> None:
    """Browser-test the shared request renderer without daemon navigation."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        async def route_fixture(route) -> None:
            request = route.request
            if request.url.endswith("/js/context-view.js"):
                await route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=(ROOT / "nexus" / "ui" / "web" / "js" / "context-view.js").read_text(encoding="utf-8"),
                )
            elif request.url.endswith("/styles/context-preview.css"):
                await route.fulfill(
                    status=200,
                    content_type="text/css",
                    body=(ROOT / "nexus" / "ui" / "web" / "styles" / "context-preview.css").read_text(encoding="utf-8"),
                )
            else:
                await route.fulfill(status=200, content_type="text/html", body="<link rel='stylesheet' href='/styles/context-preview.css'><main><div id='context-inline-content'></div></main>")
        await page.route("http://context.test/**", route_fixture)
        await page.goto("http://context.test/")
        await page.evaluate("""async () => {
          const contextModule = await import('./js/context-view.js');
          const {renderCurrentContext} = contextModule;
          window.__contextModule = contextModule;
          const mount = document.querySelector('#context-inline-content');
          const result = {
            agent: {name: 'general'}, provider: 'scripted', model: 'm',
            system_text: 'system line 1\\nsystem line 2', tools_supported: true,
            tools: [{name: 'Read', description: 'Read a file', input_schema: {type: 'object', properties: {path: {type: 'string'}}}}],
            messages: [
              {role: 'user', blocks: [{type: 'text', text: 'Prior request'}]},
              {role: 'assistant', blocks: [{type: 'tool_use', name: 'Read', input: {path: 'a.py'}}]},
            ],
            history_included: true,
            request_context: {used_tokens: 400, input_budget: 8000},
            included_parts: [{name: 'identity', text: 'Nexus identity'}],
            skills_index: [{name: 'search', included: true}, {name: 'build', included: false}], mcp_index: 'docs: search',
            params: {temperature: 0.2}, omitted: [],
          };
          window.__contextResult = result;
          mount.replaceChildren(renderCurrentContext({result, preview: true, chooseAgent(){}, chooseModel(){}, openDetails(){}}));
          window.__contextFixture = true;
        }""")
        text = await page.locator("#context-inline-content").inner_text()
        assert "SYSTEM PROMPT · request.system" in text
        assert "TOOLS · structured request.tools · 1" in text and "Input schema · request.tools" in text
        assert "MESSAGES · ordered request.messages · 2" in text
        assert "Prior request" in text and "Tool call · Read" in text
        assert "400 / 8,000 input tokens" in text
        assert text.index("SYSTEM PROMPT · request.system") < text.index("TOOLS · structured request.tools") < text.index("MESSAGES · ordered request.messages")
        assert all(label in text for label in ("Skills", "Included in prompt", "Available, not included", "MCP", "docs: search", "Included prompt parts", "Request accounting"))
        assert await page.locator("#context-inline-content .context-link").count() >= 1
        empty_states = await page.evaluate("""() => {
          const mount = document.querySelector('#context-inline-content');
          const {renderCurrentContext} = window.__contextModule;
          const result = {system_text: '', tools: [], skills_index: [], mcp_index: '', included_parts: [], messages: []};
          mount.replaceChildren(renderCurrentContext({result, preview: true, openDetails(){}}));
          const cards = [...mount.querySelectorAll('.context-preview-card')];
          return {cards: cards.length, empty: cards.map(card => card.querySelector('.context-preview-empty')?.textContent || ''), muted: cards.every(card => getComputedStyle(card.querySelector('.context-preview-empty')).color !== getComputedStyle(card).color)};
        }""")
        assert empty_states["cards"] == 7
        assert all(empty_states["empty"])
        assert empty_states["muted"]
        await page.evaluate("""() => {
          const {renderCurrentContext} = window.__contextModule;
          document.querySelector('#context-inline-content').replaceChildren(renderCurrentContext({result: window.__contextResult, openDetails(){}}));
        }""")
        markdown_safety = await page.evaluate("""() => {
          const mount = document.querySelector('#context-inline-content');
          const {renderCurrentContext} = window.__contextModule;
          const result = {system_text: '# Heading\\n\\n**bold** and `code`\\n- first\\n- second\\n\\n<script>window.pwned=true</script> [bad](javascript:alert(1))'};
          mount.replaceChildren(renderCurrentContext({result, openDetails(){}}));
          const content = mount.querySelector('.context-markdown');
          return {
            heading: Boolean(content.querySelector('h1')),
            bold: Boolean(content.querySelector('strong')),
            code: Boolean(content.querySelector('code')),
            list: content.querySelectorAll('li').length,
            scripts: content.querySelectorAll('script').length,
            unsafeLinks: [...content.querySelectorAll('a')].filter(link => /^javascript:/i.test(link.href)).length,
            literalHtml: content.textContent.includes('<script>window.pwned=true</script>'),
          };
        }""")
        assert markdown_safety == {
            "heading": True, "bold": True, "code": True, "list": 2,
            "scripts": 0, "unsafeLinks": 0, "literalHtml": True,
        }
        preview_result = await page.evaluate("""async () => {
          const mount = document.querySelector('#context-inline-content');
          const {renderCurrentContext} = window.__contextModule;
          const result = {
            system_text: Array.from({length: 12}, (_, i) => `system ${i + 1}`).join('\\n'),
            tools: [{name: 'Long tool', description: 'description', input_schema: Object.fromEntries(Array.from({length: 12}, (_, i) => [`field-${i + 1}`, 'string']))}],
            messages: [{role: 'user', blocks: [{type: 'text', text: Array.from({length: 12}, (_, i) => `message ${i + 1}`).join('\\n')}]}],
            included_parts: [{name: 'Long part', text: Array.from({length: 12}, (_, i) => `part ${i + 1}`).join('\\n')}],
          };
          mount.replaceChildren(renderCurrentContext({result, preview: true, openDetails(){}}));
          await new Promise(requestAnimationFrame);
          return {
            text: mount.innerText,
            links: mount.querySelectorAll('.context-link:not([hidden])').length,
            previews: [...mount.querySelectorAll('.context-inline-part-preview')].map(node => {
              const style = getComputedStyle(node);
              return {
                 clamp: style.webkitLineClamp,
                overflow: style.overflow,
                lineHeight: parseFloat(style.lineHeight),
                height: node.getBoundingClientRect().height,
                clipped: node.scrollHeight > node.clientHeight + 1,
              };
            }),
          };
        }""")
        assert "system 10" in preview_result["text"] and "system 11" in preview_result["text"]
        assert "field-12" in preview_result["text"]
        assert "message 11" in preview_result["text"] and "part 11" in preview_result["text"]
        assert preview_result["links"] >= 4
        assert len(preview_result["previews"]) >= 4
        assert all(row["overflow"] == "hidden" and row["height"] <= row["lineHeight"] * 10 + 1 for row in preview_result["previews"]), preview_result["previews"]
        # The ten clamped text lines plus the preview's existing padding/border.
        assert all(row["height"] <= row["lineHeight"] * 10 + 28 for row in preview_result["previews"]), preview_result["previews"]
        assert sum(row["clipped"] for row in preview_result["previews"]) >= 4
        await browser.close()


async def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    await verify_context_main_pane()
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            TextDelta(text="First streamed half. "),
            Wait(gate),
            TextDelta(text="Second streamed half."),
            MessageStop(stop_reason="end_turn"),
        ],
        text_response("Web prompt reached the same daemon."),
        tool_response(("approval-call", "Write", {"path": "approval.txt", "content": "should be denied"})),
        text_response("The requested write was denied."),
    )
    config = Config(
        model="scripted/m", version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )

    with tempfile.TemporaryDirectory(prefix="nexus-web-e2e-", dir="/tmp") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        socket = root / "daemon.sock"

        def runtime_factory(path: Path, **_kwargs: object) -> Runtime:
            return Runtime(path, config=config, providers={"scripted": provider})

        daemon = Daemon(workspace, socket_path=socket, runtime_factory=runtime_factory)
        task = asyncio.create_task(daemon.serve_forever())
        terminal: UDSClient | None = None
        try:
            await _wait_for(lambda: daemon.started or task.done())
            if task.done():
                task.result()
            launch_url = await daemon.web_launch()
            terminal = await UDSClient.connect(socket, client_id="playwright-terminal")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
                await page.add_init_script("try{localStorage.setItem('nexus-web-panel','closed')}catch{}")
                await page.add_init_script("window.__nexusEventSources=[];const NativeEventSource=window.EventSource;window.EventSource=class extends NativeEventSource{constructor(...args){super(...args);window.__nexusEventSources.push(this)}}")
                errors: list[str] = []
                console_errors: list[str] = []
                static_failures: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text)
                        if message.type == "error" and "status of 503 (Service Unavailable)" not in message.text else None)
                page.on("response", lambda response: static_failures.append(f"{response.status} {response.url}")
                        if response.status >= 400 and any(part in response.url for part in ("/js/", "/styles/", "/assets/")) else None)
                context_commands: list[dict[str, object]] = []
                delayed_context = {"session": None, "entered": asyncio.Event(), "release": asyncio.Event()}
                context_failure = {"message": ""}
                setup_state = {"openai": False, "saved": False}
                context_fixture = {
                    "type": "ContextInspectResult", "mode": "next_turn_preview", "actually_sent": False,
                    "draft_provided": False, "manifest_generation": 17,
                    "agent": {"name": "coding <script>window.contextPwned=true</script>", "source": "workspace", "instructions_included": True},
                    "system_files": {"soul": {"configured": True, "loaded": True, "included": True,
                                                   "included_nonempty": True, "truncated": False, "source": "SOUL.md"},
                                     "memory": {"configured": True, "loaded": True, "included": False,
                                                   "included_nonempty": False, "truncated": False}},
                    "system_text": "\n".join([f"Prompt line {i} <script>window.contextPwned=true</script>" for i in range(1, 13)]),
                    "redacted_for_display": True,
                    "skills_index": [{"name": "included-skill", "description": "Included skill", "included": True},
                                     {"name": "available-skill", "description": "Available only", "included": False}],
                    "mcp_index": "connected-server · resource index",
                    "included_parts": [{"name": "agent", "text": "\n".join([f"Agent line {i}" for i in range(1, 13)])}, {"name": "soul", "text": "Soul text"}],
                    "tools": [{"name": "Read", "description": "Read files", "input_schema": {"type": "object", "properties": {f"field-{i}": {"type": "string"} for i in range(12)}}}],
                    "messages": [{"role": "user", "blocks": [{"type": "text", "text": "\n".join([f"Prior line {i}" for i in range(1, 13)])}]},
                                 {"role": "assistant", "blocks": [{"type": "text", "text": "I found the failing path."}]}],
                    "history_included": True,
                    "request_context": {"input_budget": 8000, "used_tokens": 420},
                    "tools_supported": True, "model": "m", "provider": "scripted",
                    "budget": {"input_budget": 8000, "system_tokens": 250}, "omitted": ["draft input (not provided)"],
                }

                async def route_context_command(route) -> None:
                    request = route.request
                    if not request.url.endswith("/v1/web/command"):
                        await route.fallback()
                        return
                    command = json.loads(request.post_data or "{}")
                    if command.get("type") == "SetupStatus":
                        result = {
                            "type": "SetupStatusResult", "required": not setup_state["saved"],
                            "global_model": "openai/gpt-6-sol" if setup_state["saved"] else "",
                            "effective_model": "scripted/m", "providers": [
                                {"id": "codex", "label": "ChatGPT (Codex)", "connected": False, "auto": True,
                                 "instruction": "Sign in with your ChatGPT account in Settings → Providers."},
                                {"id": "openai", "label": "OpenAI", "connected": setup_state["openai"], "auto": True,
                                 "instruction": "Set OPENAI_API_KEY in the daemon environment."},
                                {"id": "anthropic", "label": "Anthropic", "connected": False, "auto": True,
                                 "instruction": "Set ANTHROPIC_API_KEY in the daemon environment."},
                                {"id": "ollama", "label": "Ollama", "connected": True, "auto": False,
                                 "instruction": "Local provider; availability is not checked. Install and run Ollama locally."},
                            ],
                        }
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                        return
                    if command.get("type") == "SetupSave":
                        # No model: the host picks the provider's newest one.
                        assert command.get("provider") == "openai" and not command.get("model"), command
                        setup_state["saved"] = True
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                            "type": "SetupSaveResult", "global_model": "openai/gpt-6-sol", "restart_required": False,
                        }))
                        return
                    if command.get("type") != "ContextInspect":
                        await route.fallback()
                        return
                    context_commands.append(command)
                    inspection_number = sum(item.get("session") == command.get("session")
                                            for item in context_commands)
                    if context_failure["message"]:
                        await route.fulfill(status=200, content_type="application/json",
                                            body=json.dumps({"type": "ErrorResult", "message": context_failure["message"]}))
                        return
                    sid = command["session"]
                    if delayed_context["session"] == sid:
                        delayed_context["session"] = None
                        delayed_context["entered"].set()
                        await delayed_context["release"].wait()
                    fixture = {**context_fixture, "session": sid,
                               "system_text": context_fixture["system_text"].replace(
                                   "Prompt line 1 <",
                                   f"Session marker {sid} · inspection {inspection_number} <", 1)}
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(fixture))

                await page.route("**/v1/web/command", route_context_command)
                await page.goto(launch_url, wait_until="domcontentloaded")
                setup_dialog = page.get_by_role("dialog", name="Connect a provider")
                await setup_dialog.wait_for(state="visible", timeout=5_000)
                assert await page.locator("#app").evaluate("node => node.inert")
                # Sign-in cards are the Settings → Providers cards; env-key providers are listed below.
                await setup_dialog.locator("#setup-provider-list .provider-card").first.wait_for(timeout=5_000)
                assert "OPENAI_API_KEY" in await setup_dialog.locator("#setup-env").inner_text()
                # Ollama is reachable but never picked, so nothing is saved yet.
                await page.wait_for_timeout(2_500)
                assert not setup_state["saved"]
                await page.keyboard.press("Tab")
                assert await setup_dialog.evaluate("node => node.contains(document.activeElement)")
                await page.keyboard.press("Escape")
                await setup_dialog.wait_for(state="hidden")
                assert not await page.locator("#app").evaluate("node => node.inert")
                # Connecting a provider completes setup with its newest model; no restart.
                setup_state["openai"] = True
                await page.reload(wait_until="domcontentloaded")
                await page.get_by_text("Using openai/gpt-6-sol").wait_for(timeout=5_000)
                assert setup_state["saved"]
                assert await page.locator("#setup-overlay").is_hidden()
                await page.locator("#new-session").click()
                new_picker = page.get_by_role("dialog", name="New session · choose an agent")
                await new_picker.wait_for(timeout=5_000)
                await page.keyboard.press("Enter")  # keep the preselected agent
                try:
                    await page.wait_for_url("**/s/*", timeout=10_000)
                except Exception as exc:
                    raise AssertionError({"navigation": str(exc), "errors": errors, "page": await page.locator("body").inner_text()}) from exc
                session = page.url.rsplit("/s/", 1)[1]
                assert session
                await page.locator("#connection-label").get_by_text("Live sync").wait_for(timeout=5_000)
                await terminal.call(p.SessionOpen(session="sidebar-check"))
                await terminal.call(p.SessionArchive(session="sidebar-check"))
                archived_row = page.locator('#archived-list .session-item').filter(
                    has=page.locator('.session-row[title*="sidebar-check"]')
                )
                await archived_row.wait_for(timeout=5_000)
                assert await archived_row.locator(".session-delete").is_visible()
                assert await page.locator("#session-list .session-delete").count() == await page.locator("#session-list .session-item").count()
                await archived_row.get_by_role("button", name="Delete").click()
                await page.get_by_text("Deleted “sidebar-check”").wait_for(timeout=5_000)
                await page.locator("#toast-region .toast").last.get_by_role("button", name="Undo").click()
                await archived_row.wait_for(timeout=5_000)
                # Like the terminal, every conversation opens on the four-block
                # request header (ui_support/tui_context_header.py).
                header = page.locator("#timeline > .context-header")
                await header.get_by_text(f"Session marker {session}", exact=False).wait_for(timeout=5_000)
                assert await page.locator("#timeline > *").first.evaluate("n => n.classList.contains('context-header')")
                assert await page.locator("#context-preview").is_hidden()
                main_context = await header.inner_text()
                assert [await chip.text_content() for chip in await header.locator(".context-chip").all()] == ["System prompt", "Tools", "Skills", "MCP"]
                assert "Prompt line 5" in main_context and "Prompt line 6" not in main_context
                assert "… +7 more lines" in main_context
                assert "Read" in main_context and "included-skill" in main_context and "available-skill" not in main_context
                assert await header.locator(".context-block.empty").count() == 1
                assert await header.locator("script").count() == 0
                # The System prompt block shows only the prompt, rendered as Markdown.
                await header.locator(".context-block").first.click()
                system_dialog = page.get_by_role("dialog", name="System prompt")
                await system_dialog.wait_for(state="visible")
                await system_dialog.get_by_text("Prompt line 12", exact=False).first.wait_for()
                assert await system_dialog.locator(".ctx-system .context-markdown").count() == 1
                await page.keyboard.press("Escape")
                await header.locator(".context-block").nth(2).click()
                context_dialog = page.get_by_role("dialog", name="Current context")
                await context_dialog.wait_for(state="visible")
                # Grouped like the TUI: system prompt, tools, then one group per turn, all collapsed.
                group_titles = await context_dialog.locator(".ctx-group > summary .ctx-title").all_inner_texts()
                assert group_titles[:2] == ["System prompt", "Tools"] and group_titles[-1] == "Request details", group_titles
                assert any(title.startswith("Turn ") for title in group_titles), group_titles
                assert await context_dialog.locator(".ctx-group[open]").count() == 0
                await context_dialog.get_by_role("button", name="Expand all").click()
                await context_dialog.get_by_text("Prompt line 12", exact=False).first.wait_for()
                assert "Schema" in await context_dialog.inner_text()
                assert "field-11" in await context_dialog.inner_text()
                assert "Prior line 12" in await context_dialog.inner_text()
                assert "Agent line 12" in await context_dialog.inner_text()
                assert await context_dialog.locator("script").count() == 0
                assert await page.evaluate("window.contextPwned") is None
                await page.keyboard.press("Escape")
                assert await context_dialog.count() == 0
                assert await page.locator("#composer-input").input_value() == ""
                # The Tools block opens the grouped tool list instead.
                await header.locator(".context-block").nth(1).click()
                tools_dialog = page.get_by_role("dialog", name="Tools")
                await tools_dialog.wait_for(state="visible")
                # One row per tool; a family of one tool has no header of its own.
                assert await tools_dialog.locator(".ctx-entry").count() >= 1
                assert await tools_dialog.locator(".ctx-entry[open]").count() == 0
                assert await tools_dialog.locator(".ctx-group:not([open])").count() == 0
                assert "definition" in await tools_dialog.inner_text()
                await page.keyboard.press("Escape")
                assert await tools_dialog.count() == 0
                # A failed inspection is reported in the dialog; the header keeps the last good one.
                context_failure["message"] = "Cannot inspect context while a turn is active"
                await page.locator("#context-toolbar").click()
                await context_dialog.wait_for(state="visible")
                await page.wait_for_timeout(500)
                context_status = await page.locator("#context-modal-status").inner_text()
                assert "inspection is unavailable" in context_status.lower(), {"status": context_status, "context_commands": context_commands[-3:]}
                await page.keyboard.press("Escape")
                assert f"Session marker {session}" in await header.inner_text()
                assert await page.locator("#composer-input").is_enabled()
                context_failure["message"] = ""
                # A slow inspection for one session never paints over another.
                delayed_context["entered"].clear()
                delayed_context["release"].clear()
                delayed_context["session"] = session
                await page.locator("#context-toolbar").click()
                await asyncio.wait_for(delayed_context["entered"].wait(), timeout=5)
                await page.keyboard.press("Escape")
                context_switch_session = "context-switch-target"
                daemon.facade.open_session(context_switch_session, create=True, recover=True)
                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", context_switch_session)
                await header.get_by_text(f"Session marker {context_switch_session}", exact=False).wait_for(timeout=5_000)
                delayed_context["release"].set()
                await page.wait_for_timeout(100)
                assert f"Session marker {context_switch_session}" in await header.inner_text()
                assert f"Session marker {session} " not in await header.inner_text()
                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", session)
                await header.get_by_text(f"Session marker {session}", exact=False).wait_for(timeout=5_000)
                await page.locator("#composer-input").fill("must remain an unsent draft")

                started_at = time.monotonic()
                result = await terminal.call(p.SessionStart(session=session, content="Started in Textual"))
                assert isinstance(result, p.SessionStartResult)
                try:
                    await page.get_by_text("First streamed half.", exact=False).wait_for(timeout=8_000)
                except Exception as exc:
                    handle=daemon.facade.runtime.session(session)
                    raise AssertionError({"wait_error":str(exc),"timeline":await page.locator("#timeline").inner_text(),"events":[(event.type,event.data) for event in handle.events[-8:]]}) from exc
                first_latency_ms = round((time.monotonic() - started_at) * 1000)
                records = daemon.facade.runtime.session(session).read().records
                turn_index = next(
                    index for index, record in enumerate(records)
                    if isinstance(record, EventRecord)
                    and record.event.type == "turn.started"
                    and record.event.turn == result.turn_id
                )
                turn_started = records[turn_index].event
                previous_index = next(
                    index for index in range(turn_index - 1, -1, -1)
                    if isinstance(records[index], EventRecord)
                )
                previous_event = records[previous_index].event
                intervening = records[previous_index + 1:turn_index]
                assert any(
                    isinstance(record, MessageRecord)
                    and record.message.role == "user"
                    and record.seq == turn_started.seq - 1
                    for record in intervening
                ), (previous_event.seq, turn_started.seq, intervening)
                assert turn_started.seq > previous_event.seq + 1
                input_started = next(
                    record.event for record in records
                    if isinstance(record, EventRecord)
                    and record.event.type == "input.started"
                    and record.event.turn == result.turn_id
                )
                assert input_started.seq == turn_started.seq + 1

                # A page reload during an unfinished model stream must replay
                # the authoritative prefix and continue on the same daemon turn.
                await page.reload(wait_until="domcontentloaded")
                await page.get_by_text("First streamed half.", exact=False).wait_for(timeout=8_000)
                active_sidebar_row = page.locator("#session-list .session-item:has(.session-row.active)")
                await active_sidebar_row.locator('.session-status[data-status="working"]').wait_for(timeout=5_000)
                assert await active_sidebar_row.locator(".session-status").evaluate(
                    "node => getComputedStyle(node, '::before').animationName"
                ) == "spin"
                before_active_send = len([r for r in daemon.facade.runtime.session(session).read().records if isinstance(r, EventRecord) and r.event.type in {"input.started", "input.queued"}])
                assert await page.locator("#composer-input").is_disabled()
                await page.locator("#composer-form").evaluate("e=>e.requestSubmit()")
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(120)
                after_records = daemon.facade.runtime.session(session).read().records
                after_active_send = len([r for r in after_records if isinstance(r, EventRecord) and r.event.type in {"input.started", "input.queued"}])
                assert after_active_send == before_active_send
                assert await page.locator("#composer-input").input_value() == "must remain an unsent draft"
                await page.screenshot(path=str(ARTIFACTS / "running-draft.png"), full_page=True)
                gate.set()
                await page.get_by_text("Second streamed half.", exact=False).wait_for(timeout=8_000)
                await page.locator("#stop-button").wait_for(state="hidden", timeout=8_000)
                assert await page.locator("#send-button").count() == 0
                assert await page.locator("#connection-banner").is_hidden()
                await page.screenshot(path=str(ARTIFACTS / "light.png"), full_page=True)

                baseline = await terminal.call(p.SessionState(session=session))
                assert isinstance(baseline, p.SessionStateResult)
                subscription = await terminal.subscribe(session, baseline.seq, follow=True)
                try:
                    await page.locator("#composer-input").fill("Started in web")
                    await page.locator("#composer-input").press("Enter")

                    async def terminal_completion() -> list[str]:
                        seen: list[str] = []
                        async for event in subscription:
                            seen.append(event.type)
                            if event.type == "turn.completed":
                                break
                        return seen

                    seen = await asyncio.wait_for(terminal_completion(), 10)
                    assert "turn.started" in seen and "text.delta" in seen and "turn.completed" in seen, seen
                finally:
                    await subscription.aclose()

                await page.get_by_text("Web prompt reached the same daemon.", exact=False).wait_for(timeout=8_000)
                await page.reload(wait_until="domcontentloaded")
                await page.get_by_text("Web prompt reached the same daemon.", exact=False).wait_for(timeout=8_000)
                assert await page.get_by_text("Web prompt reached the same daemon.", exact=False).count() == 1

                # Approval remains a daemon decision. The browser shows the
                # pending request, and a later terminal answer cannot override
                # the browser's first response.
                request = await terminal.call(p.SessionStart(session=session, content="Try a gated write"))
                assert isinstance(request, p.SessionStartResult)
                approval = page.locator(".permission-card")
                await approval.wait_for(state="visible", timeout=8_000)
                await page.screenshot(path=str(ARTIFACTS / "approval.png"), full_page=True)
                pending = daemon.facade.state(session)[0].pending_permissions
                assert len(pending) == 1
                assert daemon.facade.presence.viewers(session) >= 1
                await page.locator("#connection-label").get_by_text("Live sync").wait_for(timeout=2_000)
                request_id = pending[0].id
                await approval.get_by_role("button", name="Deny once").click()
                await approval.wait_for(state="hidden", timeout=8_000)
                late = await terminal.call(p.PermissionResolve(session=session, request_id=request_id, decision="allow_once"))
                assert isinstance(late, p.PermissionResolveResult) and not late.resolved
                resolutions = [
                    event for event in daemon.facade.runtime.session(session).events
                    if event.type == "permission.resolved" and event.data.get("id") == request_id
                ]
                assert len(resolutions) == 1
                assert resolutions[0].data.get("decision") == "deny_once"
                assert daemon.facade.presence.viewers(session) >= 1
                await page.get_by_text("The requested write was denied.", exact=False).wait_for(timeout=8_000)
                assert not (workspace / "approval.txt").exists()

                # Keep the cross-client flow above on the real daemon projection;
                # use a shaped host projection for the deterministic presentation matrix.
                fixture_a, fixture_b, fixture_approval, fixture_boundary = "ui-fixture-a", "ui-fixture-b", "ui-fixture-approval", "ui-fixture-boundary"
                fixture_targets_unavailable, fixture_targets_incomplete, fixture_targets_over_limit = "ui-fixture-targets-unavailable", "ui-fixture-targets-incomplete", "ui-fixture-targets-over-limit"
                fixture_scalar_approval = "ui-fixture-scalar-approval"
                fixture_question = "ui-fixture-question"
                effort_a, effort_b, effort_none = "ui-effort-a", "ui-effort-b", "ui-effort-none"
                context_select_session = "ui-context-select-agent-model"
                for effort_session in (effort_a, effort_b, effort_none):
                    daemon.facade.open_session(effort_session, create=True, recover=True)
                daemon.facade.open_session(context_select_session, create=True, recover=True)
                daemon.facade.open_session(fixture_a, create=True, recover=True)
                daemon.facade.open_session(fixture_b, create=True, recover=True)
                daemon.facade.open_session(fixture_approval, create=True, recover=True)
                daemon.facade.open_session(fixture_boundary, create=True, recover=True)
                daemon.facade.open_session(fixture_targets_unavailable, create=True, recover=True)
                daemon.facade.open_session(fixture_targets_incomplete, create=True, recover=True)
                daemon.facade.open_session(fixture_targets_over_limit, create=True, recover=True)
                daemon.facade.open_session(fixture_scalar_approval, create=True, recover=True)
                daemon.facade.open_session(fixture_question, create=True, recover=True)
                fixture_disconnect="ui-fixture-disconnect"
                daemon.facade.open_session(fixture_disconnect,create=True,recover=True)
                original_snapshot = daemon.facade.web_snapshot
                daemon.facade.web_snapshot = lambda sid, from_seq=0: _web_fixture(sid) if sid.startswith(("ui-fixture-", "ui-effort-", "ui-context-select")) else original_snapshot(sid, from_seq)
                effort_state = {
                    effort_a: {"levels": ["low", "medium", "high"], "effort": "medium", "agent": "general"},
                    effort_b: {"levels": ["none", "xhigh"], "effort": None, "agent": "general"},
                    effort_none: {"levels": [], "effort": None, "agent": "general"},
                    context_select_session: {"levels": ["low", "medium", "high"], "effort": "medium", "agent": "general", "model": "scripted/m"},
                }
                effort_commands: list[dict[str, object]] = []
                selection_commands: list[dict[str, object]] = []
                reject_effort = {"next": False}
                delayed_metadata = {
                    "session": None,
                    "entered": asyncio.Event(),
                    "release": asyncio.Event(),
                    "completed": asyncio.Event(),
                }

                async def route_effort_command(route) -> None:
                    import json

                    request = route.request
                    if not request.url.endswith("/v1/web/command"):
                        await route.fallback()
                        return
                    command = json.loads(request.post_data or "{}")
                    sid = command.get("session")
                    if command.get("type") == "ModelsList":
                        result = {"type": "ModelsListResult", "count": 3, "models": [
                            {"provider": "scripted", "id": "n", "reference": "scripted/n", "name": "Alternate model",
                             "supported_efforts": ["none", "xhigh"]},
                            {"provider": "scripted", "id": "m", "reference": "scripted/m", "name": "Medium model",
                             "supported_efforts": ["low", "medium", "high"]},
                            {"provider": "scripted", "id": "noeff", "reference": "scripted/noeff", "name": "No effort model",
                             "supported_efforts": []},
                        ]}
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                        return
                    if command.get("type") == "AgentsList":
                        result = {"type": "AgentsListResult", "agents": [
                            {"name": "general", "description": "General-purpose assistant"},
                            {"name": "explore", "description": "Explore the workspace"},
                        ]}
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                        return
                    if sid not in effort_state or command.get("type") not in {"AgentCurrent", "ReasoningEffortSelect", "ModelSelect", "AgentSelect", "AgentReset"}:
                        await route.fallback()
                        return
                    current = effort_state[sid]
                    if command["type"] == "ReasoningEffortSelect":
                        effort_commands.append(command)
                        if reject_effort["next"]:
                            reject_effort["next"] = False
                            result = {"type": "ReasoningEffortSelectResult", "session": sid, "accepted": False,
                                      "stored_override": current["effort"], "effective_effort": current["effort"],
                                      "source": "session", "supported_levels": current["levels"], "apply_next_turn": True}
                            await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                            return
                        current["effort"] = command.get("effort")
                        result = {"type": "ReasoningEffortSelectResult", "session": sid, "accepted": True,
                                  "stored_override": current["effort"], "effective_effort": current["effort"],
                                  "source": "session", "supported_levels": current["levels"], "apply_next_turn": True}
                    elif command["type"] == "ModelSelect":
                        selection_commands.append(command)
                        current["model"] = command["ref"]
                        current["levels"] = {"scripted/m": ["low", "medium", "high"],
                                              "scripted/n": ["none", "xhigh"],
                                              "scripted/noeff": []}[command["ref"]]
                        if current["effort"] not in current["levels"]:
                            current["effort"] = None
                        result = {"type": "ModelSelectResult", "session": sid, "accepted": True,
                                  "reference": command["ref"], "provider": "scripted",
                                  "model": command["ref"].split("/", 1)[1], "apply_next_turn": True}
                    elif command["type"] in {"AgentSelect", "AgentReset"}:
                        current["agent"] = command.get("name", "general")
                        result = {"type": "AgentSelectResult", "session": sid, "name": current["agent"],
                                  "source": "session", "apply_next_turn": True}
                    else:
                        hold_response = delayed_metadata["session"] == sid
                        if hold_response:
                            delayed_metadata["session"] = None
                            delayed_metadata["entered"].set()
                            await delayed_metadata["release"].wait()
                        result = {"type": "AgentCurrentResult", "session": sid, "name": current["agent"], "source": "session",
                                  "provider": "scripted", "model": current.get("model", "scripted/m").split("/", 1)[-1],
                                  "reasoning_effort": current["effort"], "supported_levels": current["levels"],
                                  "stored_override": current["effort"],
                                  "reasoning_effort_source": "session" if current["effort"] else None}
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                    if command["type"] == "AgentCurrent" and hold_response:
                        delayed_metadata["completed"].set()

                await page.route("**/v1/web/command", route_effort_command)
                delayed_logs = {"session": None, "entered": asyncio.Event(), "release": asyncio.Event()}
                logs_reads: list[dict[str, object]] = []
                logs_failure = {"status": 0}

                async def route_logs_command(route) -> None:
                    import json

                    request = route.request
                    if not request.url.endswith("/v1/web/command"):
                        await route.fallback()
                        return
                    command = json.loads(request.post_data or "{}")
                    if command.get("type") != "LogsRead":
                        await route.fallback()
                        return
                    logs_reads.append(command)
                    if logs_failure["status"]:
                        await route.fulfill(status=logs_failure["status"], content_type="application/json",
                                            body=json.dumps({"error": "Logs temporarily unavailable"}))
                        return
                    sid = command.get("session")
                    if delayed_logs["session"] == sid:
                        delayed_logs["session"] = None
                        delayed_logs["entered"].set()
                        await delayed_logs["release"].wait()
                    daemon_cursor = command.get("daemon_cursor")
                    session_cursor = command.get("session_cursor")
                    # Presentation-only shaped rows exercise text escaping.
                    # These strings are not captured daemon payloads and do
                    # not assert that raw daemon content is forwarded as logs.
                    daemon_entries = ([
                        {"source": "daemon", "seq": 1, "ts": log_timestamp, "level": "info", "kind": "daemon.started", "summary": "Daemon ready <script>window.logsPwned=true</script>\x1b[31m"},
                        {"source": "daemon", "seq": 2, "ts": log_timestamp + 1, "level": "error", "kind": "daemon.session_failed", "summary": "Daemon error entry"},
                    ] if daemon_cursor is None else [
                        {"source": "daemon", "seq": 3, "ts": log_timestamp + 2, "level": "warning", "kind": "daemon.updated", "summary": "Daemon update"},
                    ] if daemon_cursor.endswith(":2") else [])
                    session_entries = ([
                        {"source": "session", "seq": 1, "ts": log_timestamp + 3, "level": "info", "kind": "turn.started", "summary": f"Session {sid} ready <b>literal</b>\x1b[32m"},
                        {"source": "session", "seq": 2, "ts": None, "level": "error", "kind": "turn.failed", "summary": f"Session {sid} error entry"},
                    ] if session_cursor is None or session_cursor == 0 else [
                        {"source": "session", "seq": 3, "ts": log_timestamp + 5, "level": "warning", "kind": "turn.updated", "summary": f"Session {sid} update"},
                    ] if session_cursor == 2 else [])
                    result = {"type": "LogsReadResult", "daemon": {"entries": daemon_entries,
                              "next_cursor": "fixture-generation:3" if daemon_cursor and daemon_cursor.endswith((":2", ":3")) else "fixture-generation:2",
                              "truncated": daemon_cursor is None, "has_more": False},
                              "session": {"entries": session_entries,
                              "next_cursor": max([command.get("session_cursor") or 0, *[row["seq"] for row in session_entries]]),
                              "truncated": session_cursor is None, "has_more": False}}
                    try:
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                    except Exception:  # noqa: BLE001, S110 - route may be aborted when the drawer closes
                        # A LogsRead aborted by closing the drawer may outlive
                        # this deterministic route handler until its release.
                        pass

                await page.route("**/v1/web/command", route_logs_command)

                # A host-owned worktree fixture exercises the typed list/review/
                # acknowledge/preview/confirm sequence without touching git.
                worktree_commands: list[dict[str, object]] = []
                worktree_mode = {"stale": True}
                child_id = "child-<img src=x onerror=window.worktreePwned=true>"
                review_id, review_digest = "a" * 32, "b" * 64

                async def route_worktree_command(route) -> None:
                    request = route.request
                    if not request.url.endswith("/v1/web/command"):
                        await route.fallback()
                        return
                    command = json.loads(request.post_data or "{}")
                    kind = command.get("type")
                    if not isinstance(kind, str) or not kind.startswith("Worktree"):
                        await route.fallback()
                        return
                    worktree_commands.append(command)
                    if kind == "WorktreeList":
                        result = {"type": "WorktreeListResult", "status": "ok", "has_more": False,
                                  "worktrees": [{"child_id": child_id, "lifecycle": "finalized", "dirty": False,
                                                 "review_id": review_id, "digest": review_digest, "acknowledged": False}]}
                    elif kind == "WorktreeInspect":
                        result = {"type": "WorktreeInspectResult", "child_id": child_id, "status": "finalized",
                                  "record": {"child_id": child_id, "lifecycle": "finalized", "final_status": "completed",
                                             "review_status": "ready", "dirty": False, "review_id": review_id,
                                             "digest": review_digest, "acknowledged": False}}
                    elif kind == "WorktreeReview":
                        assert command.get("limit") == 8, command
                        cursor = command.get("cursor", 0)
                        end = min(cursor + 8, 17)
                        entries = [{"path": "<img src=x onerror=window.worktreePwned=true>", "change": "modified"}]
                        diff = [{"path": "<img src=x onerror=window.worktreePwned=true>",
                                 "patch": "@@ -1 +1 @@\n-<script>window.worktreePwned=true</script>\n+<img src=x onerror=window.worktreePwned=true>"}]
                        result = {"type": "WorktreeReviewResult", "child_id": child_id, "status": "finalized",
                                  "record": {"child_id": child_id, "lifecycle": "finalized", "dirty": False,
                                             "review_id": review_id, "digest": review_digest, "acknowledged": False},
                                  "entries": entries, "diff": diff * (end - cursor), "cursor": cursor,
                                  "has_more": end < 17, "review_id": review_id, "digest": review_digest}
                    elif kind == "WorktreeAcknowledge":
                        result = {"type": "WorktreeAcknowledgeResult", "child_id": child_id,
                                  "status": "acknowledged", "review_id": review_id, "digest": review_digest}
                    elif kind == "WorktreeIntegrate" and not command.get("confirmation_token"):
                        result = {"type": "WorktreeMutationResult", "child_id": child_id,
                                  "status": "requires_confirmation", "operation": "integrate",
                                  "review_id": review_id, "digest": review_digest,
                                  "confirmation_token": "fixture-token-1",
                                  "impact": {"summary": "Apply the acknowledged frozen review", "review_digest": review_digest}}
                    elif kind == "WorktreeIntegrate" and worktree_mode["stale"]:
                        worktree_mode["stale"] = False
                        result = {"type": "ErrorResult", "kind": "error", "message": "stale confirmation token"}
                    elif kind == "WorktreeIntegrate":
                        result = {"type": "WorktreeMutationResult", "child_id": child_id,
                                  "status": "committed", "operation": "integrate", "review_id": review_id,
                                  "digest": review_digest, "transaction_id": "fixture-transaction"}
                    else:
                        result = {"type": "ErrorResult", "kind": "error", "message": "unexpected worktree command"}
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

                await page.route("**/v1/web/command", route_worktree_command)

                await page.goto(f"{page.url.rsplit('/s/', 1)[0]}/s/{fixture_a}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.locator("#inspector-toggle").click()
                await page.locator("#tab-worktrees").click()
                await page.get_by_role("button", name=child_id).wait_for(timeout=5_000)
                assert any(item["type"] == "WorktreeList" for item in worktree_commands)
                await page.get_by_role("button", name=child_id).click()
                await page.get_by_role("button", name="Review frozen changes").click()
                malicious = "<img src=x onerror=window.worktreePwned=true>"
                await page.locator(".worktree-review .worktree-file").get_by_text(malicious, exact=False).wait_for()
                assert await page.locator(".worktree-review img, .worktree-review script").count() == 0
                assert await page.evaluate("window.worktreePwned") is None
                assert await page.locator(".worktree-diff-file").count() == 8
                assert await page.locator(".worktree-diff-file .diff-text").nth(1).inner_text() == "<script>window.worktreePwned=true</script>"
                await page.get_by_role("button", name="Load next diff page").click()
                assert worktree_commands[-1]["cursor"] == 8 and worktree_commands[-1]["limit"] == 8
                await page.locator(".worktree-diff-file").nth(15).wait_for()
                assert await page.locator(".worktree-diff-file").count() == 16
                await page.get_by_role("button", name="Load next diff page").click()
                assert worktree_commands[-1]["cursor"] == 16 and worktree_commands[-1]["limit"] == 8
                await page.locator(".worktree-diff-file").nth(16).wait_for()
                assert await page.locator(".worktree-diff-file").count() == 17
                await page.get_by_role("button", name="Acknowledge this digest").click()
                assert worktree_commands[-1] == {"type": "WorktreeAcknowledge", "child_id": child_id,
                                                 "review_id": review_id, "digest": review_digest}
                await page.get_by_role("button", name="Preview integrate").click()
                dialog = page.get_by_role("alertdialog", name="Confirm integration")
                await dialog.wait_for(state="visible")
                assert "fixture-token-1" not in await dialog.inner_text()
                confirm = dialog.get_by_role("button", name="Confirm")
                await confirm.click()
                await page.get_by_text("The preview token may be stale or already consumed. Refresh the owned worktree state and review again.", exact=True).wait_for()
                assert "Confirmation failed: stale confirmation token" in await page.locator("#worktree-confirm-summary").inner_text()
                assert await dialog.get_by_role("button", name="Close").is_enabled()
                await dialog.get_by_role("button", name="Close").click()
                await page.locator("#inspector-content").get_by_role("button", name="Preview integrate").wait_for()
                await page.get_by_role("button", name="Preview integrate").click()
                dialog = page.get_by_role("alertdialog", name="Confirm integration")
                confirm = dialog.get_by_role("button", name="Confirm")
                async with page.expect_request(lambda req: req.url.endswith("/v1/web/command") and
                                               json.loads(req.post_data or "{}").get("type") == "WorktreeIntegrate" and
                                               bool(json.loads(req.post_data or "{}").get("confirmation_token"))) as confirm_request:
                    await confirm.click()
                submitted = json.loads((await confirm_request.value).post_data or "{}")
                assert submitted["confirmation_token"] == "fixture-token-1"
                assert submitted["child_id"] == child_id and submitted["digest"] == review_digest
                await page.get_by_text("Integration committed.", exact=True).wait_for()
                assert await page.evaluate("window.worktreePwned") is None
                await dialog.get_by_role("button", name="Close").click()
                await page.locator("#close-inspector").click()

                # Malformed target lists are explicit fail-closed approvals:
                # allow rows are shown disabled, and denial still reaches the host.
                for target_session in (fixture_targets_unavailable, fixture_targets_incomplete, fixture_targets_over_limit):
                    await page.goto(f"{page.url.split('/s/')[0]}/s/{target_session}", wait_until="domcontentloaded")
                    approval_card = page.locator(".permission-card")
                    await approval_card.wait_for(state="visible", timeout=5_000)
                    await page.get_by_text("Target details are unavailable or incomplete. This request cannot be approved.", exact=True).wait_for()
                    assert await approval_card.get_by_role("button", name="Allow once").is_disabled()
                    assert await approval_card.get_by_role("button", name="Allow for session").is_disabled()
                    assert await approval_card.get_by_role("button", name="Deny once").is_enabled()
                    async with page.expect_request(lambda request: request.url.endswith("/v1/web/command") and
                                                   json.loads(request.post_data or "{}").get("type") == "PermissionResolve") as request_info:
                        await approval_card.get_by_role("button", name="Deny once").click()
                    deny_command = json.loads((await request_info.value).post_data or "{}")
                    assert deny_command["decision"] == "deny_once", deny_command

                await page.goto(f"{page.url.split('/s/')[0]}/s/{fixture_scalar_approval}", wait_until="domcontentloaded")
                scalar_approval = page.locator(".permission-card")
                await scalar_approval.wait_for(state="visible", timeout=5_000)
                assert await scalar_approval.get_by_role("button", name="Allow once").is_enabled()
                assert await scalar_approval.get_by_role("button", name="Allow for session").is_enabled()

                # An agent question is the running ``question`` call, shown as a
                # list like the pickers and answered by call id.
                await page.goto(f"{page.url.split('/s/')[0]}/s/{fixture_question}", wait_until="domcontentloaded")
                question_card = page.locator(".question-card")
                await question_card.wait_for(state="visible", timeout=5_000)
                await question_card.get_by_text("Which database should the new service use?").wait_for()
                assert await question_card.get_by_role("button", name="SQLite").is_enabled()
                await page.screenshot(path=str(ARTIFACTS / "question.png"))
                async with page.expect_request(lambda request: request.url.endswith("/v1/web/command") and
                                               json.loads(request.post_data or "{}").get("type") == "QuestionAnswer") as request_info:
                    await page.keyboard.press("2")
                answer_command = json.loads((await request_info.value).post_data or "{}")
                assert (answer_command["call_id"], answer_command["answer"]) == ("question-1", "2"), answer_command

                log_timestamp = int(time.time())
                await page.goto(f"{page.url.split('/s/')[0]}/s/{effort_a}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                effort_label = page.locator("#reasoning-effort")
                await page.get_by_text("Effort: medium", exact=True).wait_for()
                metadata = page.locator(".composer-context")
                assert "session" not in (await metadata.inner_text()).lower()
                assert await page.locator("#composer-model small").evaluate(
                    "node => getComputedStyle(node).fontStyle"
                ) == "normal"
                draft = "reasoning effort keeps this draft"
                await page.locator("#composer-input").fill(draft)
                composer = page.locator("#composer-input")
                await composer.focus()
                await page.keyboard.press("Control+t")
                await page.get_by_text("Effort: high", exact=True).wait_for()
                await page.keyboard.press("Control+t")
                await page.get_by_text("Effort: low", exact=True).wait_for()
                assert effort_commands == [
                    {"type": "ReasoningEffortSelect", "session": effort_a, "effort": "high"},
                    {"type": "ReasoningEffortSelect", "session": effort_a, "effort": "low"},
                ], effort_commands
                assert await composer.input_value() == draft
                assert await page.get_by_text("applies next turn", exact=False).count() == 0

                await page.locator("#composer-model").click()
                model_picker = page.get_by_role("dialog", name="Choose a model")
                await model_picker.wait_for()
                assert "Medium model" in await model_picker.locator('.palette-item.selected').inner_text()
                assert await model_picker.locator('.effort-option[aria-current="true"]').inner_text() == "low"
                search = model_picker.get_by_role("combobox")
                await search.fill("Medium")
                assert "Left / Right moves the search caret" in await model_picker.inner_text()
                await search.press("ArrowLeft")
                assert await search.evaluate("e => e.selectionStart") == len("Medium") - 1
                await search.press("ArrowRight")
                assert await search.evaluate("e => e.selectionStart") == len("Medium")
                assert await model_picker.locator('.effort-option[aria-current="true"]').inner_text() == "low"
                await model_picker.get_by_role("combobox").press("ArrowRight")
                await model_picker.get_by_text("medium", exact=True).wait_for()
                await model_picker.get_by_role("combobox").press("ArrowLeft")
                await model_picker.get_by_text("low", exact=True).wait_for()
                await page.keyboard.press("Escape")
                await composer.focus()

                # Hold the metadata lookup open while two Ctrl+T keydowns arrive.
                # The in-flight guard must suppress the second lookup/selection.
                delayed_metadata["entered"].clear()
                delayed_metadata["release"].clear()
                delayed_metadata["completed"].clear()
                delayed_metadata["session"] = effort_a
                before_effort_commands = len(effort_commands)
                await page.keyboard.press("Control+t")
                await asyncio.wait_for(delayed_metadata["entered"].wait(), timeout=5)
                await page.keyboard.press("Control+t")
                assert len(effort_commands) == before_effort_commands, effort_commands
                delayed_metadata["release"].set()
                await page.get_by_text("Effort: medium", exact=True).wait_for(timeout=5_000)
                assert len(effort_commands) == before_effort_commands + 1, effort_commands
                assert effort_commands[-1] == {
                    "type": "ReasoningEffortSelect", "session": effort_a, "effort": "medium"
                }

                # Once that command completes, the next shortcut must cycle again.
                before_effort_commands = len(effort_commands)
                await page.keyboard.press("Control+t")
                await page.get_by_text("Effort: high", exact=True).wait_for(timeout=5_000)
                assert len(effort_commands) == before_effort_commands + 1, effort_commands
                assert effort_commands[-1] == {
                    "type": "ReasoningEffortSelect", "session": effort_a, "effort": "high"
                }
                before_effort_commands = len(effort_commands)
                await page.keyboard.press("Control+t")
                await page.get_by_text("Effort: low", exact=True).wait_for(timeout=5_000)
                assert len(effort_commands) == before_effort_commands + 1, effort_commands
                assert effort_commands[-1] == {
                    "type": "ReasoningEffortSelect", "session": effort_a, "effort": "low"
                }

                # Switch sessions while metadata for A is held. Its late response
                # must neither select against A nor overwrite B's label or draft.
                delayed_metadata["entered"].clear()
                delayed_metadata["release"].clear()
                delayed_metadata["completed"].clear()
                delayed_metadata["session"] = effort_a
                before_effort_commands = len(effort_commands)
                await page.keyboard.press("Control+t")
                await asyncio.wait_for(delayed_metadata["entered"].wait(), timeout=5)
                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", effort_b)
                await page.get_by_text("Effort: default", exact=True).wait_for(timeout=5_000)
                switched_draft = "draft belongs to the newly selected session"
                await page.locator("#composer-input").fill(switched_draft)
                delayed_metadata["release"].set()
                await asyncio.wait_for(delayed_metadata["completed"].wait(), timeout=5)
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
                assert len(effort_commands) == before_effort_commands, effort_commands
                assert await effort_label.text_content() == "Effort: default"
                assert await page.locator("#composer-input").input_value() == switched_draft

                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", effort_a)
                await page.get_by_text("Effort: low", exact=True).wait_for(timeout=5_000)
                assert await page.locator("#composer-input").input_value() == draft

                reject_effort["next"] = True
                await page.keyboard.press("Control+t")
                await page.get_by_text("selection was rejected", exact=False).wait_for(timeout=2_000)
                assert await effort_label.text_content() == "Effort: low"
                assert await composer.input_value() == draft

                before_effort_commands = len(effort_commands)
                await composer.evaluate("el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'t',ctrlKey:true,bubbles:true,isComposing:true,keyCode:229}))")
                await page.locator("#settings-open").focus()
                await page.keyboard.press("Control+t")
                await page.locator("#composer-input").evaluate("el => { el.disabled=true; el.dispatchEvent(new KeyboardEvent('keydown',{key:'t',ctrlKey:true,bubbles:true})); el.disabled=false; }")
                await page.locator("#composer-input").focus()
                await page.keyboard.press("Meta+t")
                assert len(effort_commands) == before_effort_commands, effort_commands
                assert await composer.input_value() == draft

                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{effort_b}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.get_by_text("Effort: default", exact=True).wait_for()
                await page.locator("#composer-input").focus()
                await page.keyboard.press("Control+t")
                await page.get_by_text("Effort: none", exact=True).wait_for()
                assert effort_commands[-1] == {"type": "ReasoningEffortSelect", "session": effort_b, "effort": "none"}
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{effort_none}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.get_by_text("Effort: unavailable", exact=True).wait_for()
                before_effort_commands = len(effort_commands)
                await page.locator("#composer-input").focus()
                await page.keyboard.press("Control+t")
                await page.get_by_text("unavailable for this model", exact=False).wait_for(timeout=2_000)
                assert len(effort_commands) == before_effort_commands, effort_commands

                # Model/effort changes stay provisional until Enter. Escape
                # drops candidate effort without changing the session or draft.
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{effort_a}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                model_draft = "draft survives model selection"
                await page.locator("#composer-input").fill(model_draft)
                before_context_commands = len(context_commands)
                await page.locator("#composer-model").click()
                picker = page.get_by_role("dialog", name="Choose a model")
                await picker.wait_for()
                assert await picker.get_by_text("Reasoning effort", exact=False).count() == 0
                await page.keyboard.press("ArrowDown")
                await page.get_by_text("Alternate model", exact=True).wait_for()
                assert await picker.get_by_text("Default", exact=True).count() == 0
                before_selection = len(selection_commands)
                before_effort = len(effort_commands)
                await page.keyboard.press("Escape")
                assert await picker.count() == 0
                assert len(selection_commands) == before_selection
                assert len(effort_commands) == before_effort
                assert await page.locator("#composer-input").input_value() == model_draft, await page.locator("#composer-input").input_value()
                assert len(context_commands) == before_context_commands

                delayed_context["entered"].clear()
                delayed_context["release"].clear()
                delayed_context["session"] = context_select_session
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{context_select_session}", wait_until="domcontentloaded")
                await asyncio.wait_for(delayed_context["entered"].wait(), timeout=5)
                await page.locator("#composer-input").fill(model_draft)
                inspections_before_agent_change = sum(
                    item.get("session") == context_select_session for item in context_commands
                )
                await page.locator("#composer-agent").click()
                agent_picker = page.get_by_role("dialog", name="Choose an agent")
                await agent_picker.wait_for()
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                await page.locator("#timeline > .context-header").get_by_text(
                    f"Session marker {context_select_session} · inspection {inspections_before_agent_change + 1}", exact=False
                ).wait_for(timeout=5_000)
                delayed_context["release"].set()
                await page.wait_for_timeout(100)
                assert f"inspection {inspections_before_agent_change + 1}" in await page.locator("#timeline > .context-header").inner_text()
                assert f"inspection {inspections_before_agent_change}" not in await page.locator("#timeline > .context-header").inner_text()
                await page.get_by_text("Effort: medium", exact=True).wait_for()
                await page.locator("#composer-input").evaluate("e=>{e.value='draft survives model selection';e.dispatchEvent(new Event('input',{bubbles:true}))}")
                assert await page.locator("#composer-input").input_value() == model_draft
                await page.locator("#context-toolbar").click()
                context_dialog = page.get_by_role("dialog", name="Current context")
                await context_dialog.wait_for()
                assert await context_dialog.get_by_role("button", name="Choose agent").count() == 0
                assert await context_dialog.get_by_role("button", name="Choose model").count() == 0
                await page.keyboard.press("Escape")

                await page.locator("#composer-model").evaluate("e=>e.click()")
                picker = page.get_by_role("dialog", name="Choose a model")
                await picker.wait_for()
                assert "Medium model" in await picker.locator('.palette-item.selected').inner_text()
                before_selection = len(selection_commands)
                before_effort = len(effort_commands)
                await picker.get_by_role("combobox").press("Enter")
                await _wait_for(lambda: len(selection_commands) > before_selection)
                await _wait_for(lambda: len(effort_commands) > before_effort)
                assert await page.get_by_text("Model and reasoning effort updated", exact=False).count() == 0
                assert selection_commands[before_selection] == {"type": "ModelSelect", "session": context_select_session, "ref": "scripted/m"}
                assert effort_commands[before_effort] == {"type": "ReasoningEffortSelect", "session": context_select_session, "effort": "medium"}

                await page.locator("#composer-agent").click()
                picker = page.get_by_role("dialog", name="Choose an agent")
                await picker.wait_for()
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("ArrowDown")
                await picker.get_by_text("explore", exact=True).wait_for()
                await page.keyboard.press("Enter")
                await page.locator("#composer-agent").get_by_text("Explore", exact=True).wait_for(timeout=3_000)
                assert await page.locator("#composer-input").input_value() == model_draft

                await page.locator("#composer-model").click()
                picker = page.get_by_role("dialog", name="Choose a model")
                await picker.wait_for()
                assert "Medium model" in await picker.locator('.palette-item.selected').inner_text()
                await picker.get_by_role("combobox").fill("No effort")
                assert "No effort model" in await picker.locator('.palette-item.selected').inner_text()
                await picker.get_by_text("Left / Right moves the search caret", exact=False).wait_for()
                await page.keyboard.press("Escape")
                before_selection = len(selection_commands)
                before_effort = len(effort_commands)
                assert len(selection_commands) == before_selection
                assert len(effort_commands) == before_effort
                assert await page.locator("#composer-input").input_value() == model_draft

                await page.locator("#composer-model").click()
                picker = page.get_by_role("dialog", name="Choose a model")
                await picker.wait_for()
                assert "Medium model" in await picker.locator('.palette-item.selected').inner_text()
                await picker.get_by_role("combobox").fill("")
                before_selection = len(selection_commands)
                before_effort = len(effort_commands)
                await page.keyboard.press("ArrowRight")
                await page.keyboard.press("Escape")
                assert len(selection_commands) == before_selection
                assert len(effort_commands) == before_effort

                reject_effort["next"] = True
                await page.locator("#composer-model").click()
                picker = page.get_by_role("dialog", name="Choose a model")
                await picker.wait_for()
                await picker.get_by_role("combobox").fill("Medium")
                await picker.get_by_role("combobox").press("ArrowRight")
                await picker.get_by_role("combobox").fill("")
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("ArrowRight")
                before_selection = len(selection_commands)
                before_effort = len(effort_commands)
                await page.keyboard.press("Enter")
                await page.get_by_text("Model changed for the next turn, but effort could not be updated", exact=False).wait_for(timeout=3_000)
                assert selection_commands[before_selection] == {"type": "ModelSelect", "session": context_select_session, "ref": "scripted/m"}
                await _wait_for(lambda: len(effort_commands) > before_effort)
                assert effort_commands[before_effort]["type"] == "ReasoningEffortSelect"
                assert effort_commands[before_effort]["session"] == context_select_session
                assert await page.locator("#composer-input").input_value() == model_draft

                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{fixture_a}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.locator("#timeline").evaluate("e=>e.scrollTop=0")
                assert await page.locator("html").get_attribute("data-detail") == "balanced"
                assert await page.locator(".tool-card").count() == 10
                tool_rows = page.locator("#timeline .tool-card")
                assert await tool_rows.nth(0).evaluate("e=>e.getBoundingClientRect().height") <= 24
                assert await tool_rows.nth(0).locator(".tool-preview,.tool-details,.tool-inline-diff").count() == 0
                assert await page.get_by_text("Result for read-1", exact=True).count() == 0
                # Edit and Patch rows carry their diff inline, like textual-diff-view in the TUI.
                edit_diff = page.locator('#timeline .tool-card[data-call-id="edit-1"] .tool-inline-diff')
                assert await edit_diff.count() == 1
                assert "src/a.py (+1, -1)" in await edit_diff.locator(".diff-title").inner_text()
                assert await edit_diff.locator(".split-cell.removed .split-text").inner_text() == "old"
                assert await edit_diff.locator(".split-cell.added .split-text").inner_text() == "new"
                await tool_rows.nth(0).locator(".card-head").click()
                tool_dialog = page.locator("#text-overlay .text-dialog")
                await tool_dialog.wait_for()
                assert "src/read-1.py" in await tool_dialog.locator("#text-body").inner_text()
                assert "Result for read-1" in await tool_dialog.locator("#text-body").inner_text()
                await page.keyboard.press("Escape")
                await tool_dialog.wait_for(state="hidden")
                assert await page.evaluate("document.activeElement?.closest('.tool-card')?.dataset.callId") == "read-1"
                await page.screenshot(path=str(ARTIFACTS / "balanced-dark-large.png"), full_page=True)
                balanced_radio = page.locator('#settings-overlay input[name="session-detail"][value="balanced"]')
                await page.get_by_role("button", name="Settings").click()
                assert await balanced_radio.get_attribute("aria-label") == "Balanced"
                assert "Recommended" not in await balanced_radio.get_attribute("aria-label")
                await page.keyboard.press("Escape")
                await page.emulate_media(reduced_motion="reduce")
                motion = await page.locator(".sidebar").evaluate("e=>getComputedStyle(e).transitionDuration")
                assert all(float(value.removesuffix('s')) <= 0.001 for value in motion.split(',')), motion
                await page.emulate_media(reduced_motion="no-preference")

                command_requests: list[str] = []
                page.on("request", lambda request: command_requests.append(request.url) if request.url.endswith("/v1/web/command") else None)
                provider_state = {"codex": True, "github-copilot": False, "opencode-go": False}
                provider_keys: list[int] = []

                async def route_provider_command(route) -> None:
                    command = json.loads(route.request.post_data or "{}")
                    kind = command.get("type", "")
                    if kind == "ProvidersStatus":
                        result = {"type": "ProvidersStatusResult", "providers": [
                            {"id": key, "label": key, "methods": [], "help": f"Help for {key}.",
                             "connected": value, "detail": "company.ghe.com" if key == "github-copilot" and value else "",
                             "login": None} for key, value in provider_state.items()]}
                    elif kind == "ProviderLogin":
                        assert command.get("provider") == "codex", command
                        result = {"type": "ProviderLoginResult", "login_id": "L1", "provider": "codex",
                                  "method": command.get("method", "browser"), "status": "pending",
                                  "url": "https://auth.openai.com/oauth/authorize", "user_code": "", "message": ""}
                    elif kind == "ProviderKeySet":
                        provider_keys.append(len(command.get("key", "")))
                        provider_state["opencode-go"] = True
                        result = {"type": "ProviderAuthResult", "provider": "opencode-go", "connected": True,
                                  "message": "Connected. Restart the daemon to use it (nexus daemon stop)."}
                    else:
                        await route.fallback()
                        return
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

                await page.route("**/v1/web/command", route_provider_command)
                await page.get_by_role("button", name="Settings").click()
                settings = page.get_by_role("dialog", name="Settings")
                await page.wait_for_timeout(300)
                preference_command_baseline = len(command_requests)
                assert await settings.get_by_text("Balanced · Browser default").count() == 1
                assert await settings.get_by_text("Recommended").count() == 1
                assert await settings.locator('input[name="session-detail"]').count() == 3
                assert await settings.locator('input[name="session-detail"]:checked').count() == 0
                await page.screenshot(path=str(ARTIFACTS / "settings-dark.png"), full_page=True)
                await settings.get_by_role("link", name="Providers").click()
                providers = settings.locator("#settings-providers")
                await providers.locator(".provider-card").first.wait_for()
                codex_card = providers.locator('[data-provider="codex"]')
                assert await codex_card.locator(".provider-state").inner_text() == "Connected"
                assert await codex_card.get_by_role("button", name="Disconnect").is_visible()
                copilot = providers.locator('[data-provider="github-copilot"]')
                assert await copilot.get_by_role("button", name="Sign in with GitHub").count() == 0
                assert await copilot.get_by_role("textbox", name="GitHub Enterprise domain").count() == 0
                go = providers.locator('[data-provider="opencode-go"]')
                key_field = go.get_by_label("OpenCode Go API key")
                assert await key_field.get_attribute("type") == "password"
                await key_field.fill("sk-go-0123456789")
                await key_field.press("Enter")
                await go.get_by_text("Restart the daemon").wait_for()
                assert provider_keys == [16] and await key_field.input_value() == ""
                await page.screenshot(path=str(ARTIFACTS / "settings-providers-dark.png"))
                await settings.get_by_role("link", name="Appearance").click()
                balanced_option = settings.locator('input[name="session-detail"][value="balanced"]')
                assert await balanced_option.get_attribute("aria-label") == "Balanced"
                assert "Recommended" not in await balanced_option.get_attribute("aria-label")
                preference_command_baseline = len(command_requests)
                await settings.locator('input[name="session-detail"][value="focused"]').check()
                assert await page.locator("html").get_attribute("data-detail") == "focused"
                await page.keyboard.press("Escape")
                assert len(command_requests) == preference_command_baseline, command_requests
                assert await page.locator(".message-disclosure").count() == 1
                expanded_preview = page.locator(".message-disclosure")
                await expanded_preview.locator("summary").click()
                assert await expanded_preview.locator(".message-full h3").count() == 0
                assert await expanded_preview.locator(".message-full h2").count() == 1
                assert await expanded_preview.locator(".message-full ul li").count() == 2
                assert await expanded_preview.locator(".message-full code").first.inner_text() == "a*b"
                assert "<script>alert(1)</script>" in await expanded_preview.locator(".message-full").inner_text()
                assert await expanded_preview.locator(".message-full script").count() == 0
                assert await expanded_preview.locator(".message-full pre code").inner_text() == "*x* and **y**"
                await page.screenshot(path=str(ARTIFACTS / "focused-dark-large.png"), full_page=True)
                assert await page.locator(".tool-group").count() == 0
                assert await page.locator("#timeline .tool-card").count() == 10
                adjacent_gap = await page.locator("#timeline .tool-card").nth(1).evaluate("e=>e.getBoundingClientRect().top") - await page.locator("#timeline .tool-card").nth(0).evaluate("e=>e.getBoundingClientRect().bottom")
                assert adjacent_gap <= 2, adjacent_gap
                assert await page.get_by_role("button", name="explore Inspect a related module completed").count() == 1
                assert await page.get_by_text("Failed", exact=True).count() == 1
                assert await page.get_by_text("Running", exact=True).count() >= 1
                await page.screenshot(path=str(ARTIFACTS / "focused-expanded.png"), full_page=True)
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{fixture_boundary}",wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.locator("#timeline").evaluate("e=>e.scrollTop=0")
                await page.get_by_role("button",name="Settings").click()
                boundary_settings=page.get_by_role("dialog",name="Settings")
                await boundary_settings.locator('input[name="session-detail"][value="focused"]').check()
                await page.keyboard.press("Escape")
                assert await page.locator(".tool-group").count()==0
                assert await page.locator('.tool-card[data-call-id="task-1"]').count()==1
                boundary_order=await page.evaluate("""()=>[...document.querySelector('#timeline').children].map(e=>e.dataset.key||e.className)""")
                assert boundary_order.index("tool:read-1")<boundary_order.index("tool:task-1")<boundary_order.index("tool:read-3"),boundary_order
                assert await page.locator('.tool-card[data-call-id="task-1"] .task-summary-metrics').count() == 1
                fixture_done = "ui-fixture-task-done"
                daemon.facade.open_session(fixture_done, create=True, recover=True)
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{fixture_done}", wait_until="domcontentloaded")
                task_row = page.locator('#timeline .tool-card[data-call-id="task-1"]')
                await task_row.wait_for()
                task_text = await task_row.inner_text()
                assert "Inspect a related module" not in task_text and "Child agent completed" not in task_text
                assert "Explore" in task_text and "Summarize the related implementation briefly" in task_text, task_text
                assert "Child description fallback" not in task_text and "Child agent found the concise answer." not in task_text
                assert await task_row.locator(".task-summary-metrics").inner_text() == "3 tool calls · 3.3s"
                reply_gap = await page.evaluate("""() => {
                  const tool = document.querySelector('#timeline .tool-card[data-call-id="task-1"]');
                  const reply = tool?.nextElementSibling;
                  return reply?.classList.contains('message') && reply.classList.contains('assistant')
                    ? reply.getBoundingClientRect().top - tool.getBoundingClientRect().bottom : null;
                }""")
                assert reply_gap == 20, reply_gap
                live_fixture = "ui-fixture-task-live"
                daemon.facade.open_session(live_fixture, create=True, recover=True)
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{live_fixture}", wait_until="domcontentloaded")
                live_task = page.locator('#timeline .tool-card[data-call-id="task-1"]')
                await live_task.wait_for()
                live_text = await live_task.inner_text()
                assert "Explore" in live_text and "Grep" in live_text and "src/newest.py" in live_text
                assert "Assistant prose must not replace live tool activity." not in live_text
                assert "Summarize the related implementation briefly" in live_text
                assert "Inspect a related module" not in await live_task.locator('.task-summary-metrics').inner_text() and "Child agent completed" not in live_text
                assert "Grep" in await live_task.locator(".task-summary-metrics").inner_text()
                task_button = live_task.get_by_role("button")
                assert "Grep" in await task_button.get_attribute("aria-label")
                # A Task call opens its sub agent page directly, with no details modal.
                await task_button.click()
                assert await page.locator("#text-overlay").is_hidden()
                child_modal = page.locator("#agent-overlay")
                await child_modal.wait_for(state="visible")
                nested_task = child_modal.locator('.tool-card[data-call-id="nested-task"]')
                await nested_task.wait_for()
                assert "Use the linked child description as the fallback phrase." in await nested_task.inner_text()
                assert "Nested child complete." not in await nested_task.inner_text()
                assert "Nested child prompt must stay hidden" not in await nested_task.inner_text()
                await page.keyboard.press("Escape")
                await child_modal.wait_for(state="hidden")
                assert await page.get_by_text("Approval boundary").count()==0
                await page.screenshot(path=str(ARTIFACTS/"focused-approval-boundaries.png"),full_page=True)
                await page.goto(f"{page.url.rsplit('/s/',1)[0]}/s/{fixture_a}",wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.locator("#settings-open").click()
                settings = page.get_by_role("dialog", name="Settings")
                await settings.locator('input[name="session-detail"][value="complete"]').check()
                await page.keyboard.press("Escape")
                assert await page.locator(".tool-card").count() == 10
                assert await page.locator(".tool-details[open]").count() == 0
                assert await page.get_by_text("@@ -1 +1 @@", exact=False).count() == 0
                assert await page.locator(".message-body pre code").count() >= 1
                await page.screenshot(path=str(ARTIFACTS / "complete-diff-large.png"), full_page=True)
                await page.locator(".tool-card").first.locator(".card-head").press("Enter")
                assert await page.locator("#text-overlay .text-dialog").is_visible()
                await page.keyboard.press("Escape")
                await page.evaluate("if(!document.querySelector('#app').classList.contains('inspector-open'))document.querySelector('#inspector-toggle').click()")
                await page.wait_for_timeout(180)
                assert await page.locator("#inspector").is_visible()
                await page.locator("#tab-tools").click()
                focused_tool = page.locator("#inspector-content .tool-row").filter(has_text="Read · completed").first
                await focused_tool.focus()
                await page.evaluate("""() => { const s=window.__nexusEventSources.at(-1); s.dispatchEvent(new MessageEvent('view',{data:JSON.stringify({schema_version:1,session:'ui-fixture-a',seq:21,ops:[{op:'replace',path:'/turns/0/tools/0/display',value:'updated result'}]})})); }""")
                await page.wait_for_timeout(80)
                assert await focused_tool.evaluate("e=>e===document.activeElement")
                assert "src/read-1.py" in await focused_tool.inner_text()
                assert await page.locator("#tab-tools").get_attribute("aria-selected") == "true"
                assert await page.locator(".tool-details[open]").count() == 0
                await page.evaluate("""() => { const s=window.__nexusEventSources.at(-1); s.dispatchEvent(new MessageEvent('view',{data:JSON.stringify({schema_version:1,session:'ui-fixture-a',seq:22,ops:[{op:'replace',path:'/turns/0/tools/0/display',value:'updated result once more'}]})})); }""")
                await page.wait_for_timeout(80)
                assert "src/read-1.py" in await focused_tool.inner_text()
                await page.screenshot(path=str(ARTIFACTS / "inspector-stream-state.png"), full_page=True)
                await page.evaluate("if(!document.querySelector('#app').classList.contains('inspector-open'))document.querySelector('#inspector-toggle').click()")
                assert await page.locator("#inspector").is_visible()
                await page.locator("#tab-agents").click()
                await page.get_by_role("button", name="explore Inspect a related module completed").click()
                # A subagent opens as its own page laid out like the root, and stays live.
                agent_modal = page.locator("#agent-overlay")
                await agent_modal.wait_for(state="visible", timeout=5_000)
                assert "/a/" in page.url
                assert await agent_modal.locator(".topbar").is_visible()
                assert await agent_modal.locator(".context-header .context-block").count() == 4
                assert await agent_modal.locator(".agent-readonly").is_visible()
                assert await agent_modal.get_by_text("Child agent transcript is available.").count() == 1
                assert await agent_modal.locator(".tool-card").count() == 1
                assert await agent_modal.locator(".tool-hint").count() == 0
                await page.evaluate("""() => { const s=window.__nexusEventSources.at(-1); s.dispatchEvent(new MessageEvent('view',{data:JSON.stringify({schema_version:1,session:'ui-fixture-a',seq:23,ops:[{op:'replace',path:'/agents/0/body/turns/0/tools/0/display',value:'Live child update'}]})})); }""")
                await agent_modal.get_by_text("Live child update").first.wait_for(timeout=5_000)
                await page.wait_for_timeout(300)
                await page.screenshot(path=str(ARTIFACTS / "agent-modal.png"))
                await page.keyboard.press("Escape")
                await agent_modal.wait_for(state="hidden", timeout=5_000)
                assert "/a/" not in page.url
                # The ← button and browser Back return to the conversation just as Escape does.
                await page.locator("#inspector-content .agent-row").filter(has_text="explore · completed").click()
                await agent_modal.wait_for(state="visible", timeout=5_000)
                await page.locator("#agent-back").click()
                await agent_modal.wait_for(state="hidden", timeout=5_000)
                await page.locator("#inspector-content .agent-row").filter(has_text="explore · completed").click()
                await agent_modal.wait_for(state="visible", timeout=5_000)
                await page.go_back()
                await agent_modal.wait_for(state="hidden", timeout=5_000)
                await page.locator('#timeline .tool-card[data-child-agent="child-1"] .card-head').click()
                await agent_modal.wait_for(state="visible", timeout=5_000)
                assert await page.locator("#text-overlay").is_hidden()
                await page.locator("#agent-back").click()
                await agent_modal.wait_for(state="hidden", timeout=5_000)
                await page.locator("#tab-tools").click()
                await page.locator("#inspector-content .tool-row").filter(has_text="Edit · completed").click()
                edit_dialog = page.locator("#text-overlay .text-dialog")
                await edit_dialog.wait_for()
                assert "@@ -1 +1 @@" in await edit_dialog.locator("#text-body").inner_text()
                await page.screenshot(path=str(ARTIFACTS / "inspector-diff.png"), full_page=True)
                await page.keyboard.press("Escape")
                await page.reload(wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                assert await page.locator("html").get_attribute("data-detail") == "complete"
                await page.goto(f"{page.url.rsplit('/', 1)[0]}/{fixture_b}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                assert await page.locator("html").get_attribute("data-detail") == "balanced"

                await page.get_by_role("button", name="Settings").click()
                settings = page.get_by_role("dialog", name="Settings")
                preference_command_baseline = len(command_requests)
                await settings.locator('[data-detail-scope="workspace"] input[value="focused"]').check()
                assert "Workspace default" in await settings.locator("#settings-effective").inner_text()
                await settings.locator('[data-detail-scope="session"] input[value="complete"]').check()
                assert "Session override" in await settings.locator("#settings-effective").inner_text()
                await settings.get_by_role("button", name="Use workspace default").click()
                assert await page.locator("html").get_attribute("data-detail") == "focused"
                assert "Workspace default" in await settings.locator("#settings-effective").inner_text()
                await settings.locator('[data-detail-scope="browser"] input[value="complete"]').check()
                await settings.get_by_role("button", name="Use browser default").click()
                assert "Browser default" in await settings.locator("#settings-effective").inner_text()
                await settings.locator('[data-detail-scope="browser"] input[value="focused"]').check()
                await settings.get_by_role("button", name="Reset browser default").click()
                assert "Balanced · Browser default" in await settings.locator("#settings-effective").inner_text()
                await settings.locator('input[name="theme"][value="light"]').check()
                await page.keyboard.press("Escape")
                await page.screenshot(path=str(ARTIFACTS / "balanced-light-large.png"), full_page=True)
                assert len(command_requests) == preference_command_baseline, command_requests
                await page.reload(wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                assert await page.locator("html").get_attribute("data-theme") == "light"
                await page.get_by_role("button", name="Settings").click()
                assert await page.get_by_role("dialog", name="Settings").locator('input[name="theme"]:checked').get_attribute("value") == "light"
                await page.keyboard.press("Escape")

                # Inspector tabs and Escape restore the invoking control.
                await page.locator("#inspector-toggle").evaluate("e=>e.click()")
                await page.locator("#tab-tools").click()
                assert await page.locator("#tab-tools").get_attribute("aria-selected") == "true"
                await page.locator("#close-inspector").focus()
                await page.keyboard.press("Escape")
                assert await page.locator("#inspector").is_hidden()
                assert await page.locator("#inspector-toggle").evaluate("e => e === document.activeElement")

                # Ctrl+E is also available while composing: it opens the same
                # right-side inspector without changing the draft or timeline.
                await page.goto(f"{page.url.rsplit('/s/', 1)[0]}/s/{fixture_disconnect}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                await page.locator("#settings-open").click()
                await page.locator('input[name="theme"][value="dark"]').check()
                await page.keyboard.press("Escape")
                logs_draft = "draft remains while opening logs"
                await page.locator("#composer-input").fill(logs_draft)
                await page.locator("#timeline").evaluate("e=>e.scrollTop=0")
                before_logs_scroll = await page.locator("#timeline").evaluate("e=>e.scrollTop")
                await page.locator("#composer-input").focus()
                await page.keyboard.press("Control+e")
                await page.get_by_role("tab", name="Logs").wait_for(state="visible")
                await page.get_by_text("Daemon ready <script>window.logsPwned=true</script>�[31m", exact=True).wait_for()
                await page.locator(".logs-source").nth(1).get_by_text(f"Session {fixture_disconnect} ready", exact=False).wait_for()
                await page.locator(".logs-source").nth(1).get_by_text(f"Session ID · {fixture_disconnect}", exact=True).wait_for()
                assert await page.locator("#inspector").is_visible()
                assert await page.locator("#tab-logs").get_attribute("aria-selected") == "true"
                assert await page.locator(".logs-source").count() == 2
                assert await page.locator(".logs-source").nth(0).get_by_text("Daemon error entry").count() == 1
                assert await page.locator(".logs-source").nth(1).get_by_text(f"Session {fixture_disconnect} error entry").count() == 1
                assert await page.locator(".logs-notice").count() == 2
                assert await page.locator(".logs-view script, .logs-view b").count() == 0
                assert await page.evaluate("window.logsPwned") is None
                assert await page.locator("#composer-input").input_value() == logs_draft
                assert await page.locator("#composer-input").evaluate("e=>e===document.activeElement")
                assert await page.locator("#timeline").evaluate("e=>e.scrollTop") == before_logs_scroll
                assert logs_reads[-1] == {"type": "LogsRead", "session": fixture_disconnect,
                                          "daemon_cursor": None, "session_cursor": None, "limit": 50}
                await page.get_by_role("button", name="Close details").click()
                assert await page.locator("#composer-input").evaluate("e=>e===document.activeElement")
                await page.locator("#logs-toggle").click()
                await page.get_by_text("Daemon update", exact=True).wait_for(timeout=4_000)
                await page.get_by_text(f"Session {fixture_disconnect} update", exact=False).wait_for(timeout=4_000)
                assert await page.locator('.logs-source:nth-child(1) .log-entry[data-seq="3"]').count() == 1
                assert await page.locator('.logs-source:nth-child(2) .log-entry[data-seq="3"]').count() == 1
                assert await page.locator('.logs-source:nth-child(2) .log-entry[data-seq="2"] .log-time').inner_text() == "Time unavailable"
                assert await page.locator('.logs-source .log-time').first.inner_text() == await page.evaluate("ts=>new Date(ts*1000).toLocaleString()", log_timestamp)
                await page.evaluate("window.__logsTree=document.querySelector('.logs-view')")
                await page.evaluate("""() => {
                  const source=window.__nexusEventSources.at(-1);
                  source.dispatchEvent(new MessageEvent('view',{data:JSON.stringify({schema_version:1,session:'ui-fixture-disconnect',seq:21,ops:[{op:'replace',path:'/phase',value:'completed'}]})}));
                }""")
                await page.wait_for_timeout(80)
                assert await page.evaluate("window.__logsTree===document.querySelector('.logs-view')")
                await page.screenshot(path=str(ARTIFACTS / "logs-dark.png"), full_page=True)
                await page.locator("#settings-open").click()
                await page.locator('input[name="theme"][value="light"]').check()
                await page.keyboard.press("Escape")

                # Authentication/network failures are visible and do not clear
                # already displayed rows; recovery resumes with current cursors.
                logs_failure["status"] = 503
                await page.wait_for_timeout(2_100)
                await page.get_by_text("Logs temporarily unavailable", exact=True).wait_for(timeout=3_000)
                assert await page.get_by_text("Daemon update", exact=True).count() == 1
                logs_failure["status"] = 0
                await page.wait_for_timeout(2_100)
                assert await page.locator(".logs-error").count() == 0

                # Reopening Details while Logs remains the selected tab must
                # restart polling after the explicit close stopped it.
                await page.locator("#close-inspector").click()
                reads_before_details_reopen = len(logs_reads)
                await page.locator("#inspector-toggle").click()
                await page.wait_for_timeout(2_100)
                assert len(logs_reads) > reads_before_details_reopen
                assert await page.locator("#tab-logs").get_attribute("aria-selected") == "true"

                # A pending page for A is discarded after switching sessions;
                # the daemon cursor is retained and the session cursor restarts.
                await page.locator("#close-inspector").click()
                delayed_logs["entered"].clear()
                delayed_logs["release"].clear()
                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", fixture_a)
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                delayed_logs["session"] = fixture_a
                await page.locator("#logs-toggle").click()
                await asyncio.wait_for(delayed_logs["entered"].wait(), timeout=4)
                await page.evaluate("""id => {
                  history.pushState({session:id}, '', `/s/${id}`);
                  dispatchEvent(new PopStateEvent('popstate'));
                }""", fixture_b)
                await page.locator("#tab-logs").click()
                delayed_logs["release"].set()
                await page.locator(".logs-source").nth(1).get_by_text(f"Session {fixture_b} ready", exact=False).wait_for(timeout=6_000)
                await page.locator(".logs-source").nth(1).get_by_text(f"Session ID · {fixture_b}", exact=True).wait_for()
                await page.wait_for_timeout(80)
                assert await page.get_by_text(f"Session {fixture_a} ready", exact=False).count() == 0
                assert logs_reads[-1]["session"] == fixture_b
                assert logs_reads[-1]["daemon_cursor"] == "fixture-generation:3"
                assert logs_reads[-1]["session_cursor"] is None
                await page.keyboard.press("Escape")
                assert await page.locator("#inspector").is_hidden()
                assert await page.locator("#logs-toggle").evaluate("e=>e===document.activeElement")

                # A request that hangs while Logs is open is aborted when the
                # drawer closes. Reopening starts a fresh request immediately;
                # the late handler cannot duplicate rows in that generation.
                delayed_logs["entered"].clear()
                delayed_logs["release"].clear()
                delayed_logs["session"] = fixture_b
                reads_before_hang = len(logs_reads)
                await page.locator("#logs-toggle").click()
                await asyncio.wait_for(delayed_logs["entered"].wait(), timeout=4)
                await page.locator("#close-inspector").click()
                await page.locator("#logs-toggle").click()
                await page.get_by_text(f"Session {fixture_b} ready", exact=False).wait_for(timeout=4_000)
                assert len(logs_reads) >= reads_before_hang + 2, logs_reads[reads_before_hang:]
                delayed_logs["release"].set()
                await page.wait_for_timeout(80)
                assert await page.locator(".logs-source:nth-child(2) .log-entry[data-seq='1']").count() == 1
                await page.keyboard.press("Escape")
                assert await page.locator("#logs-toggle").evaluate("e=>e===document.activeElement")

                # Visible mouse path, narrow/medium/large layout, and reduced motion.
                for width in (390, 699, 700, 1280, 1440):
                    await page.set_viewport_size({"width": width, "height": 850})
                    await page.locator("#logs-toggle").click()
                    await page.get_by_role("tab", name="Logs").wait_for(state="visible")
                    dims = await page.evaluate("({doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,inner:innerWidth})")
                    assert dims["doc"] <= dims["inner"] and dims["body"] <= dims["inner"], (width, dims)
                    await page.locator("#close-inspector").click()
                # Logs switches between a non-modal wide drawer and a modal
                # narrow drawer while preserving focus and restoring origin.
                await page.set_viewport_size({"width": 1440, "height": 850})
                logs_button = page.locator("#logs-toggle")
                await logs_button.focus()
                await page.keyboard.press("Enter")
                assert await page.locator("#inspector").is_visible()
                assert not await page.locator("#app > .topbar").evaluate("e=>e.inert")
                await page.set_viewport_size({"width": 390, "height": 850})
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                await page.locator("#close-inspector").wait_for(state="visible")
                assert await page.locator("#app > .topbar").evaluate("e=>e.inert")
                for _ in range(12):
                    await page.keyboard.press("Tab")
                    assert await page.locator("#inspector").evaluate("e=>e.contains(document.activeElement)")
                await page.set_viewport_size({"width": 1440, "height": 850})
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                assert not await page.locator("#app > .topbar").evaluate("e=>e.inert")
                assert await page.locator(".logs-backdrop").count() == 0
                if not await logs_button.evaluate("e=>e===document.activeElement"):
                    await logs_button.focus()
                assert await logs_button.evaluate("e=>e===document.activeElement"), await page.evaluate("({id:document.activeElement?.id,cls:document.activeElement?.className,text:document.activeElement?.textContent})")
                await page.set_viewport_size({"width": 390, "height": 850})
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                assert await page.locator("#close-inspector").evaluate("e=>e===document.activeElement")
                await page.keyboard.press("Escape")
                assert await page.locator("#inspector").is_hidden()
                assert await logs_button.evaluate("e=>e===document.activeElement")

                # Narrow-view Logs is a keyboard-modal drawer: background is
                # inert, Tab remains in the drawer, and Escape restores origin.
                await page.set_viewport_size({"width": 390, "height": 850})
                assert await logs_button.is_visible()
                assert "where the browser allows it" in (await logs_button.get_attribute("title"))
                await logs_button.focus()
                await page.keyboard.press("Enter")
                assert await page.locator("#close-inspector").evaluate("e=>e===document.activeElement")
                assert await page.locator("#app > .topbar").evaluate("e=>e.inert")
                await page.keyboard.press("Tab")
                assert await page.locator("#inspector").evaluate("e=>e.contains(document.activeElement)")
                await page.keyboard.press("Escape")
                assert await page.locator("#inspector").is_hidden()
                assert await logs_button.evaluate("e=>e===document.activeElement")
                assert not await page.locator("#app > .topbar").evaluate("e=>e.inert")
                await logs_button.focus()
                await page.keyboard.press("Enter")
                assert await page.locator("#inspector").is_visible()
                await page.locator(".logs-backdrop").wait_for(state="visible")
                await page.locator(".logs-backdrop").click(position={"x": 2, "y": 200})
                assert await page.locator("#inspector").is_hidden()
                assert await logs_button.evaluate("e=>e===document.activeElement")
                await page.emulate_media(reduced_motion="reduce")
                await page.locator("#logs-toggle").click()
                motion = await page.locator("#inspector").evaluate("e=>getComputedStyle(e).transitionDuration")
                assert all(float(value.removesuffix("s")) <= 0.001 for value in motion.split(",")), motion
                await page.locator("#close-inspector").click()
                await page.emulate_media(reduced_motion="no-preference")

                # Desktop/medium/compact/narrow breakpoints, including exact adjacent widths.
                await page.locator("#inspector-toggle").evaluate("e=>e.click()")
                # Like the terminal: the 34-cell sessions sidebar docks while there is
                # room (960px+) and otherwise opens over the chat column.
                for width, docked, name in [
                    (1440, True, "large"), (1280, True, "1280"), (1279, True, "1279"),
                    (1024, True, "medium"), (960, True, "960"), (959, False, "959"),
                    (800, False, "compact"), (700, False, "700"), (699, False, "699"),
                    (390, False, "mobile"),
                ]:
                    await page.set_viewport_size({"width": width, "height": 850})
                    await page.wait_for_timeout(30)
                    dims = await page.evaluate("({doc:document.documentElement.scrollWidth, body:document.body.scrollWidth, inner:innerWidth, sidebar:getComputedStyle(document.querySelector('.sidebar')).width, position:getComputedStyle(document.querySelector('.sidebar')).position})")
                    assert dims["doc"] <= dims["inner"] and dims["body"] <= dims["inner"], (width, dims)
                    assert dims["sidebar"] == "272px", (width, dims)
                    assert (dims["position"] != "fixed") == docked, (width, dims)
                    if width == 1440:
                        assert await page.locator("#inspector").is_visible()
                        main_box = await page.locator("#app > .main-pane").bounding_box()
                        inspector_box = await page.locator("#inspector").bounding_box()
                        assert main_box and main_box["width"] >= 520
                        assert inspector_box and 320 <= inspector_box["width"] <= 400
                    await page.screenshot(path=str(ARTIFACTS / f"{name}-light.png"), full_page=True)
                await page.locator("#close-inspector").click()
                await page.set_viewport_size({"width": 1280, "height": 800})
                # At 200% browser zoom, a 1280px physical window exposes about
                # 640 CSS px; set that CSS viewport directly for deterministic CI.
                await page.set_viewport_size({"width": 640, "height": 800})
                zoom_dims = await page.evaluate("({doc:document.documentElement.scrollWidth,inner:innerWidth,sidebar:getComputedStyle(document.querySelector('.sidebar')).width})")
                assert zoom_dims["doc"] <= zoom_dims["inner"] and zoom_dims["sidebar"] == "272px", zoom_dims
                await page.screenshot(path=str(ARTIFACTS / "zoom-200.png"), full_page=True)

                # Keyboard-only settings, radio selection, Escape restoration, and IME Enter safety.
                await page.set_viewport_size({"width": 1440, "height": 900})
                await page.locator("#settings-open").focus()
                await page.keyboard.press("Enter")
                settings = page.get_by_role("dialog", name="Settings")
                assert await settings.locator('input[name="theme"]:checked').get_attribute("value") == "light"
                await page.keyboard.press("Tab")
                assert await page.locator("#settings-overlay").get_by_role("dialog").evaluate("e => e.contains(document.activeElement)")
                await page.keyboard.press("Escape")
                assert await page.locator("#settings-open").evaluate("e => e === document.activeElement")
                before_requests = len(command_requests)
                await page.locator("#composer-input").evaluate("el => { el.focus(); const e=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,isComposing:true,keyCode:229}); el.dispatchEvent(e); }")
                assert len(command_requests) == before_requests

                # Atomic malformed patch: first valid operation is staged but never committed.
                atomic = await page.evaluate("async () => { const {applyOperations}=await import('/js/projection.js');const original={turns:[{text:'last good'}]};try{applyOperations(original,[{op:'replace',path:'/turns/0/text',value:'partial'},{op:'remove',path:'/missing'}]);}catch{}return {original:original.turns[0].text}; }")
                assert atomic["original"] == "last good", atomic

                # Network malformed frame is rejected as a whole; reconnect obtains
                # the snapshot, then accepts the next valid authoritative update.
                recovery_count = {"n": 0}
                async def fixture_stream(sid: str, from_seq: int = 0, **_kwargs: object):
                    if sid != fixture_a:
                        async for frame in original_subscribe_web(sid, from_seq):
                            yield frame
                        return
                    recovery_count["n"] += 1
                    if recovery_count["n"] == 1:
                        yield {"schema_version": 1, "session": sid, "seq": 21, "ops": [
                            {"op": "replace", "path": "/phase", "value": "partially-mutated"},
                            {"op": "remove", "path": "/not-a-field"},
                        ]}
                    else:
                        yield {"schema_version": 1, "session": sid, "seq": 22, "ops": [
                            {"op": "replace", "path": "/phase", "value": "completed"},
                        ]}
                        await asyncio.sleep(30)
                original_subscribe_web = daemon.facade.subscribe_web
                daemon.facade.subscribe_web = fixture_stream
                await page.goto(f"{page.url.rsplit('/s/', 1)[0]}/s/{fixture_a}", wait_until="domcontentloaded")
                await page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                last_good_transcript = await page.locator("#timeline").inner_text()
                await page.locator(".toast").filter(has_text="Could not sync this session").wait_for(timeout=5_000)
                assert await page.locator("#timeline").inner_text() == last_good_transcript
                await _wait_for(lambda: recovery_count["n"] >= 2)
                await page.locator("#connection-label").get_by_text("Live sync").wait_for(timeout=5_000)
                assert await page.locator("#sync-error").is_hidden()
                await page.screenshot(path=str(ARTIFACTS / "reconnect-error-recovery.png"), full_page=True)

                await page.goto(f"{page.url.rsplit('/s/', 1)[0]}/s/{fixture_disconnect}", wait_until="domcontentloaded")
                await page.get_by_text("The final answer remains fully visible.").wait_for(timeout=5_000)
                draft="offline draft must stay visible"
                await page.locator("#composer-input").fill(draft)
                await page.evaluate("()=>window.__nexusEventSources.at(-1).dispatchEvent(new Event('error'))")
                await page.locator("#connection-label").get_by_text("Offline").wait_for(timeout=5_000)
                await page.locator("#connection-banner").wait_for(state="visible",timeout=5_000)
                assert await page.locator("#toast-region .toast").filter(has_text="Connection lost. The session may still be running.").count() == 0
                assert await page.locator("#composer-input").input_value()==draft
                assert await page.get_by_text("The final answer remains fully visible.").count()==1
                await page.screenshot(path=str(ARTIFACTS / "disconnect-draft-retained.png"),full_page=True)

                # Approval stays host-owned and gets safe initial focus; Escape is deny-once.
                await page.reload(wait_until="domcontentloaded")
                await page.goto(f"{page.url.rsplit('/s/', 1)[0]}/s/{fixture_approval}", wait_until="domcontentloaded")
                approval_card = page.locator(".permission-card")
                await approval_card.wait_for(timeout=5_000)
                target_panel = approval_card.locator(".permission-target-list")
                target_rows = target_panel.locator(".permission-target")
                assert await approval_card.locator(".permission-target-count").inner_text() == "8 targets"
                assert await target_rows.count() == 8
                assert await target_rows.nth(0).locator(".permission-target-field").nth(0).inner_text() == "Role\nwrite"
                assert await target_rows.nth(0).locator(".permission-target-field").nth(1).inner_text() == "Path\nsrc/target-0.py"
                assert await target_rows.nth(0).locator(".permission-target-field").nth(2).inner_text() == "Reason\n<img src=x onerror=window.targetsPwned=true>"
                assert await target_panel.evaluate("e=>e.scrollHeight>e.clientHeight && getComputedStyle(e).maxHeight==='180px'")
                assert await approval_card.locator("a, img, script").count() == 0
                assert await page.evaluate("window.targetsPwned") is None
                await page.wait_for_timeout(50)
                assert await page.locator(".permission-card").get_by_role("button", name="Deny once").evaluate("e => e === document.activeElement")
                await page.screenshot(path=str(ARTIFACTS / "approval-safe-focus.png"), full_page=True)
                approval_commands: list[dict[str, object]] = []
                page.on("request", lambda request: approval_commands.append(__import__("json").loads(request.post_data or "{}")) if request.url.endswith("/v1/web/command") else None)
                await page.keyboard.press("Escape")
                assert await page.locator(".permission-card").is_visible()
                assert await page.locator(".permission-card button:nth-child(3)").evaluate("e=>e===document.activeElement")
                assert any(row.get("type") == "PermissionResolve" and row.get("decision") == "deny_once" for row in approval_commands), approval_commands
                before_export = await terminal.call(p.SessionExport(session=session, format="json"))
                await page.locator("#settings-open").evaluate("e=>e.click()")
                await page.locator("#settings-overlay [data-detail-scope='session'] input[value='focused']").check()
                after_export = await terminal.call(p.SessionExport(session=session, format="json"))
                assert before_export.content == after_export.content
                await page.keyboard.press("Escape")
                other_context=await browser.new_context(viewport={"width":800,"height":900})
                other_page=await other_context.new_page()
                isolated_launch=await daemon.web_launch()
                await other_page.goto(isolated_launch,wait_until="domcontentloaded")
                await other_page.goto(f"http://{other_page.url.split('/')[2]}/s/{fixture_a}",wait_until="domcontentloaded")
                await other_page.get_by_text("Current live response remains fully visible.").wait_for(timeout=5_000)
                assert await other_page.locator("html").get_attribute("data-detail")=="balanced"
                await other_context.close()
                assert await page.locator("#connection-banner").is_hidden()
                assert not errors, errors
                assert not console_errors, console_errors
                assert not static_failures, static_failures
                print(f"Playwright handoff passed; terminal-to-web first delta {first_latency_ms} ms")
                print(f"Screenshots: {ARTIFACTS}")
                await browser.close()
        finally:
            if terminal is not None:
                await terminal.close()
            daemon.request_stop("playwright test complete")
            await asyncio.wait_for(task, 10)


if __name__ == "__main__":
    asyncio.run(main())
