import pytest

from nexus import agents


def _write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text)


def test_builtins_parse_with_their_modes():
    found = agents.load(None)
    assert not found.problems
    assert {a.name: a.mode for a in found.agents.values()} == {
        "build": "all", "orchestrate": "primary", "plan": "subagent", "quick": "subagent", "scout": "subagent"}
    assert [a.name for a in found.allowed(child=False)] == ["build", "orchestrate"]
    assert [a.name for a in found.allowed(child=True)] == ["build", "plan", "quick", "scout"]
    # Only orchestrate can start sessions; scout is read-only.
    assert {a.name for a in found.agents.values() if a.control} == {"orchestrate"}
    assert found.get("scout").tools == ("read", "bash") and found.get("build").tools is None


def test_mode_is_enforced():
    found = agents.load(None)
    with pytest.raises(ValueError, match="cannot run as a root session; allowed: build, orchestrate"):
        found.get("scout", child=False)
    with pytest.raises(ValueError, match="cannot run as a child session"):
        found.get("orchestrate", child=True)
    assert found.get("build", child=True).name == "build"


def test_project_overrides_global_overrides_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / "agents", "build", "---\ndescription: global build\n---\nGlobal.")
    project = tmp_path / "proj"
    _write(project / ".agents" / "agents", "build", "---\ndescription: project build\nsteps: 5\n---\nProject.")
    build = agents.load(str(project)).get("build")
    assert (build.description, build.source, build.prompt, build.steps) == ("project build", "project", "Project.", 5)
    assert agents.load(None).get("build").source == "global"


@pytest.mark.parametrize("text, error", [
    ("Just text", "missing frontmatter"),
    ("---\nmodel: x/y\n---\nBody", "description is required"),
    ("---\ndescription: d\ntools: read\n---\nBody", "tools must be a list"),
    ("---\ndescription: d\ncolour: red\n---\nBody", "unknown frontmatter keys: colour"),
    ("---\ndescription: d\n---\n", "body (system prompt) is empty"),
    ("---\ndescription: d\nsteps: 0\n---\nBody", "steps must be"),
    ("---\ndescription: d\nmode: child\n---\nBody", "mode must be one of"),
])
def test_bad_files_are_reported_not_skipped(tmp_path, monkeypatch, text, error):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / "agents", "bad", text)
    found = agents.load(None)
    assert "bad" not in found.agents
    assert error in found.problems[0]["error"]


def test_unknown_agent_names_the_options():
    with pytest.raises(ValueError, match="available: build, orchestrate, plan, quick, scout"):
        agents.load(None).get("nope")
