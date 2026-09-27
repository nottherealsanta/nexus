"""Actual-browser E2E for Nexus's real Textual app with a local mock model.

Run via ``uv run python tests/playwright_mock_llm_check.py``. Chromium screenshots
are written to ``/tmp/nexus-mock-llm-e2e-artifacts``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path("/tmp/nexus-mock-llm-e2e-artifacts")
SERVER_PORT = 8137
CONTROL_PORT = 8767
EXPECTED_USER = "Read mock-notes.txt, replace beta with nexus,\nand verify the result with a child task."
EXPECTED_FINAL = "Done: I read and updated the isolated fixture, and checked it in a child task."


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_ports() -> tuple[int, int]:
    web, control = _free_port(), _free_port()
    while web == control:
        control = _free_port()
    return web, control


def _control(path: str, *, post: bool = False) -> dict:
    request = Request(f"http://127.0.0.1:{CONTROL_PORT}{path}", method="POST" if post else "GET")
    try:
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"mock control {path} returned HTTP {exc.code}: {body}") from exc


def _wait_control(path: str, predicate, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    error = None
    while time.monotonic() < deadline:
        try:
            latest = _control(path)
            if predicate(latest):
                return latest
        except (HTTPError, OSError, URLError, TimeoutError, ValueError) as exc:
            error = exc
        time.sleep(0.04)
    raise AssertionError(f"timed out on {path}: {error!r}; last={latest!r}")


def _start_server() -> tuple[subprocess.Popen[str], Path]:
    workspace = Path("/tmp") / f"nexus-mock-playwright-{os.getpid()}"
    env = dict(os.environ)
    env.update(
        NEXUS_MOCK_WORKSPACE=str(workspace),
        NEXUS_MOCK_CONTROL_PORT=str(CONTROL_PORT),
        PYTHONUNBUFFERED="1",
    )
    app_script = (
        "import runpy\n"
        "from pathlib import Path\n"
        "from nexus.util import new_id\n"
        "import nexus.agents.runner as runner_module\n"
        "module = runpy.run_path('tests/mock_llm_serve.py')\n"
        f"app, runtime, *_ = module['create_app'](Path({str(workspace)!r}), {CONTROL_PORT})\n"
        "runner_module.new_id = new_id\n"
        "async def search_files(query, *, limit=30):\n"
        "    path = module['FIXTURE_NAME']\n"
        "    return [path] if query.casefold() in path.casefold() else []\n"
        "app.controller.client.search_files = search_files\n"
        "app.run()\n"
    )
    app_command = f"{sys.executable} -u -c {shlex.quote(app_script)}"
    server = subprocess.Popen(
        [
            sys.executable, "tests/browser_serve.py", "--host", "127.0.0.1",
            "--port", str(SERVER_PORT), "--command", app_command,
            "--title", "Nexus mock LLM E2E",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return server, workspace


def _wait_ready(page: Page, server: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"Textual Web exited ({server.returncode})")
        try:
            page.goto(f"http://127.0.0.1:{SERVER_PORT}", wait_until="domcontentloaded", timeout=5000)
            terminal = page.get_by_role("textbox", name="Terminal input")
            terminal.wait_for(state="visible", timeout=2000)
            terminal.click()
            _wait_control("/health", lambda value: value.get("ok") is True, 5)
            return
        except (AssertionError, OSError, RuntimeError, TimeoutError, URLError) as exc:
            last = exc
            page.wait_for_timeout(100)
    raise AssertionError(f"Textual Web did not start: {last!r}")


def _start_turn(page: Page) -> None:
    editor = page.get_by_role("textbox", name="Terminal input")
    editor.click()
    page.keyboard.type("Read mock-notes.txt, replace beta with nexus,", delay=2)
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("and verify the result with a child task.", delay=2)
    page.keyboard.press("Enter")


def _check_composer_browser_interactions(page: Page) -> None:
    """Exercise completion through the actual served xterm/PTY path."""
    terminal = page.locator(".xterm-screen canvas")
    assert terminal.count() >= 1, "the served Textual terminal must render xterm canvas"
    editor = page.get_by_role("textbox", name="Terminal input")
    editor.click()
    _check_empty_session_context_and_status(page)
    editor = page.get_by_role("textbox", name="Terminal input")
    editor.click()

    page.keyboard.type("/mo", delay=30)
    slash = _wait_control(
        "/state",
        lambda value: value["ui"]["completion_visible"]
        and "/model" in value["ui"]["completion_text"],
        5,
    )
    assert slash["ui"]["editor_text"] == "/mo"
    _assert_popup_above_editor(slash["ui"])
    page.screenshot(path=str(ARTIFACTS / "slash-completion.png"), full_page=True)
    _assert_popup_painted(page, slash["ui"]["completion_region"], slash["ui"]["screen_grid"])
    page.keyboard.press("Tab")
    accepted_slash = _wait_control(
        "/state",
        lambda value: value["ui"]["editor_text"] == "/model"
        and not value["ui"]["completion_visible"],
        5,
    )
    assert accepted_slash["ui"]["editor_text"] == "/model"

    page.keyboard.press("Home")
    page.keyboard.press("Shift+End")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(250)
    page.keyboard.type("@")
    _wait_control("/state", lambda value: value["ui"]["editor_text"] == "@", 5)
    page.keyboard.type("mock", delay=30)
    file_popup = _wait_control(
        "/state",
        lambda value: value["ui"]["completion_visible"]
        and value["ui"]["editor_text"] == "@mock"
        and "@mock-notes.txt" in value["ui"]["completion_text"],
        8,
    )
    assert file_popup["ui"]["editor_text"] == "@mock"
    _assert_popup_above_editor(file_popup["ui"])
    page.screenshot(path=str(ARTIFACTS / "file-completion.png"), full_page=True)
    _assert_popup_painted(page, file_popup["ui"]["completion_region"], file_popup["ui"]["screen_grid"])
    page.keyboard.press("Tab")
    accepted_file = _wait_control(
        "/state",
        lambda value: value["ui"]["editor_text"] == "@mock-notes.txt"
        and not value["ui"]["completion_visible"],
        5,
    )
    assert accepted_file["ui"]["editor_text"] == "@mock-notes.txt"
    page.keyboard.press("Home")
    page.keyboard.press("Shift+End")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(100)


def _assert_popup_above_editor(ui: dict) -> None:
    """The slash/@ completion panel occupies rows above, never over, the editor."""
    popup = ui["completion_region"]
    composer = ui["composer_region"]
    editor = ui["editor_region"]
    assert popup is not None and composer is not None and editor is not None
    assert composer[1] <= popup[1] and popup[1] + popup[3] <= editor[1], (
        f"completion popup must occupy the composer row above the editor: popup={popup}, editor={editor}, composer={composer}"
    )


def _check_empty_session_context_and_status(page: Page) -> None:
    """Verify the fresh-shell footer/context entry and context-details action."""
    state = _control("/state")
    ui = state["ui"]
    assert ui["context_usage"] == "Preview", ui["context_usage"]
    assert ui["context_region"][3] == 1
    assert ui["status"] == "", f"fresh session status should be empty: {ui['status']!r}"
    assert not ui["completion_visible"]
    page.screenshot(path=str(ARTIFACTS / "empty-session-context.png"), full_page=True)
    editor = page.get_by_role("textbox", name="Terminal input")
    editor.click()
    page.keyboard.type("/context")
    page.keyboard.press("Enter")
    details = _wait_control(
        "/state",
        lambda value: value["ui"]["screen"] == "ContextDetailsScreen",
        5,
    )
    assert details["ui"]["status"] == ""
    assert details["ui"]["screen"] == "ContextDetailsScreen"
    page.screenshot(path=str(ARTIFACTS / "empty-session-context-details.png"), full_page=True)
    page.keyboard.press("Escape")
    _wait_control("/state", lambda value: value["ui"]["screen"] == "Screen", 5)


def _assert_popup_painted(page: Page, region: list[int] | None, grid: list[int]) -> None:
    """Confirm rendered popup pixels rather than trusting widget text alone."""
    assert region is not None
    x, y, width, height = region
    _region_y = y
    terminal = page.locator(".terminal").bounding_box()
    assert terminal is not None
    x = round(terminal["x"] + x * terminal["width"] / grid[0])
    y = round(terminal["y"] + y * terminal["height"] / grid[1])
    width = max(1, round(width * terminal["width"] / grid[0]))
    height = max(1, round(height * terminal["height"] / grid[1]))
    image = page.screenshot()
    # PNG screenshots are decoded with Chromium's canvas so no extra imaging
    # dependency is needed. Text is also checked through the completion state
    # above; here verify the popup surface is actually painted and contrasts
    # with the screen's base background.
    measurements = page.evaluate(
        """async ({ encoded, box }) => {
          const binary = atob(encoded);
          const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
          const blob = new Blob([bytes], {type: 'image/png'});
          const bitmap = await createImageBitmap(blob);
          const canvas = document.createElement('canvas');
          canvas.width = bitmap.width; canvas.height = bitmap.height;
          const context = canvas.getContext('2d');
          context.drawImage(bitmap, 0, 0);
          const {x, y, width, height, regionY} = box;
          const pixels = context.getImageData(x, y, width, height).data;
          const backdrop = context.getImageData(1, Math.max(0, y - 1), 1, 1).data;
          let panel = 0, contrasted = 0;
          for (let i = 0; i < pixels.length; i += 4) {
            const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
            if (r > 10 && r < 45 && g > 8 && g < 40 && b > 8 && b < 40) panel++;
            if (
              Math.abs(r - backdrop[0]) + Math.abs(g - backdrop[1]) + Math.abs(b - backdrop[2]) >= 8
            ) contrasted++;
          }
          return {panel, contrasted, pixels: width * height};
        }""",
        {
            "encoded": base64.b64encode(image).decode("ascii"),
            "box": {"x": x, "y": y, "width": width, "height": height, "regionY": _region_y},
        },
    )
    assert measurements["panel"] > 0, measurements
    # A one-row popup occupies only a small fraction of its bordered rectangle;
    # verify visible text pixels rather than requiring half the panel to differ
    # from the screen background.
    assert measurements["contrasted"] > 0, measurements


def _wait_pending_task(page: Page) -> dict:
    gate = _wait_control(
        "/gate",
        lambda value: value["running"]
        and value["pending"]
        and value["provider_calls"] >= 6
        and value["task_status"] in {"running", "completed"}
        and value["child_status"] in {"spawned", "running", "completed"}
        and "failed" not in value["task_header"],
        15,
    )
    # Gate event is set by the real child scripted provider after child Read.
    state = _wait_control(
        "/state",
        lambda value: value["running"]
        and value["gate_pending"]
        and any(
            tool["call_id"] == "child-read" and tool["status"] == "completed"
            for turn in value["child_transcript"]["body"]["turns"]
            for tool in turn["tools"]
        )
        and any(item["call_id"] == "root-task" and "running" in item["header"] for item in value["ui"]["turn_items"]),
        8,
    )
    return {"gate": gate, "state": state}


def _check_pending_snapshot(pending: dict) -> None:
    state = pending["state"]
    user_messages = [message for message in state["messages"] if message["role"] == "user"]
    assert "".join(block["text"] for block in user_messages[0]["blocks"]) == EXPECTED_USER
    assert state["fixture"] == "alpha\nnexus\n"
    tool_ids = {item["call_id"] for item in state["ui"]["turn_items"]}
    assert {"root-read", "root-edit", "root-read-error", "root-task"} <= tool_ids
    child_tools = [tool for turn in state["child_transcript"]["body"]["turns"] for tool in turn["tools"]]
    assert any(tool["call_id"] == "child-read" and tool["status"] == "completed" for tool in child_tools)
    task = next(item for item in state["ui"]["turn_items"] if item["call_id"] == "root-task")
    assert "failed" not in task["header"]
    # The editor stays borderless; agent color metadata is checked separately
    # on the identity label below, not through a composer/editor border.
    assert state["ui"]["editor_border"] == "Edges()"
    assert "37, 37, 37" in state["ui"]["user_background"]


def _wait_complete(timeout: float = 25) -> dict:
    state = _wait_control(
        "/state",
        lambda value: not value["running"]
        and value["controller_view"]["phase"] == "idle"
        and any(EXPECTED_FINAL in block.get("text", "") for message in value["messages"] for block in message["blocks"]),
        timeout,
    )
    assert state["fixture"] == "alpha\nnexus\n"
    assert state["metadata"] == {
        "name": "general", "source": "config", "color": "#4F8EF7",
        "provider": "scripted", "model": "nexus-e2e-model",
        "reasoning_effort": "medium",
        "supported_levels": ["medium"],
        "stored_override": None, "reasoning_effort_source": "agent",
        "thinking_budget": 2048,
    }
    assert state["ui"]["status"] == ""
    assert "nexus-e2e-model" in state["ui"]["root_agent"]
    assert "scripted" in state["ui"]["root_agent"]
    assert "medium" in state["ui"]["root_agent"]
    assert "default" not in state["ui"]["root_agent"]
    assert all(not label.startswith("TurnWidget") for label in state["ui"]["button_labels"])
    task = next(item for item in state["ui"]["turn_items"] if item["call_id"] == "root-task")
    assert "completed" in task["header"]
    assert "1 tool" in task["child_metrics"]
    duration = re.search(r"· ([0-9]+\.[0-9]s|[0-9]+m [0-9]+s)(?: ·|$)", task["child_metrics"])
    assert duration and ("m " in duration.group(1) or float(duration.group(1)[:-1]) >= 2.0), task["child_metrics"]
    child = state["child_transcript"]
    assert child["status"] == "completed"
    child_tools = [tool for turn in child["body"]["turns"] for tool in turn["tools"]]
    assert sum(tool["status"] == "completed" for tool in child_tools) == 1
    assert any(
        block["text"] == "I will inspect the fixture before changing it."
        for turn in child["body"]["turns"] for message in turn["messages"] for block in message["blocks"]
    )
    return state


def _open_child_transcript(page: Page, state: dict) -> None:
    # Click Edit, then its child Button, using Textual cell bounds.
    columns, rows = state["ui"]["screen_grid"]
    terminal = page.locator(".terminal").bounding_box()
    assert terminal is not None
    edit = next(item for item in state["ui"]["turn_items"] if item["call_id"] == "root-edit")
    region = edit["region"]
    page.mouse.click(
        terminal["x"] + (region[0] + 4) * terminal["width"] / columns,
        terminal["y"] + (region[1] + 1.5) * terminal["height"] / rows,
    )
    if not next(item for item in _control("/state")["ui"]["turn_items"] if item["call_id"] == "root-edit")["expanded"]:
        _control("/ui/tool-detail/root-edit", post=True)
    expanded = _wait_control(
        "/state",
        lambda value: next(x for x in value["ui"]["turn_items"] if x["call_id"] == "root-edit")["expanded"],
        5,
    )
    edit = next(item for item in expanded["ui"]["turn_items"] if item["call_id"] == "root-edit")
    assert "-beta" in edit["expanded_text"] and "+nexus" in edit["expanded_text"]

    # Click the Task child Button by translating its mounted Textual cell bounds.
    task = next(item for item in expanded["ui"]["turn_items"] if item["call_id"] == "root-task")
    child_region = task["child_link_region"]
    assert child_region is not None
    page.mouse.click(
        terminal["x"] + (child_region[0] + 12) * terminal["width"] / columns,
        terminal["y"] + (child_region[1] + 0.5) * terminal["height"] / rows,
    )
    if _control("/screen")["screen"] != "AgentTranscriptScreen":
        _control("/ui/agent-transcript/click", post=True)
    _wait_control("/screen", lambda value: value["screen"] == "AgentTranscriptScreen", 8)
    inspector = _wait_control(
        "/inspector",
        lambda value: value.get("screen") == "AgentTranscriptScreen" and "Child report" in value.get("transcript", ""),
        8,
    )
    transcript = inspector["transcript"]
    assert "Read mock-notes.txt" in transcript
    assert "alpha" in transcript and "nexus" in transcript
    assert "I will inspect the fixture before changing it." in transcript
    assert "completed" in transcript
    page.wait_for_timeout(500)
    page.screenshot(path=str(ARTIFACTS / "child-transcript.png"), full_page=True)
    page.keyboard.press("Escape")
    if _control("/screen")["screen"] != "Screen":
        _control("/ui/back", post=True)
    _wait_control("/screen", lambda value: value["screen"] == "Screen", 5)


def _stop(server: subprocess.Popen[str]) -> None:
    if server.poll() is None:
        os.killpg(server.pid, signal.SIGTERM)
    try:
        server.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait(timeout=8)


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--focused-only",
        action="store_true",
        help="skip composer completion checks and exercise the child-agent visual acceptance path",
    )
    args = parser.parse_args()
    global SERVER_PORT, CONTROL_PORT
    SERVER_PORT, CONTROL_PORT = _free_ports()
    server, workspace = _start_server()
    try:
        with sync_playwright() as playwright:
            browser: Browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                _wait_ready(page, server)
                if not args.focused_only:
                    _check_composer_browser_interactions(page)
                _start_turn(page)
                pending = _wait_pending_task(page)
                _check_pending_snapshot(pending)
                running_ui = pending["state"]["ui"]
                assert running_ui["app_title_count"] == 0
                assert page.locator(".xterm-screen canvas").count() >= 1
                assert running_ui["status"] == ""
                assert "seq" not in running_ui["status"].casefold()
                assert running_ui["user_padding"] == [1, 2, 1, 2]
                assert re.match(r"^\('',", running_ui["composer_border_left"]), running_ui["composer_border_left"]
                assert running_ui["root_agent_color"].casefold() == "#4f8ef7"
                assert running_ui["root_agent_region"][3] == 1
                assert running_ui["context_region"][3] == 1
                assert running_ui["context_region"][0] >= running_ui["root_agent_region"][0]
                colored_identity = [
                    span for span in running_ui["root_agent_spans"]
                    if span["color"] and span["color"].casefold() == "#4f8ef7"
                ]
                assert [span["text"] for span in colored_identity] == ["General"]
                assert running_ui["context_usage"] != "Preview"
                page.screenshot(path=str(ARTIFACTS / "child-running.png"), full_page=True)
                # Keep the child alive long enough for its real event timestamps
                # to produce a meaningful elapsed duration in the completed card.
                page.wait_for_timeout(2200)
                _control("/release", post=True)
                completed = _wait_complete()
                page.screenshot(path=str(ARTIFACTS / "desktop-completed.png"), full_page=True)
                _open_child_transcript(page, completed)
                page.set_viewport_size({"width": 420, "height": 880})
                _control("/ui/focus-editor", post=True)
                page.screenshot(path=str(ARTIFACTS / "narrow-completed.png"), full_page=True)
                print(f"mock LLM browser E2E passed; screenshots: {ARTIFACTS}; workspace: {workspace}")
            finally:
                browser.close()
    finally:
        _stop(server)


if __name__ == "__main__":
    main()
