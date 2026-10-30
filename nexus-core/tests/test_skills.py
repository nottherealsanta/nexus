"""Skills: lenient frontmatter, fail-closed resources, discovery, the `skill` tool, switches and notices."""
import asyncio
import json

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus import skills
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.skills import frontmatter, resources
from nexus.store import Store


def write_skill(root, name, description="Does a thing. Use when asked.", body="# Steps\n1. Do it.\n", extra=""):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}")
    return folder


# -- frontmatter -----------------------------------------------------------------

def test_public_skill_frontmatter_is_accepted_with_warnings():
    parsed = frontmatter.parse(
        "---\nname: pdf\ndescription: >\n  Work with PDFs.\n  Use when asked.\nlicense: MIT\n"
        "allowed-tools: Bash(git:*), Read\nmetadata:\n  author: x\n  version: '1'\n# a comment\ntags:\n  - a\n  - b\n---\nBody\n")
    assert parsed.description == "Work with PDFs. Use when asked."
    assert parsed.allowed_tools == ("Bash(git:*)", "Read")
    assert parsed.extra["metadata"] == {"author": "x", "version": "1"} and parsed.extra["tags"] == ["a", "b"]
    assert any("license" in w for w in parsed.warnings) and parsed.body == "Body\n"


def test_quoted_and_flow_values():
    parsed = frontmatter.parse('---\nname: "q"\ndescription: \'It\'\'s: fine # not a comment? yes\'\nallowed-tools: [Read, "Grep"]\n---\n')
    assert parsed.name == "q" and parsed.description == "It's: fine # not a comment? yes"
    assert parsed.allowed_tools == ("Read", "Grep")


@pytest.mark.parametrize("text,message", [
    ("no frontmatter", "must start"),
    ("---\nname: a\n", "not closed"),
    ("---\nname: a\n---\n", "description is required"),
    ("---\nname: ../x\ndescription: d\n---\n", "invalid"),
])
def test_unusable_files_are_errors(text, message):
    with pytest.raises(frontmatter.SkillParseError, match=message):
        frontmatter.parse(text)


# -- resources -------------------------------------------------------------------

def test_resources_fail_closed(tmp_path):
    folder = write_skill(tmp_path, "s")
    (folder / "references").mkdir()
    (folder / "references" / "a.md").write_text("doc")
    (folder / "scripts").mkdir()
    (folder / "scripts" / "bin").write_bytes(b"\x00\x01")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (folder / "references" / "link.md").symlink_to(outside)
    assert resources.read(folder, "references/a.md").text == "doc"
    for bad in ("/etc/passwd", "../x", "references/../../secret.txt", "SKILL.md", "references", "references/missing",
                "references\\a.md", "~/x", "references/link.md", "scripts/bin", ""):
        with pytest.raises(resources.ResourceError):
            resources.read(folder, bad)
    assert resources.listing(folder) == ["scripts/bin", "references/a.md"]


# -- discovery -------------------------------------------------------------------

def test_discovery_project_wins_and_problems_are_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    write_skill(tmp_path / "userhome" / ".agents" / "skills", "shared", "global one")
    write_skill(tmp_path / "userhome" / ".agents" / "skills", "only-global")
    project = tmp_path / "p"
    write_skill(project / ".agents" / "skills", "shared", "project one")
    (project / ".agents" / "skills" / "broken").mkdir()
    (project / ".agents" / "skills" / "broken" / "SKILL.md").write_text("nope")
    found = skills.discover(str(project))
    assert found.skills["shared"].scope == "project" and found.skills["shared"].description == "project one"
    assert "only-global" in found.skills and "broken" not in found.skills
    kinds = {p["kind"] for p in found.problems}
    assert kinds == {"error", "shadowed"}


def test_name_defaults_to_the_folder_and_mismatch_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    root = tmp_path / "userhome" / ".agents" / "skills"
    (root / "folder").mkdir(parents=True)
    (root / "folder" / "SKILL.md").write_text("---\ndescription: d\n---\nb")
    (root / "other").mkdir()
    (root / "other" / "SKILL.md").write_text("---\nname: renamed\ndescription: d\n---\nb")
    found = skills.discover(None)
    assert set(found.skills) == {"folder", "renamed"}
    assert any("folder name" in w for w in found.skills["folder"].warnings + found.skills["renamed"].warnings)


# -- through the daemon ----------------------------------------------------------

class Scripted:
    name = "scripted"

    def __init__(self):
        self.replies = []
        self.requests = []

    def body(self, req):
        return {"instructions": req.instructions, "tools": [{"name": t.name} for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace, tmp_path / "userhome" / ".agents" / "skills"


async def _idle(rt):
    for _ in range(500):
        if not rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("still running")


async def _ask(rt, client, provider, sid, replies, text="go"):
    provider.replies = replies
    await client.post(f"/api/session/{sid}/prompt", json={"text": text})
    await _idle(rt)


def tool_results(messages):
    return [str(m) for m in messages if m["role"] == "tool"]


async def test_index_in_the_prompt_and_the_tool_loads_the_body(env):
    rt, client, provider, workspace, root = env
    folder = write_skill(root, "deploy", "Ship the app. Use when deploying.", "# Deploy\nRun it.\n")
    (folder / "references").mkdir()
    (folder / "references" / "env.md").write_text("ENV DOC")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    context = (await client.get(f"/api/session/{session['id']}/context")).json()
    assert "`deploy`: Ship the app. Use when deploying." in rt.store.snapshot(session["id"])["tool_definitions"]["skill"]["description"]
    assert "skill" in [t["name"] for t in context["tools"]]
    listed = (await client.get("/api/skill", params={"directory": str(workspace)})).json()
    assert [s["name"] for s in listed["skills"]] == ["deploy"]
    one = (await client.get("/api/skill/deploy", params={"directory": str(workspace)})).json()
    assert "Run it." in one["body"] and one["files"] == ["references/env.md"]

    sid = session["id"]
    await _ask(rt, client, provider, sid, [[ToolCall("c1", "skill", json.dumps({"name": "deploy"}))],
                                           [ToolCall("c2", "skill", json.dumps({"name": "deploy", "resource": "references/env.md"}))],
                                           [ToolCall("c3", "skill", json.dumps({"name": "deploy", "resource": "../x"}))],
                                           [Text("done")]])
    first, second, third = tool_results((await client.get(f"/api/session/{sid}/message")).json())
    assert '<skill name="deploy"' in first and "Run it." in first and "references/env.md" in first
    assert "ENV DOC" in second
    assert "outside" in third or "must not contain" in third
    await rt.shutdown()


async def test_switched_off_skill_is_not_in_the_index_and_is_refused(env):
    rt, client, provider, workspace, root = env
    write_skill(root, "keep")
    write_skill(root, "drop")
    made = (await client.post("/api/session", json={"directory": str(workspace), "disabled_tools": ["skill:drop"]})).json()
    listing = rt.store.snapshot(made["id"])["tool_definitions"]["skill"]["description"]
    assert "`keep`" in listing and "`drop`" not in listing
    await _ask(rt, client, provider, made["id"], [[ToolCall("c1", "skill", json.dumps({"name": "drop"}))], [Text("ok")]])
    (result,) = tool_results((await client.get(f"/api/session/{made['id']}/message")).json())
    assert "switched off" in result
    bad = await client.patch(f"/api/session/{made['id']}", json={"disabled_tools": ["skill:nope"]})
    assert bad.status_code == 400
    await rt.shutdown()


async def test_skill_added_later_is_not_injected_and_not_loadable(env):
    rt, client, provider, workspace, root = env
    write_skill(root, "old")
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await _ask(rt, client, provider, sid, [[Text("hi")]], "hello")
    frozen = provider.requests[-1].instructions
    write_skill(root, "newcomer", "Brand new skill.")
    await _ask(rt, client, provider, sid, [[ToolCall("c1", "skill", json.dumps({"name": "newcomer"}))], [Text("ok")]], "again")
    assert provider.requests[-2].instructions == frozen and "newcomer" not in frozen
    assert "newcomer" not in json.dumps([str(m) for m in provider.requests[-2].messages])  # nothing was injected
    (result,) = tool_results((await client.get(f"/api/session/{sid}/message")).json())
    assert "no skill named 'newcomer'" in result
    fresh = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert {s["name"] for s in fresh["skills"]} == {"old", "newcomer"}
    await rt.shutdown()


async def test_header_switches_inject_one_json_notice_and_only_cover_what_started(env):
    rt, client, provider, workspace, root = env
    write_skill(root, "a")
    write_skill(root, "b")
    made = (await client.post("/api/session", json={"directory": str(workspace), "disabled_tools": ["skill:b"]})).json()
    sid = made["id"]
    assert {(s["name"], s["state"], s["locked"]) for s in made["skills"]} == {("a", "on", False), ("b", "off", True)}
    off = await client.patch(f"/api/session/{sid}", json={"disabled_tools": ["skill:a", "skill:b"]})
    assert off.status_code == 200 and {s["name"]: s["state"] for s in off.json()["skills"]} == {"a": "off", "b": "off"}
    await _ask(rt, client, provider, sid, [[Text("ok")]], "hi")
    sent = [str(m) for m in provider.requests[-1].messages]
    assert any('{"skill_unavailable": {"name": "a"}}' in m for m in sent) and not any("tool availability" in m for m in sent)
    locked = await client.patch(f"/api/session/{sid}", json={"disabled_tools": ["skill:a"]})
    assert locked.status_code == 400 and "new session" in locked.text
    back = await client.patch(f"/api/session/{sid}", json={"disabled_tools": ["skill:b"]})
    assert back.status_code == 200 and {x["name"]: x["state"] for x in back.json()["skills"]} == {"a": "on", "b": "off"}
    await _ask(rt, client, provider, sid, [[Text("ok")]], "again")
    assert any('{"skill_available": {"name": "a"}}' in str(m) for m in provider.requests[-1].messages)
    await rt.shutdown()


async def test_skill_edited_mid_session_loads_current_text_and_says_so(env):
    rt, client, provider, workspace, root = env
    write_skill(root, "s", body="v1")
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    write_skill(root, "s", body="v2")
    await _ask(rt, client, provider, sid, [[ToolCall("c1", "skill", json.dumps({"name": "s"}))], [Text("ok")]])
    (result,) = tool_results((await client.get(f"/api/session/{sid}/message")).json())
    assert "v2" in result and "changed on disk" in result
    await rt.shutdown()


async def test_no_skills_means_no_skill_tool(env):
    rt, client, _, workspace, _ = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    context = (await client.get(f"/api/session/{sid}/context")).json()
    assert "skill" not in [t["name"] for t in context["tools"]]
    await rt.shutdown()
