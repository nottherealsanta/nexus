"""Native workflow journeys against the real host facade (no network, no Textual).

Each journey mirrors the matching Textual screen (``tui_settings``, ``tui_providers``,
``tui_setup``, ``WorktreesScreen``, ``attachments``) step by step.
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
    assert shell.panel_title == "Settings · global · sections"
    assert "Switch to project" in labels(shell)
    await flows.operate(op(shell, "Switch to project"))
    assert shell.panel_title == "Settings · project · sections"
    await flows.operate(op(shell, "Agents"))
    # Agents are always global: the scope toggle never applies (Textual hides it).
    assert shell.panel_title == "Settings · global · agents"
    assert labels(shell)[0] == "New sessions start with…"
    ordered = [label for label in labels(shell) if "built-in" in label]
    assert ordered[0].startswith("build") and ordered[-1].startswith("task") or ordered[-1].startswith("quick")
    # Opening a file keeps the list reachable: Back from the editor returns to the category page.
    await flows.operate(op(shell, "build"))
    assert shell.panel_title == "Agent · build" and labels(shell)[-1] == "Edit prompt file…"
    await flows.operate(op(shell, "Edit prompt file…"))
    assert flows.form and flows.form["status"].startswith("Built-in default")
    flows.back()
    assert shell.panel_title == "Agent · build"
    flows.back()
    assert shell.panel_title == "Settings · global · agents"
    flows.back()
    assert shell.panel_title == "Settings · project · sections"


@pytest.mark.asyncio
async def test_project_file_edit_conflict_validation_and_delete(settings_shell):
    shell, workspace = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "project"})
    await flows.operate(op(shell, "Soul"))
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
    assert shell.panel_title == "Settings · project · sections"


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
    await flows.operate(op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    assert flows.form_target["builtin"] is True
    with pytest.raises(ValueError, match="Built-in"):
        await flows.operate({"kind": "confirm", "label": "x", "next": {**flows.form_target, "kind": "settings_delete"}})


@pytest.mark.asyncio
async def test_agent_override_saves_then_resets_to_builtin(settings_shell, home):
    shell, _ = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "global", "category": "agents"})
    await flows.operate(op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    body = flows.form["body"]
    await flows.save(flows.form["id"], body + "\nExtra line.\n", 1)
    assert flows.form["status"].startswith("Saved")
    assert flows.form_target["overrides_builtin"] is True and flows.form_target["builtin"] is False
    flows.back()
    flows.back()
    assert any(label.startswith("build") and "edited" in label for label in labels(shell))
    await flows.operate(op(shell, "build"))
    await flows.operate(op(shell, "Edit prompt file…"))
    await flows.operate({"kind": "confirm", "label": "x", "next": {**flows.form_target, "kind": "settings_delete"}})
    assert shell.panel_title.startswith("Reset build to the built-in default")
    await flows.operate(shell.items[1]["operation"])
    assert shell.notice == "Reset to built-in default"
    assert any(label.startswith("build") and "built-in" in label for label in labels(shell))


@pytest.mark.asyncio
async def test_reset_category_lists_removed_files_and_returns_to_fresh_list(settings_shell):
    shell, workspace = settings_shell
    flows = shell.workflows
    await flows.operate({"kind": "settings", "scope": "project", "category": "skills"})
    await flows.operate(op(shell, "New file"))
    await flows.save(flows.form["id"], "demo", 1)
    assert "name: demo" in flows.form["body"]  # same starter template as Textual
    await flows.save(flows.form["id"], flows.form["body"], 2)
    flows.back()
    await flows.operate(op(shell, "Reset category"))
    assert "demo" in shell.panel_lines
    await flows.operate(shell.items[1]["operation"])
    assert shell.panel_title == "Settings · project · skills" and shell.notice == "Reset to default"
    assert not any(label.startswith("demo") for label in labels(shell))
    assert not (workspace / ".agents" / "skills" / "demo" / "SKILL.md").exists()


# -- providers ---------------------------------------------------------------

def provider_shell(home, **kwargs):
    runtime, secrets = _runtime(home, **kwargs)
    facade = HostFacade.__new__(HostFacade)
    facade.runtime = runtime
    facade.supervisor = SimpleNamespace(running=False)
    return shell_for(Client(FacadeTransport(facade))), runtime, secrets


@pytest.mark.asyncio
async def test_api_key_journey_never_echoes_the_key_and_shows_state(home):
    shell, _, secrets = provider_shell(home)
    flows = shell.workflows
    await flows.operate({"kind": "providers"})
    assert [label.split(" · ")[0] for label in labels(shell)] == ["ChatGPT (Codex)", "GitHub Copilot", "OpenCode Go", "Claude (Pro/Max)"]
    await flows.operate(op(shell, "OpenCode Go"))
    assert labels(shell) == ["Set API key"]
    await flows.operate(op(shell, "Set API key"))
    key = "sk-go-live-0123456789abcdef"
    with pytest.raises(ValueError):
        await flows.save(flows.form["id"], "  ", 1)
    await flows.save(flows.form["id"], key, 2)
    assert secrets.values["opencode-go:default"] == key
    assert flows.form is None and shell.panel_title == "OpenCode Go"
    assert "Sign out…" in labels(shell)
    assert key not in repr((shell.panel_lines, shell.items, shell.panel_title))
    flows.back()
    assert shell.panel_title == "Providers"
    await flows.operate(op(shell, "OpenCode Go"))
    await flows.operate(op(shell, "Sign out"))
    await flows.operate(shell.items[1]["operation"])
    assert "opencode-go:default" not in secrets.values and "Sign out…" not in labels(shell)


@pytest.mark.asyncio
async def test_claude_code_flow_cancel_resume_and_failure(home):
    claude = FakeClaude()
    shell, runtime, _ = provider_shell(home, claude=claude)
    flows = shell.workflows
    await flows.operate({"kind": "provider", "id": "claude-agent"})
    assert "Sign in with browser" in labels(shell)
    assert "Sign out…" not in labels(shell)  # can_logout is false for Claude
    await flows.operate(op(shell, "Sign in"))
    assert shell.panel_title == "Provider sign-in"
    assert labels(shell)[0] == "Paste sign-in code" and any("claude.com" in line for line in shell.panel_lines)
    # Leaving and returning offers to resume the pending sign-in.
    await flows.operate({"kind": "provider", "id": "claude-agent"})
    assert "Resume sign-in" in labels(shell)
    await flows.operate(op(shell, "Resume"))
    await flows.operate(op(shell, "Paste sign-in code"))
    with pytest.raises(ValueError):
        await flows.save(flows.form["id"], "", 1)
    await flows.save(flows.form["id"], "abc123#state", 2)
    assert shell.panel_title == "Provider sign-in" and shell.panel_lines[0].startswith("Code sent")
    assert "abc123" not in repr(shell.panel_lines)
    await runtime._provider_logins[flows.login.login_id].task
    await flows.operate({"kind": "provider_poll"})
    assert shell.panel_title == "Claude (Pro/Max)"
    assert any("connected" in line.lower() for line in shell.panel_lines)
    assert claude.code == "abc123#state"


@pytest.mark.asyncio
async def test_device_flow_poll_pending_then_cancel_and_denial(home):
    device = FakeDevice()
    shell, runtime, _ = provider_shell(home, copilot=device)
    flows = shell.workflows
    await flows.operate({"kind": "provider_login", "id": "github-copilot", "method": "device"})
    assert any("WXYZ-0000" in line for line in shell.panel_lines)
    await flows.operate({"kind": "provider_poll"})
    assert shell.panel_title == "Provider sign-in"  # still pending: stay on the code screen
    await flows.operate({"kind": "provider_cancel"})
    assert shell.panel_title == "GitHub Copilot" and flows.login is None
    assert any("cancelled" in line.lower() for line in shell.panel_lines)
    assert not any(label.startswith("Resume") for label in labels(shell))
    # Approval path ends on the provider card with the host message.
    device2 = FakeDevice()
    shell, runtime, _ = provider_shell(home, copilot=device2)
    await shell.workflows.operate({"kind": "provider_login", "id": "github-copilot", "method": "device"})
    device2.approved.set()
    await runtime._provider_logins[shell.workflows.login.login_id].task
    await shell.workflows.operate({"kind": "provider_poll"})
    assert shell.panel_title == "GitHub Copilot" and "Sign out…" in labels(shell)


@pytest.mark.asyncio
async def test_setup_journey_selects_connected_provider_default(home, tmp_path):
    shell, runtime, secrets = provider_shell(home)
    await shell.workflows.operate({"kind": "provider_key", "id": "opencode-go"})
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
            assert reference.split()[0] == ("image" if name.endswith(".png") else "document")
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
        assert shell.composer_insert == "image 3"
        await shell.submit("see image 2 and image 3")
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
    await flows.operate(op(shell, "helper"))
    assert shell.panel_title == "Agent · helper"
    assert labels(shell)[0] == "Model · inherit the session model" and "+ Add fallback" in labels(shell)
    await flows.operate({"kind": "agent_set", "field": "model", "index": 0, "ref": "openai/gpt-x"})
    assert shell.panel_title == "Agent · helper" and labels(shell)[0] == "Model · openai/gpt-x" and "  × Clear model" in labels(shell)
    await flows.operate({"kind": "agent_set", "field": "fallback", "index": 0, "ref": "anthropic/claude-y"})
    await flows.operate({"kind": "agent_set", "field": "fallback", "index": 1, "ref": "openai/gpt-z"})
    assert [l for l in labels(shell) if l.startswith("Fallback")] == ["Fallback 1 · anthropic/claude-y", "Fallback 2 · openai/gpt-z"]
    saved = (home / ".nexus" / "agents" / "helper.md").read_text() if (home / ".nexus" / "agents" / "helper.md").exists() else ""
    assert "model: openai/gpt-x" in saved and "fallback: [anthropic/claude-y, openai/gpt-z]" in saved and saved.rstrip().endswith("Hello.")
    await flows.operate({"kind": "agent_clear", "field": "fallback", "index": 0})
    await flows.operate({"kind": "agent_clear", "field": "model"})
    assert labels(shell)[0] == "Model · inherit the session model" and [l for l in labels(shell) if l.startswith("Fallback")] == ["Fallback 1 · openai/gpt-z"]
