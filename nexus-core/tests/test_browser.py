"""Browser tools: browser_exec and browser_screenshot as ordinary tools, their process and the browser setting."""
import json
import sys
from pathlib import Path

import pytest
from conftest import remembering
from test_mcp import Scripted, _run

from nexus import browser as browser_control
from nexus import runtime as runtime_module
from nexus.llmhub.types import Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

FIXTURE = str(Path(__file__).parent / "fixtures" / "browser_fixture.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    import httpx
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    rt.browser.command = (sys.executable, FIXTURE)
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace, home


async def _tools(client, sid):
    return [t["name"] for t in (await client.get(f"/api/session/{sid}/context")).json()["tools"]]


async def test_the_two_tools_are_offered_directly_and_start_the_process_lazily(env):
    rt, client, provider, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    tools = await _tools(client, sid)
    assert [n for n in tools if n.startswith(("browser_", "expose_browser"))] == ["browser_exec", "browser_screenshot"]
    assert not rt.browser.connections  # nothing starts before the first call

    results = await _run(rt, client, provider, sid, [
        [ToolCall("c1", "browser_exec", json.dumps({"code": "x = 41\nprint(x + 1, FLAG)"}))],
        [ToolCall("c2", "browser_exec", json.dumps({"code": "print(x)"}))],
        [ToolCall("c3", "browser_screenshot", "{}")],
        [Text("done")]])
    assert "42 --mcp" in results[0]  # its own Chromium by default, never the user's Chrome
    assert "<untrusted-mcp-data>" in results[0] and "came from the browser tools" in results[0]
    assert "browser restarted" not in results[0]  # the first start is not a restart
    assert "41" in results[1]  # the namespace persists between calls
    assert "image" in results[2]
    artifacts = (await client.get(f"/api/session/{sid}/artifact")).json()
    assert any(a["source"] == "mcp:browser-use/browser_screenshot" for a in artifacts)
    await rt.shutdown()
    assert not rt.browser.connections


async def test_login_and_feedback_are_not_offered_even_though_upstream_has_them(env):
    rt, client, provider, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    tools = await _tools(client, sid)
    assert not {"browser_login", "browser_feedback", "expose_browser_tools"} & set(tools)
    results = await _run(rt, client, provider, sid, [[ToolCall("c1", "browser_login", "{}")], [Text("ok")]])
    assert "signed in" not in results[0]
    await rt.shutdown()


async def test_the_setting_picks_the_users_chrome_and_a_change_restarts_the_process(env):
    rt, client, provider, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    config = (await client.get("/api/config")).json()
    assert config["values"]["browser"]["use_my_chrome"] is False
    first = await _run(rt, client, provider, sid, [
        [ToolCall("c1", "browser_exec", json.dumps({"code": "print(FLAG)"}))], [Text("ok")]])
    assert "--mcp" in first[0] and "--cli-mcp" not in first[0]
    patched = await client.patch("/api/config", json={"expected_revision": config["revision"],
                                                      "set": {"browser.use_my_chrome": True}, "unset": []})
    assert patched.status_code == 200, patched.text
    results = await _run(rt, client, provider, sid, [
        [ToolCall("c2", "browser_exec", json.dumps({"code": "print(FLAG)"}))], [Text("ok")]])
    assert "--cli-mcp" in results[-1] and "[Nexus notice: browser restarted]" in results[-1]
    await rt.shutdown()


async def test_an_agent_with_a_tools_list_gets_them_only_by_name(env):
    rt, client, _, workspace, _ = env
    (workspace / ".agents" / "agents").mkdir(parents=True)
    (workspace / ".agents" / "agents" / "narrow.md").write_text(
        "---\ndescription: reads only\nmode: primary\ntools: [read]\n---\nRead.\n")
    (workspace / ".agents" / "agents" / "browsing.md").write_text(
        "---\ndescription: reads and browses\nmode: primary\ntools: [read, browser_exec]\n---\nBrowse.\n")
    made = (await client.post("/api/session", json={"directory": str(workspace), "agent": "narrow"})).json()
    sid = made["id"]
    assert not {"browser_exec", "browser_screenshot"} & set(await _tools(client, sid))
    named = (await client.post("/api/session", json={"directory": str(workspace), "agent": "browsing"})).json()["id"]
    assert [n for n in await _tools(client, named) if n.startswith("browser_")] == ["browser_exec"]
    off = (await client.post("/api/session", json={"directory": str(workspace),
                                                    "disabled_tools": ["browser_exec"]})).json()["id"]
    tools = await _tools(client, off)
    assert "browser_screenshot" in tools and "browser_exec" not in tools
    assert {"browser_exec", "browser_screenshot"} <= runtime_module.FORK_REFUSED
    await rt.shutdown()


async def test_a_dead_process_restarts_on_the_next_call_and_says_the_state_is_gone(env):
    rt, client, provider, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    results = await _run(rt, client, provider, sid, [
        [ToolCall("c1", "browser_exec", json.dumps({"code": "x = 1\nprint('first')"}))],
        [ToolCall("c2", "browser_exec", json.dumps({"code": "import os\nos._exit(1)"}))],
        [ToolCall("c3", "browser_exec", json.dumps({"code": "print('x' in globals())"}))],
        [ToolCall("c4", "browser_exec", json.dumps({"code": "print('steady')"}))],
        [Text("done")]])
    assert "first" in results[0] and "browser restarted" not in results[0]
    assert "unavailable" in results[1] and "starts browser-use again" in results[1]
    assert "[Nexus notice: browser restarted]" in results[2] and "False" in results[2]  # no backoff, state is gone
    assert "steady" in results[3] and "browser restarted" not in results[3]
    await rt.shutdown()


async def test_after_a_daemon_restart_the_first_call_says_the_browser_restarted(env):
    rt, client, provider, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await _run(rt, client, provider, sid, [
        [ToolCall("c1", "browser_exec", json.dumps({"code": "print(1)"}))], [Text("ok")]])
    await rt.browser.close()
    rt.browser.started.clear()  # as in a new daemon: only the stored history says the browser ran
    results = await _run(rt, client, provider, sid, [
        [ToolCall("c2", "browser_exec", json.dumps({"code": "print(2)"}))], [Text("ok")]])
    assert "[Nexus notice: browser restarted]" in results[-1]
    await rt.shutdown()


def test_definitions_are_nexus_own_and_name_only_the_two_tools():
    assert browser_control.BROWSER_TOOLS == ("browser_exec", "browser_screenshot")
    text = json.dumps([browser_control.definition(n) for n in browser_control.BROWSER_TOOLS])
    assert "start_remote_daemon" not in text and "browser_login" not in text and "Settings" in text


def _bare_machine(monkeypatch, tmp_path, *, uvx=False, chrome=False):
    """No browser anywhere browser-use looks, optionally uvx and a Google Chrome in a fake /Applications."""
    apps = tmp_path / "Applications"
    monkeypatch.setattr(browser_control.sys, "platform", "darwin")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    monkeypatch.setattr(browser_control.Path, "home", staticmethod(lambda: tmp_path / "home"))
    real_path = browser_control.Path

    def fake_path(*parts):
        text = str(real_path(*parts))
        return real_path(text.replace("/Applications", str(apps), 1) if text.startswith("/Applications") else text)
    monkeypatch.setattr(browser_control, "Path", fake_path)
    fake_path.home = staticmethod(lambda: tmp_path / "home")  # type: ignore[attr-defined]
    monkeypatch.setattr(browser_control.shutil, "which", lambda name: str(tmp_path / "bin" / name) if uvx and name == "uvx" else None)
    if uvx:
        (tmp_path / "bin").mkdir(exist_ok=True)
        (tmp_path / "bin" / "uvx").write_text("")
    if chrome:
        binary = apps / "Google Chrome.app/Contents/MacOS/Google Chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("")


def test_requirements_name_each_gap_and_its_fix(monkeypatch, tmp_path):
    _bare_machine(monkeypatch, tmp_path)
    needed = browser_control.requirements(False)
    assert needed["ok"] is False and needed["mode"] == "own"
    assert any("uvx was not found" in p for p in needed["problems"])
    assert any("browser-use install" in p and "Google Chrome" in p for p in needed["problems"])


def test_requirements_find_chrome_and_a_playwright_chromium(monkeypatch, tmp_path):
    _bare_machine(monkeypatch, tmp_path, uvx=True, chrome=True)
    needed = browser_control.requirements(True)
    assert needed["ok"] and needed["problems"] == [] and needed["browser"]["name"] == "Google Chrome"
    assert needed["chrome"]["name"] == "Google Chrome" and "remote debugging" in needed["note"]
    chromium = tmp_path / "pw/chromium-1200/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"
    chromium.parent.mkdir(parents=True)
    chromium.write_text("")
    (tmp_path / "Applications").rename(tmp_path / "gone")
    needed = browser_control.requirements(True)
    assert needed["ok"] and needed["browser"]["name"] == "Chromium (browser-use install)"
    assert any("Google Chrome is not installed" in p for p in needed["problems"])  # mode chrome needs Chrome itself


async def test_a_call_says_what_is_missing_instead_of_a_failed_start(env, monkeypatch, tmp_path):
    rt, client, provider, workspace, _ = env
    rt.browser.check = True  # as with the real browser-use command
    monkeypatch.setattr(browser_control, "requirements", lambda mine: {
        "ok": False, "problems": ["no Chrome, Chromium or Edge was found: install Google Chrome"]})
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    results = await _run(rt, client, provider, sid, [
        [ToolCall("c1", "browser_exec", json.dumps({"code": "print(1)"}))], [Text("ok")]])
    assert "cannot start" in results[0] and "install Google Chrome" in results[0]
    assert not rt.browser.connections
    await rt.shutdown()


async def test_api_and_doctor_report_the_requirements(env, monkeypatch):
    rt, client, _, _, _ = env
    from nexus import doctor
    monkeypatch.setattr(browser_control, "requirements", lambda mine: {
        "ok": True, "mode": "chrome" if mine else "own", "uvx": "/bin/uvx", "problems": [], "note": "n",
        "browser": {"name": "Google Chrome", "path": "/c"}, "chrome": None, "package": browser_control.PACKAGE})
    body = (await client.get("/api/browser")).json()
    assert body["ok"] and body["mode"] == "own" and body["browser"]["name"] == "Google Chrome"
    row = doctor.check_browser()
    assert row["status"] == "ok" and "own profile" in row["detail"] and "/bin/uvx" in row["detail"]
    monkeypatch.setattr(browser_control, "requirements", lambda mine: {"ok": False, "mode": "own", "problems": ["no uvx"]})
    assert doctor.check_browser() == {"name": "browser", "status": "warn", "detail": "no uvx"}
    await rt.shutdown()
