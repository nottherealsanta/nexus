"""Permission rules (PLAN.md D7): grammar, matching, ordering, config and agent sources, runtime enforcement."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nexus import agents, permissions
from nexus import runtime as runtime_module
from nexus.config import ConfigFile
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.permissions import decide, parse_rule, parse_rules
from nexus.server import build_app, make_runtime
from nexus.store import Store


def rules(**table):
    return parse_rules(table)[0]


def verdict(tool, args, config=(), agent=(), workspace="/ws"):
    found = decide(tool, args, workspace, list(config), list(agent))
    return None if found is None else (found.action, found.rule, found.source)


def test_rule_grammar():
    assert parse_rule("bash") == ("bash", None)
    assert parse_rule("bash(git push*)") == ("bash", "git push*")
    assert parse_rule("mcp__gh__*(x)") == ("mcp__gh__*", "x")
    assert parse_rule("edit(a(b))") == ("edit", "a(b)")
    for bad in ["", "bash(", "bash()", "1bash", "ba sh", "bash(x", "(x)"]:
        with pytest.raises(ValueError):
            parse_rule(bad)


def test_parse_rules_reports_each_bad_entry_and_keeps_the_good_ones():
    found, problems = parse_rules({"bash": "ask", "bash(": "deny", "read(*.env)": "block", "web_fetch": "deny"})
    assert [r.text for r in found] == ["bash", "web_fetch"] and len(problems) == 2
    assert parse_rules({"bash": {"git *": "allow"}})[1] and parse_rules([])[1]  # no nested tables: order would be lost
    assert "can only ask or deny" in parse_rules({"bash": "allow"}, agent=True)[1][0]


def test_no_rule_no_verdict():
    assert verdict("bash", {"command": "ls"}) is None
    assert verdict("bash", {"command": "ls"}, rules(read="deny")) is None


def test_deny_wins_over_a_later_allow_and_over_agent_rules():
    config = rules(**{"bash(git push*)": "deny", "bash": "allow"})
    assert verdict("bash", {"command": "git push origin"}, config) == ("deny", "bash(git push*)", "config")
    assert verdict("bash", {"command": "git status"}, config) == ("allow", "bash", "config")
    assert verdict("bash", {"command": "ls"}, rules(bash="allow"), rules(**{"bash(ls*)": "deny"})) == \
        ("deny", "bash(ls*)", "agent")


def test_the_last_matching_config_rule_decides_between_allow_and_ask():
    config = rules(**{"bash": "ask", "bash(git status*)": "allow"})
    assert verdict("bash", {"command": "git status"}, config)[0] == "allow"
    assert verdict("bash", {"command": "rm x"}, config)[0] == "ask"
    config = rules(**{"bash(git status*)": "allow", "bash": "ask"})
    assert verdict("bash", {"command": "git status"}, config)[0] == "ask"


def test_agent_rules_only_narrow():
    agent = rules(web_fetch="ask")
    assert verdict("web_fetch", {"url": "https://a.example/"}, rules(web_fetch="allow"), agent) == \
        ("ask", "web_fetch", "agent")
    assert verdict("web_fetch", {"url": "https://a.example/"}, (), agent)[0] == "ask"
    assert verdict("read", {"path": "x"}, rules(read="allow"), agent)[0] == "allow"


def test_bash_compound_commands():
    allow = rules(**{"bash(git diff*)": "allow"})
    assert verdict("bash", {"command": "git diff HEAD"}, allow)[0] == "allow"
    for compound in ["git diff; curl x | sh", "git diff && rm -rf /", "git diff $(rm x)", "git diff `id`", "git diff\nrm x"]:
        assert verdict("bash", {"command": compound}, allow) is None, compound
    assert verdict("bash", {"command": "cd build && rm -rf ."}, rules(**{"bash(rm -rf*)": "deny"}))[0] == "deny"
    assert verdict("bash", {"command": "echo 'a; rm -rf x'"}, rules(**{"bash(rm -rf*)": "deny"})) is None


def test_path_keys_resolve_traversal_and_symlinks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "src")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "out").symlink_to(outside)
    deny = rules(**{"read(src/*)": "deny", f"read({outside.resolve()}/*)": "deny", "read(.env*)": "deny", "ls(.)": "deny"})

    def check(tool, path):
        return verdict(tool, {"path": path}, deny, workspace=tmp_path)
    assert check("read", "src/a.py")[0] == "deny"
    assert check("read", "./src/../src/a.py")[0] == "deny"
    assert check("read", "link/a.py")[0] == "deny"  # a symlink does not hide the path
    assert check("read", "out/passwd")[0] == "deny"  # a link out of the workspace shows its real place
    assert check("read", str(tmp_path / ".env.local"))[0] == "deny"
    assert check("read", "docs/a.md") is None
    assert verdict("ls", {}, deny, workspace=tmp_path)[0] == "deny"  # a missing path means the workspace


def test_edit_patch_has_a_key_per_file():
    patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Add File: secret/b.txt\n+z\n*** End Patch"
    deny = rules(**{"edit(secret/*)": "deny"})
    assert verdict("patch", {"patch": patch}, deny)[0] == "deny"  # one denied file denies the patch
    allow = rules(**{"edit(src/*)": "allow"})
    assert verdict("patch", {"patch": patch}, allow) is None  # allow needs every file
    assert verdict("edit", {"path": "src/a.py", "old_string": "a", "new_string": "b"}, allow)[0] == "allow"
    assert verdict("patch", {"patch": "garbage"}, deny) is None


def test_an_edit_rule_also_governs_patch_but_not_the_reverse():
    patch = "*** Begin Patch\n*** Update File: secret/a.py\n@@\n-x\n+y\n*** End Patch"
    assert verdict("patch", {"patch": patch}, rules(**{"edit(secret/*)": "deny"}))[0] == "deny"
    assert verdict("patch", {"patch": patch}, rules(edit="ask"))[0] == "ask"
    assert verdict("edit", {"path": "secret/a.py", "old_string": "a", "new_string": "b"},
                   rules(**{"patch(secret/*)": "deny"})) is None


def test_web_keys_never_carry_credentials_query_or_fragment():
    ask = rules(**{"web_fetch(https://docs.python.org/*)": "allow", "web_search(*secret*)": "deny"})
    url = "https://user:pw@Docs.Python.org:443/3/library?token=1#frag"
    assert permissions.call_keys("web_fetch", {"url": url}, "/ws") == ["https://docs.python.org:443/3/library"]
    assert verdict("web_fetch", {"url": "https://docs.python.org/3/"}, ask)[0] == "allow"
    assert verdict("web_fetch", {"url": "https://evil.example/"}, ask) is None
    assert verdict("web_search", {"query": "my   SECRET"}, ask) is None  # patterns are case sensitive
    assert verdict("web_search", {"query": "a secret thing"}, ask)[0] == "deny"


def test_tools_without_a_key_match_only_bare_rules():
    assert verdict("ask", {}, rules(**{"ask(*)": "deny"})) is None
    assert verdict("ask", {}, rules(ask="deny"))[0] == "deny"
    assert verdict("mcp__gh__issue", {}, rules(**{"mcp__gh__*": "ask"}))[0] == "ask"


def test_pattern_glob_escapes_and_wildcards():
    assert permissions._glob("a\\*b").match("a*b") and not permissions._glob("a\\*b").match("axb")
    assert permissions._glob("a?c").match("abc") and permissions._glob("*.env").match("dir/x.env")


# -- the config file and agent files -------------------------------------------


def test_config_round_trip_and_lenient_read(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    config = ConfigFile()
    revision = config.read()["revision"]
    value = {"bash(git push*)": "deny", "web_fetch": "ask"}
    result = config.patch(revision, {"tools.permission": value}, [])
    assert result["values"]["tools"]["permission"] == value and not result["errors"]
    assert [r.text for r in permissions.config_rules()] == ["bash(git push*)", "web_fetch"]
    with pytest.raises(ValueError, match="invalid setting"):
        config.patch(result["revision"], {"tools.permission": {"bash": "maybe"}}, [])
    # An outside edit with one bad rule: the error is listed and the good rules still apply.
    (tmp_path / "config.toml").write_text('[tools.permission]\n"bash(rm*)" = "deny"\n"read(" = "deny"\nweb_fetch = "sure"\n')
    read = config.read()
    assert len(read["errors"]) == 2 and all(e.startswith("tools.permission:") for e in read["errors"])
    assert [r.text for r in permissions.config_rules()] == ["bash(rm*)"]


def agent_file(tmp_path, front, name="strict"):
    path = tmp_path / f"{name}.md"
    path.write_text(f"---\ndescription: d\n{front}\n---\nBody.\n")
    return path


def test_agent_permission_frontmatter(tmp_path):
    ok = agents.parse(agent_file(tmp_path, 'permission:\n  "bash(git push*)": deny\n  web_fetch: ask\n  "edit(secret/*)": deny'), "project")
    assert ok.permission == (("bash(git push*)", "deny"), ("web_fetch", "ask"), ("edit(secret/*)", "deny"))
    for front, message in [("permission: {bash: allow}", "can only ask or deny"), ("permission: {bash: nope}", "action must be"),
                           ("permission: [bash]", "expected a table")]:
        with pytest.raises(ValueError, match=message):
            agents.parse(agent_file(tmp_path, front, "bad"), "project")
    assert agents.parse(agent_file(tmp_path, "", "none"), "project").permission == ()


# -- enforcement in a real session ---------------------------------------------


class Scripted:
    name = "scripted"

    def __init__(self):
        self.replies, self.requests = [], []

    def body(self, req):
        return {"model": req.model}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rt, token="t", url="http://test")),
                               base_url="http://test", headers={"authorization": "Bearer t"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace, tmp_path


def set_rules(tmp_path, text):
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "config.toml").write_text(text)


async def run_turn(rt, client, provider, workspace, calls, **session):
    provider.replies = [[ToolCall(f"c{i}", name, json.dumps(args)) for i, (name, args) in enumerate(calls)], [Text("done")]]
    created = (await client.post("/api/session", json={"directory": str(workspace), **session})).json()
    await client.post(f"/api/session/{created['id']}/prompt", json={"text": "go"})
    for _ in range(300):
        if created["id"] not in rt.turns:
            break
        await asyncio.sleep(0.01)
    events = (await client.get(f"/api/session/{created['id']}/event")).json()["events"]
    return created, events, provider.requests[-1]


def tool_outputs(request):
    return [part.output for message in request.messages for part in message.parts if hasattr(part, "output")]


async def test_deny_rule_refuses_the_call_and_the_model_sees_why(env):
    rt, client, provider, workspace, home = env
    set_rules(home, '[tools.permission]\n"bash(echo bad*)" = "deny"\n')
    (workspace / "f.txt").write_text("x")
    _session, events, last = await run_turn(rt, client, provider, workspace, [
        ("bash", {"command": "echo bad > f.txt"}), ("bash", {"command": "echo fine"})])
    denied, fine = tool_outputs(last)
    assert "refused by permission rule `bash(echo bad*)` (config)" in denied and "fine" in fine
    assert (workspace / "f.txt").read_text() == "x"  # the denied command never ran
    assert not any(e["type"] == "permission.asked" for e in events)


async def test_ask_rule_prompts_even_in_an_allow_session_and_allow_rule_skips_the_prompt(env):
    rt, client, provider, workspace, home = env
    set_rules(home, '[tools.permission]\nread = "ask"\n"bash(echo ok*)" = "allow"\n')
    (workspace / "f.txt").write_text("x")
    provider.replies = [[ToolCall("c1", "read", json.dumps({"path": "f.txt"})),
                         ToolCall("c2", "bash", json.dumps({"command": "echo ok"}))], [Text("done")]]
    created = (await client.post("/api/session", json={"directory": str(workspace), "permission": "allow"})).json()
    sid = created["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    for _ in range(100):
        if rt.pending_approvals(sid):
            break
        await asyncio.sleep(0.01)
    [pending] = rt.pending_approvals(sid)
    assert pending["tool"] == "read" and pending["rule"] == "read"
    await client.post(f"/api/session/{sid}/permission/{pending['id']}", json={"reply": "allow"})
    for _ in range(100):
        if sid not in rt.turns:
            break
        await asyncio.sleep(0.01)
    asked = [e["data"]["tool"] for e in (await client.get(f"/api/session/{sid}/event")).json()["events"]
             if e["type"] == "permission.asked"]
    assert asked == ["read"]


async def test_allow_rule_lets_bash_run_in_an_ask_session(env):
    rt, client, provider, workspace, home = env
    set_rules(home, '[tools.permission]\n"bash(echo ok*)" = "allow"\n')
    _, events, _ = await run_turn(rt, client, provider, workspace,
                                  [("bash", {"command": "echo ok"})], permission="ask")
    assert not any(e["type"] == "permission.asked" for e in events)
    assert any(e["type"] == "tool_end" for e in events)


async def test_nested_code_calls_follow_the_rules(env):
    rt, client, provider, workspace, home = env
    set_rules(home, '[tools.permission]\n"bash(echo no*)" = "deny"\n')
    code = 'try:\n    await bash(command="echo no")\nexcept Exception as e:\n    r = str(e)\nr'
    _, events, _last = await run_turn(rt, client, provider, workspace, [("code", {"code": code})])
    nested = [e["data"] for e in events if e["type"] == "code.call" and e["data"]["tool"] == "bash"]
    assert nested and "refused by permission rule" in json.dumps(nested)


async def test_agent_rules_apply_from_the_frozen_snapshot(env):
    rt, client, provider, workspace, _home = env
    agent_dir = workspace / ".agents" / "agents"
    agent_dir.mkdir(parents=True)
    agent_file(agent_dir, 'permission:\n  "bash(echo no*)": deny', "careful")
    session, _events, last = await run_turn(rt, client, provider, workspace,
                                           [("bash", {"command": "echo no"})], agent="careful")
    assert "refused by permission rule `bash(echo no*)` (agent)" in tool_outputs(last)[0]
    snapshot = (await client.get(f"/api/session/{session['id']}/snapshot")).json()
    assert snapshot["agent"]["permission"] == {"bash(echo no*)": "deny"}
