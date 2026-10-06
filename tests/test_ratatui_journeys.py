"""Native workflow journeys against the real host facade (no network, no native terminal).

Journeys exercise native settings, providers, first-run setup, worktrees and
attachments step by step.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_host_provider_auth import FakeClaude, FakeDevice, MemorySecrets, _runtime  # noqa: F401

from nexus.client.protocol import Client
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.runtime import Runtime
from nexus.ui.ratatui.actions import ShellActions


class FacadeTransport:
    def __init__(self, facade):
        self.facade = facade

    async def request(self, command):
        return await self.facade.handle(command)

    async def events(self, *_args, **_kwargs):
        if False:
            yield None

    async def aclose(self):
        pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    return home


def shell_for(client, session="s"):
    return ShellActions(SimpleNamespace(session=session, client=client))


def labels(shell):
    return [item["label"] for item in shell.items]


def op(shell, label_start):
    return next(item["operation"] for item in shell.items if item["label"].startswith(label_start))


def page_blocks(shell):
    """Every block of the open one-page Settings area, section children included."""
    def walk(blocks):
        for block in blocks:
            yield block
            if block.get("t") == "section":
                yield from walk(block["blocks"])
    return list(walk(shell.workflows.settings_page["blocks"]))


def page_labels(shell):
    return [b["label"] for b in page_blocks(shell) if b.get("t") == "row"]


def file_op(shell, label_start):
    """The Edit operation of the file row whose label starts with ``label_start``."""
    return next(b["control"]["operation"] for b in page_blocks(shell) if b.get("t") == "row" and b["label"].startswith(label_start))


def page_button(shell, label_start):
    return next(i["operation"] for b in page_blocks(shell) if b.get("t") == "buttons" for i in b["items"] if i["label"].startswith(label_start))


# -- settings -----------------------------------------------------------------

@pytest.fixture
def settings_shell(tmp_path, home):
    workspace = tmp_path / "workspace"
    (workspace / ".agents" / "agents").mkdir(parents=True)
    (workspace / ".agents" / "agents" / "helper.md").write_text("---\nname: helper\ndescription: test\n---\nHello.\n")
    runtime = type("RuntimeStub", (), {"workspace": workspace, "extensions": None})()
    return shell_for(Client(FacadeTransport(HostFacade(runtime)))), workspace


@pytest.mark.asyncio
async def test_settings_navigation_scope_and_back_stack(settings_shell):
    shell, _ = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "global"})
    assert shell.panel_title == "Settings · Appearance", "Settings opens on the first area, not a list of sections"
    await flows.settings_area("tools")
    assert flows.settings_page["scope"]["options"] == ["Global", "Project"], "tools can differ per project"
    await flows.operate({"kind": "sp_tools", "area": "tools", "key": "scope", "value": 1})
    assert flows.settings_scope == "project" and flows.settings_page["scope"]["value"] == 1
    await flows.settings_area("agents")
    # Agents are always global: no scope control, whatever the last choice was.
    assert shell.panel_title == "Settings · Agents" and flows.settings_page["scope"] is None
    files = page_labels(shell)  # this stub host has no default-agent answer; the files still list
    assert files[0].startswith("build") and "built-in" in files[0]
    # Opening a file keeps the page reachable: Back from the editor returns to it.
    await flows.operate(file_op(shell, "build"))
    assert shell.panel_title == "Agent · build" and labels(shell)[-1] == "Edit prompt file…"
    await flows.operate(op(shell, "Edit prompt file…"))
    assert flows.form and flows.form["status"].startswith("Built-in default")
    flows.back()
    assert shell.panel_title == "Agent · build"
    flows.back()
    assert shell.panel_title == "Settings · Agents"
    flows.back()
    assert shell.panel_title == "" and shell.settings_nav is None, "Escape from the page closes Settings"


@pytest.mark.asyncio
async def test_project_file_edit_conflict_validation_and_delete(settings_shell):
    shell, workspace = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "project"})
    assert not any(label.startswith("Soul") for label in labels(shell))
    # The hidden category remains supported at the host/editor boundary.
    await flows.operate({"kind": "settings", "scope": "project", "category": "soul"})
    assert not any("Delete" in label for label in labels(shell))
    await flows.operate(op(shell, "New file"))
    await flows.save(flows.form["id"], "SOUL.md", 1)
    assert flows.form_target["sha256"] == "" and flows.form["autosave"]
    await flows.save(flows.form["id"], "Be brief.\n", 2)
    assert (workspace / ".agents" / "SOUL.md").read_text() == "Be brief.\n"
    assert flows.form["status"].startswith("Saved")
    sha = flows.form_target["sha256"]
    assert sha
    # Hash conflict: another writer changes the file; the draft is kept and autosave stops.
    (workspace / ".agents" / "SOUL.md").write_text("Changed elsewhere\n")
    await flows.save(flows.form["id"], "Mine\n", 3)
    assert "Conflict" in flows.form["status"] and not flows.form["autosave"] and flows.form["body"] == "Mine\n"
    assert (workspace / ".agents" / "SOUL.md").read_text() == "Changed elsewhere\n"
    # Back from the new-file editor lands on a refreshed category list that includes the file.
    flows.back()
    assert shell.panel_title == "Settings · project · soul"
    assert any(label.startswith("SOUL") for label in labels(shell))
    # Delete from the editor: confirm, then trash and a fresh list.
    await flows.operate(op(shell, "SOUL"))
    target = flows.form_target
    await flows.operate({"kind": "confirm", "label": "x", "next": {**target, "kind": "settings_delete"}})
    assert shell.panel_title == "Delete SOUL.md to trash?" or shell.panel_title.startswith("Delete ")
    await flows.operate(op(shell, "Continue") if False else shell.items[1]["operation"])
    assert not (workspace / ".agents" / "SOUL.md").exists()
    assert shell.panel_title == "Settings · project · soul"
    assert not any(label.startswith("SOUL") for label in labels(shell))
    assert shell.notice == "Deleted to trash"
    # Back never resurrects the deleted confirmation or editor.
    flows.back()
    assert shell.panel_title == "Settings · Appearance"


@pytest.mark.asyncio
async def test_validation_error_from_host_keeps_draft_and_builtin_cannot_be_deleted(settings_shell):
    shell, workspace = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "project", "category": "config"})
    await flows.operate(op(shell, "New file"))
    await flows.save(flows.form["id"], "config.toml", 1)
    with pytest.raises(Exception):  # noqa: B017 - the host rejects an invalid configuration body
        await flows.save(flows.form["id"], "this is = = not toml", 2)
    assert flows.form["body"] == "this is = = not toml" and not flows.form["saved"] and not flows.form["autosave"]
    await flows.operate({"kind": "settings", "scope": "global", "category": "agents"})
    await flows.operate(file_op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    assert flows.form_target["builtin"] is True
    with pytest.raises(ValueError, match="Built-in"):
        await flows.operate({"kind": "confirm", "label": "x", "next": {**flows.form_target, "kind": "settings_delete"}})


@pytest.mark.asyncio
async def test_agent_override_saves_then_resets_to_builtin(settings_shell, home):
    shell, _ = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "global", "category": "agents"})
    await flows.operate(file_op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    body = flows.form["body"]
    await flows.save(flows.form["id"], body + "\nExtra line.\n", 1)
    assert flows.form["status"].startswith("Saved")
    assert flows.form_target["overrides_builtin"] is True and flows.form_target["builtin"] is False
    flows.back()
    flows.back()
    await flows.refresh_page()
    assert any(label.startswith("build") and "edited" in label for label in page_labels(shell))
    await flows.operate(file_op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    await flows.operate({"kind": "confirm", "label": "x", "next": {**flows.form_target, "kind": "settings_delete"}})
    assert shell.panel_title.startswith("Reset build to the built-in default")
    await flows.operate(shell.items[1]["operation"])
    assert shell.notice == "Reset to built-in default"
    assert any(label.startswith("build") and "built-in" in label for label in page_labels(shell))


@pytest.mark.asyncio
async def test_reset_category_lists_removed_files_and_returns_to_fresh_list(settings_shell):
    shell, workspace = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "project", "category": "skills"})
    await flows.operate(page_button(shell, "New file"))
    await flows.save(flows.form["id"], "demo", 1)
    assert "name: demo" in flows.form["body"]  # same starter template as native terminal
    await flows.save(flows.form["id"], flows.form["body"], 2)
    flows.back()
    await flows.refresh_page()
    assert any(label.startswith("demo") for label in page_labels(shell))
    await flows.operate(page_button(shell, "Reset category"))
    assert "demo" in shell.panel_lines
    await flows.operate(shell.items[1]["operation"])
    assert shell.panel_title == "Settings · Skills" and shell.notice == "Reset to default"
    assert not any(label.startswith("demo") for label in page_labels(shell))
    assert not (workspace / ".agents" / "skills" / "demo" / "SKILL.md").exists()


# -- providers ---------------------------------------------------------------

def provider_shell(home, **kwargs):
    runtime, secrets = _runtime(home, **kwargs)
    facade = HostFacade.__new__(HostFacade)
    facade.runtime = runtime
    facade.supervisor = SimpleNamespace(running=False)
    return shell_for(Client(FacadeTransport(facade))), runtime, secrets


def _sections(shell):
    return {block["title"]: block for block in shell.workflows.settings_page["blocks"] if block.get("t") == "section"}


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block["blocks"])


def _row(section, id_suffix):
    return next(b for b in _walk(section["blocks"]) if b.get("t") == "row" and b["id"].endswith(id_suffix))


def _button(section, label_start):
    return next(item["operation"] for b in _walk(section["blocks"]) if b.get("t") == "buttons"
                for item in b["items"] if item["label"].startswith(label_start))


def _buttons(section):
    return [item["label"] for b in _walk(section["blocks"]) if b.get("t") == "buttons" for item in b["items"]]


@pytest.mark.asyncio
async def test_api_key_journey_never_echoes_the_key_and_shows_state(home):
    shell, _, secrets = provider_shell(home)
    flows = shell.workflows
    await flows.operate({"kind": "providers"})
    assert shell.panel_title == "Settings · Providers" and shell.settings_nav == "providers"
    assert list(_sections(shell)) == ["ChatGPT (Codex)", "Claude (Pro/Max)", "GitHub Copilot", "OpenCode Go"], "one section per provider, not connected: A–Z"
    section = _sections(shell)["OpenCode Go"]
    assert section["summary"] == "not connected" and "Sign out…" not in _buttons(section)
    field = _row(section, ":key")["control"]
    assert field["secret"] is True and field["value"] == ""
    key = "sk-go-live-0123456789abcdef"
    with pytest.raises(ValueError):
        await flows.operate({**field["operation"], "value": "  "})
    await flows.operate({**field["operation"], "value": key})
    assert secrets.values["opencode-go:default"] == key
    section = _sections(shell)["OpenCode Go"]
    assert section["summary"] == "connected" and "Sign out…" in _buttons(section)
    assert list(_sections(shell))[0] == "OpenCode Go", "connected providers come first"
    assert key not in repr((shell.workflows.settings_page, shell.panel_lines, shell.items, shell.toasts, shell.panel_title))
    await flows.operate(_button(section, "Sign out"))
    assert shell.panel_title == "Sign out of OpenCode Go?"
    await flows.operate(shell.items[1]["operation"])
    assert shell.panel_title == "Settings · Providers"
    assert "opencode-go:default" not in secrets.values and "Sign out…" not in _buttons(_sections(shell)["OpenCode Go"])


@pytest.mark.asyncio
async def test_claude_code_flow_cancel_resume_and_failure(home):
    claude = FakeClaude()
    shell, runtime, _ = provider_shell(home, claude=claude)
    flows = shell.workflows
    await flows.operate({"kind": "provider", "id": "claude-agent"})
    section = _sections(shell)["Claude (Pro/Max)"]
    assert section["open"] and "Sign in with browser" in _buttons(section)
    assert "Sign out…" not in _buttons(section)  # can_logout is false for Claude
    await flows.operate(_button(section, "Sign in"))
    section = _sections(shell)["Claude (Pro/Max)"]
    assert section["summary"] == "sign-in pending" and any("claude.com" in str(b) for b in _walk(section["blocks"]))
    code = _row(section, ":code")["control"]
    assert code["secret"] is True, "the sign-in code is typed in a masked field"
    # The pending sign-in survives leaving and returning to the page.
    await flows.operate({"kind": "provider", "id": "claude-agent"})
    assert _sections(shell)["Claude (Pro/Max)"]["summary"] == "sign-in pending"
    with pytest.raises(ValueError):
        await flows.operate({**code["operation"], "value": ""})
    await flows.operate({**code["operation"], "value": "abc123#state"})
    assert "abc123" not in repr((flows.settings_page, shell.toasts, shell.panel_lines))
    await runtime._provider_logins[flows.login.login_id].task
    refresh = _button(_sections(shell)["Claude (Pro/Max)"], "Refresh")
    await flows.operate(refresh)
    assert flows.login is None and _sections(shell)["Claude (Pro/Max)"]["summary"] == "connected"
    assert claude.code == "abc123#state"


@pytest.mark.asyncio
async def test_device_flow_poll_pending_then_cancel_and_denial(home):
    device = FakeDevice()
    shell, runtime, _ = provider_shell(home, copilot=device)
    flows = shell.workflows
    await flows.operate({"kind": "provider", "id": "github-copilot"})
    await flows.operate({"kind": "sp_providers", "area": "providers", "key": "login", "provider": "github-copilot", "method": "device"})
    section = _sections(shell)["GitHub Copilot"]
    assert _row(section, ":user-code")["control"]["value"] == "WXYZ-0000"
    await flows.operate(_button(section, "Refresh"))
    assert _sections(shell)["GitHub Copilot"]["summary"] == "sign-in pending"  # still pending: stay on the code
    await flows.operate(_button(_sections(shell)["GitHub Copilot"], "Cancel"))
    assert flows.login is None and _sections(shell)["GitHub Copilot"]["summary"] == "not connected"
    assert shell.toasts[-1]["title"] == "Sign-in cancelled"
    assert not any(label.startswith("Resume") for label in _buttons(_sections(shell)["GitHub Copilot"]))
    # Approval path ends connected, with a Sign out button.
    device2 = FakeDevice()
    shell, runtime, _ = provider_shell(home, copilot=device2)
    await shell.workflows.operate({"kind": "provider", "id": "github-copilot"})
    await shell.workflows.operate({"kind": "sp_providers", "area": "providers", "key": "login", "provider": "github-copilot", "method": "device"})
    device2.approved.set()
    await runtime._provider_logins[shell.workflows.login.login_id].task
    await shell.workflows.operate(_button(_sections(shell)["GitHub Copilot"], "Refresh"))
    assert "Sign out…" in _buttons(_sections(shell)["GitHub Copilot"])


@pytest.mark.asyncio
async def test_setup_journey_selects_connected_provider_default(home, tmp_path):
    shell, runtime, secrets = provider_shell(home)
    await shell.workflows.operate({"kind": "sp_providers", "area": "providers", "key": "api_key", "provider": "opencode-go", "value": "sk-test-key-0123456789"})
    shell.client.setup_status = lambda: _status()  # noqa: E731
    async def _status():
        return p.SetupStatusResult(required=True, providers=[
            {"id": "opencode-go", "label": "OpenCode Go", "connected": True, "instruction": "key", "auto": True},
            {"id": "openai", "label": "OpenAI", "connected": False, "instruction": "set OPENAI_API_KEY"}])
    async def _save(provider, model=""):
        assert (provider, model) == ("opencode-go", "")
        return p.SetupSaveResult(global_model="opencode-go/m", restart_required=True)
    shell.client.setup_save = _save
    await shell.workflows.operate({"kind": "setup"})
    assert labels(shell)[0] == "Use OpenCode Go · newest model"
    assert any("not connected · OpenAI" in line for line in shell.panel_lines)
    await shell.workflows.operate(shell.items[0]["operation"])
    assert shell.panel_title == "Setup saved" and any("Restart the daemon" in line for line in shell.panel_lines)


# -- worktrees ---------------------------------------------------------------

def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout.strip()


def worktree_setup(tmp_path, files=1, size=0):
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-q")
    git(parent, "config", "user.name", "t")
    git(parent, "config", "user.email", "t@example.invalid")
    for index in range(files):
        (parent / f"f{index}.txt").write_text("base\n")
    git(parent, "add", ".")
    git(parent, "commit", "-qm", "initial")
    runtime = Runtime(parent, providers={})
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    for index in range(files):
        (record.path / f"f{index}.txt").write_text(f"child {index}\n" + "".join(f"line {n} of file {index}\n" for n in range(size)))
    runtime._worktree_service.mark_finished(record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root)
    shell = shell_for(Client(FacadeTransport(HostFacade(runtime))))
    return shell, runtime, parent, record


@pytest.mark.asyncio
async def test_review_pages_acknowledge_and_integrate_into_parent(tmp_path):
    shell, runtime, parent, record = worktree_setup(tmp_path, files=10, size=9000)  # well over eight 128 KiB diff pages
    flows = shell.workflows
    calls = []
    original = shell.client.review_worktree
    async def spy(child, *, review_id=None, cursor=0, limit=1):
        calls.append((cursor, review_id))
        return await original(child, review_id=review_id, cursor=cursor, limit=limit)
    shell.client.review_worktree = spy
    try:
        await flows.operate({"kind": "worktrees"})
        assert labels(shell)[0].endswith(record.child_id)
        await flows.operate(op(shell, labels(shell)[0]))
        await flows.operate(op(shell, "Review changes"))
        text = "\n".join(shell.panel_lines)
        assert shell.panel_title.startswith("Worktree review")
        assert [call[0] for call in calls] == [0, 8, 16][:len(calls)] and len(calls) >= 2  # advances by one full page, never repeats
        assert calls[0][1] is None and {call[1] for call in calls[1:]} == {flows.review.review_id}
        assert "-- page 2" in text and "clipped" in text  # long diffs announce clipping
        assert all(f"f{index}.txt" in text for index in range(10))
        assert flows.review.digest in text
        before = git(parent, "rev-parse", "HEAD")
        await flows.operate(op(shell, "Acknowledge"))
        assert any("Acknowledged exact digest" in line and flows.review.digest in line for line in shell.panel_lines)
        await flows.operate(op(shell, "Integrate"))
        assert shell.panel_title.startswith("Confirm integrate") and git(parent, "rev-parse", "HEAD") == before
        assert (parent / "f0.txt").read_text() == "base\n"  # preview mutated nothing
        assert any("f0.txt" in line for line in shell.panel_lines)
        await flows.operate(op(shell, "Confirm"))
        assert shell.panel_title == "Worktree outcome"
        assert (parent / "f9.txt").read_text().startswith("child 9\n")
        assert flows.review is None
        assert labels(shell) == ["Back to worktrees"]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_stale_review_or_forged_token_cannot_mutate(tmp_path):
    shell, runtime, parent, record = worktree_setup(tmp_path)
    flows = shell.workflows
    try:
        await flows.operate({"kind": "worktree_review", "id": record.child_id})
        await flows.operate({"kind": "worktree_ack"})
        integrate = op(shell, "Integrate")
        await flows.operate(integrate)
        proceed = op(shell, "Confirm")
        # Another review replaces the pinned identity: the earlier confirmation must not apply.
        flows.review = SimpleNamespace(**{**vars(flows.review), "review_id": "0" * 32}) if hasattr(flows.review, "__dict__") else None
        if flows.review is not None:
            with pytest.raises(ValueError, match="expired"):
                await flows.operate(proceed)
        flows.review = None
        with pytest.raises(ValueError, match="Confirmation expired"):
            await flows.operate(proceed)
        assert (parent / "f0.txt").read_text() == "base\n" and record.path.exists()
        # A forged token is refused by the host.
        await flows.operate({"kind": "worktree_review", "id": record.child_id})
        await flows.operate({"kind": "worktree_ack"})
        forged = {**op(shell, "Integrate"), "token": "forged"}
        with pytest.raises(Exception):  # noqa: B017
            await flows.operate(forged)
        assert (parent / "f0.txt").read_text() == "base\n"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_clean_discard_and_force_discard_use_confirmation_tokens(tmp_path):
    shell, runtime, parent, record = worktree_setup(tmp_path)
    flows = shell.workflows
    try:
        await flows.operate({"kind": "worktree_review", "id": record.child_id})
        await flows.operate({"kind": "worktree_ack"})
        await flows.operate(op(shell, "Discard"))
        assert shell.panel_title.startswith("Confirm discard") and record.path.exists()
        await flows.operate(op(shell, "Cancel"))
        assert shell.panel_title == "Worktrees" and record.path.exists()  # cancel mutates nothing
        # Force discard of a dirty child: preview first (shows the dirty warning), then the token.
        await flows.operate({"kind": "worktree_actions", "id": record.child_id})
        assert any("uncommitted" in line for line in shell.panel_lines)
        await flows.operate(op(shell, "Force discard"))
        assert shell.panel_title.startswith("Confirm discard") and record.path.exists()
        assert any("WARNING: force discard" in line for line in shell.panel_lines)
        await flows.operate(op(shell, "Confirm"))
        assert shell.panel_title == "Worktree outcome" and not record.path.exists()
        assert (parent / "f0.txt").read_text() == "base\n"
        await flows.operate(op(shell, "Back to worktrees"))
        assert shell.panel_title == "Worktrees" and labels(shell) == [f"discarded · {record.child_id}"]  # list survives a discard
    finally:
        await runtime.aclose()


# -- attachments -------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_attachment_kind_is_numbered_previewed_and_removal_keeps_numbers(tmp_path, home):
    from attachment_fixtures import PNG, docx_bytes, pdf_bytes
    files = {"a.png": PNG, "b.png": PNG, "c.txt": b"plain text", "d.md": b"# Markdown", "e.pdf": pdf_bytes(), "f.docx": docx_bytes()}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    runtime = Runtime(tmp_path, providers={})
    client = Client(FacadeTransport(HostFacade(runtime)))
    shell = shell_for(client)
    sent = []
    async def enqueue(session, text, **kwargs):
        sent.append((text, kwargs))
    client.enqueue = enqueue
    try:
        for name in files:
            await shell.command("/attach", [str(tmp_path / name)])
            reference = shell.composer_insert
            assert reference.startswith("[image " if name.endswith(".png") else "[document ")
        assert [shell.attachment_label(i) for i in range(6)] == ["image 1", "image 2", "document 1", "document 2", "document 3", "document 4"]
        assert shell.panel_title.startswith("Attachment · ")  # converted (markdown) documents open their preview
        await shell.command("/attach", [])
        assert len(shell.items) == 6 and "document 2 · d.md" in labels(shell)[3]
        await shell.workflows.operate(shell.items[0]["operation"])
        assert shell.panel_title == "Attachment · a.png"
        assert shell.preview_image
        await shell.workflows.operate(shell.items[0]["operation"])  # remove image 1
        assert [shell.attachment_label(i) for i in range(5)] == ["image 2", "document 1", "document 2", "document 3", "document 4"]
        assert [item["label"].split(" · ")[0] for item in shell.items] == ["image 2", "document 1", "document 2", "document 3", "document 4"]
        await shell.command("/attach", [str(tmp_path / "a.png")])  # a new image never reuses a removed number
        assert shell.composer_insert == "[image 3]"
        await shell.submit("see [image 2] and [image 3]")
        text, kwargs = sent[0]
        assert kwargs["attachment_labels"] == ["image 2", "document 1", "document 2", "document 3", "document 4", "image 3"]
        assert len(kwargs["attachments"]) == 6 and shell.attachments == [] and shell.attachment_labels == {}
        for _ in range(8):
            await shell.command("/attach", [str(tmp_path / "c.txt")])
        with pytest.raises(ValueError, match="eight"):
            await shell.command("/attach", [str(tmp_path / "c.txt")])
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_submitted_attachment_content_reaches_real_runtime_and_failed_conversion_is_not_attached(tmp_path, home, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    runtime = Runtime(tmp_path, providers={})
    shell = shell_for(Client(FacadeTransport(HostFacade(runtime))))
    try:
        with pytest.raises(Exception):  # noqa: B017 - unsupported/corrupt document
            await shell.command("/attach", [str(tmp_path / "bad.pdf")])
        assert shell.attachments == [] and shell.composer_insert == ""
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_agent_page_edits_model_and_fallbacks_through_the_host(settings_shell, home):
    shell, workspace = settings_shell
    flows = shell.workflows
    (home / ".nexus" / "agents").mkdir(parents=True, exist_ok=True)
    (home / ".nexus" / "agents" / "helper.md").write_text("---\nname: helper\ndescription: test\n---\nHello.\n")
    await flows.operate({"kind": "settings", "scope": "global", "category": "agents"})
    await flows.operate(file_op(shell, "helper"))
    assert shell.panel_title == "Agent · helper"
    assert labels(shell)[0] == "Run on · Session model"
    await flows.operate(op(shell, "Run on"))
    assert labels(shell)[0] == "Run on · Specific model" and "+ Add fallback" in labels(shell)
    await flows.operate({"kind": "agent_set", "field": "model", "index": 0, "ref": "openai/gpt-x"})
    assert shell.panel_title == "Agent · helper" and labels(shell)[1] == "Model · openai/gpt-x" and "  × Clear model" in labels(shell)
    await flows.operate({"kind": "agent_set", "field": "fallback", "index": 0, "ref": "anthropic/claude-y"})
    await flows.operate({"kind": "agent_set", "field": "fallback", "index": 1, "ref": "openai/gpt-z"})
    assert [l for l in labels(shell) if l.startswith("Fallback")] == ["Fallback 1 · anthropic/claude-y", "Fallback 2 · openai/gpt-z"]
    saved = (home / ".nexus" / "agents" / "helper.md").read_text() if (home / ".nexus" / "agents" / "helper.md").exists() else ""
    assert "model: openai/gpt-x" in saved and "fallback: [anthropic/claude-y, openai/gpt-z]" in saved and saved.rstrip().endswith("Hello.")
    await flows.operate({"kind": "agent_clear", "field": "fallback", "index": 0})
    await flows.operate({"kind": "agent_clear", "field": "model"})
    assert labels(shell)[1] == "Model · choose a model" and [l for l in labels(shell) if l.startswith("Fallback")] == ["Fallback 1 · openai/gpt-z"]
