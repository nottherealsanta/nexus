from nexus.cli import _agent_list, _chat_line


class AgentClient:
    async def call(self, method, path, *args, **kwargs):
        assert (method, path) == ("GET", "/api/agent")
        return {"agents": [
            {"name": name, "mode": mode, "source": "project", "model": None,
             "effort": None, "steps": None, "tools": None, "description": ""}
            for name, mode in [("build", "all"), ("orchestrate", "primary"),
                               ("scout", "subagent"), ("custom", "primary")]
        ], "problems": []}


async def test_agent_list_keeps_full_catalog_by_default(capsys):
    await _agent_list(AgentClient(), "/work")
    output = capsys.readouterr().out
    assert all(name in output for name in ("build", "orchestrate", "scout", "custom"))


async def test_chat_agent_list_follows_session_context(capsys):
    session = {"id": "s", "directory": "/work", "agent": "build", "model": "m",
               "model_source": "session", "effort": None, "disabled_tools": []}
    for parent, included, excluded in [
        (None, ("build", "orchestrate", "custom"), "scout"),
        ("parent", ("build", "scout"), "orchestrate"),
    ]:
        await _chat_line(AgentClient(), {**session, "parent_id": parent}, "/agent", None)
        output = capsys.readouterr().out
        assert all(name in output for name in included)
        assert excluded not in output
        if parent:
            assert "custom" not in output
