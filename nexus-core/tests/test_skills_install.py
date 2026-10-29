"""`/learn`: source forms, finding skills, copying, and the install endpoint (no network: clones are faked)."""
import shutil
from pathlib import Path

import httpx
import pytest

from nexus.server import build_app, make_runtime
from nexus.skills import install
from nexus.store import Store


@pytest.mark.parametrize("text,url,ref,sub,names,scope", [
    ("npx skills add https://github.com/anthropics/skills", "https://github.com/anthropics/skills.git", None, "", (), None),
    ("https://github.com/anthropics/skills", "https://github.com/anthropics/skills.git", None, "", (), None),
    ("anthropics/skills@frontend-design", "https://github.com/anthropics/skills.git", None, "", ("frontend-design",), None),
    ("npx skills add https://github.com/anthropics/skills --skill frontend-design -g -y", "https://github.com/anthropics/skills.git", None, "", ("frontend-design",), "global"),
    ("owner/repo/path/to/skills", "https://github.com/owner/repo.git", None, "path/to/skills", (), None),
    ("https://github.com/o/r/tree/dev/skills/x", "https://github.com/o/r.git", "dev", "skills/x", (), None),
    ("npx skills@latest add o/r -s a b -a claude-code", "https://github.com/o/r.git", None, "", ("a", "b"), None),
    ("git@github.com:o/r.git", "git@github.com:o/r.git", None, "", (), None),
])
def test_remote_forms(text, url, ref, sub, names, scope):
    source = install.parse(text)
    assert (source.url, source.ref, source.subpath, source.skills, source.scope) == (url, ref, sub, names, scope)


def test_local_forms_and_refusals(tmp_path):
    assert install.parse("npx skills add ./my-skills").path == Path("./my-skills")
    assert install.parse("~/x").path == Path("~/x").expanduser()
    for bad in ("", "a b", "--weird o/r", "http://x.com/y", "justaword", "-evil/x", "o/r@"):
        with pytest.raises(install.LearnError):
            install.parse(bad)


def make_repo(root: Path) -> Path:
    for name in ("alpha", "beta"):
        folder = root / "skills" / name
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: The {name} skill.\nlicense: MIT\n---\nBody {name}\n")
        (folder / "references" / "a.md").write_text("ref")
    (root / "skills" / "alpha" / "link").symlink_to("/etc/hosts")
    (root / "skills" / "broken").mkdir()
    (root / "skills" / "broken" / "SKILL.md").write_text("nope")
    (root / ".git").mkdir()
    return root


async def test_learn_copies_chosen_skills_and_skips_links_and_existing(tmp_path):
    repo = make_repo(tmp_path / "repo")
    dest = tmp_path / "dest"
    result = await install.learn(install.parse(f"{repo}/".replace(str(repo), "/" + str(repo).lstrip("/")) + " --skill alpha"), dest)
    assert [i["name"] for i in result["installed"]] == ["alpha"] and result["available"] == ["alpha", "beta"]
    assert (dest / "alpha" / "references" / "a.md").read_text() == "ref" and not (dest / "alpha" / "link").exists()
    assert [p["path"] for p in result["problems"]] == ["skills/broken/SKILL.md"]
    with pytest.raises(install.LearnError, match="already installed"):
        await install.learn(install.parse(str(repo) + " --skill alpha"), dest)
    again = await install.learn(install.parse(str(repo)), dest)  # alpha skipped, beta installed
    assert [i["name"] for i in again["installed"]] == ["beta"] and again["skipped"][0]["name"] == "alpha"
    await install.learn(install.parse(str(repo) + " -s alpha"), dest, overwrite=True)
    with pytest.raises(install.LearnError, match="no skill named nope"):
        await install.learn(install.parse(str(repo) + " -s nope"), dest)
    assert not [p for p in dest.iterdir() if p.name.startswith(".")]


async def test_remote_uses_a_shallow_clone_and_subpath(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "src")
    calls = []

    async def fake_clone(source, into):
        calls.append((source.url, source.ref))
        shutil.copytree(repo, into)
    monkeypatch.setattr(install, "_clone", fake_clone)
    result = await install.learn(install.parse("o/r/skills/beta"), tmp_path / "d")
    assert calls == [("https://github.com/o/r.git", None)] and [i["name"] for i in result["installed"]] == ["beta"]
    with pytest.raises(install.LearnError, match="not a folder"):
        await install.learn(install.parse("o/r/../../etc"), tmp_path / "d2")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rt, token="t", url="http://test")),
                               base_url="http://test", headers={"authorization": "Bearer t"})
    project = tmp_path / "proj"
    project.mkdir()
    return rt, client, project, tmp_path


async def test_endpoint_installs_into_the_chosen_scope(env):
    rt, client, project, tmp = env
    repo = make_repo(tmp / "repo")
    body = {"source": f"npx skills add {repo} --skill beta", "directory": str(project)}
    bad = await client.post("/api/skill/install", json=body)
    assert bad.status_code == 400 and "scope" in bad.text
    ok = await client.post("/api/skill/install", json=body | {"scope": "project"})
    assert ok.status_code == 200 and ok.json()["installed"][0]["path"] == str(project / ".agents" / "skills" / "beta")
    glob = await client.post("/api/skill/install", json=body | {"scope": "global"})
    assert glob.status_code == 200 and (tmp / "userhome" / ".agents" / "skills" / "beta" / "SKILL.md").is_file()
    listed = (await client.get("/api/skill", params={"directory": str(project)})).json()
    assert [s["name"] for s in listed["skills"]] == ["beta"] and listed["skills"][0]["scope"] == "project"
    again = await client.post("/api/skill/install", json=body | {"scope": "project"})
    assert again.status_code == 400 and "already installed" in again.text
    await rt.shutdown()


async def test_preview_then_install_chosen_skills_with_overwrite(env):
    rt, client, project, tmp = env
    repo = make_repo(tmp / "repo")
    body = {"source": str(repo), "scope": "project", "project": (await client.post("/api/project", json={"path": str(project)})).json()["id"]}
    preview = (await client.post("/api/skill/preview", json=body)).json()
    assert [(f["name"], f["installed"]) for f in preview["found"]] == [("alpha", False), ("beta", False)]
    assert preview["requested"] == []
    first = await client.post("/api/skill/install", json={**body, "fetch_id": preview["fetch_id"], "skills": ["beta"]})
    assert first.status_code == 200 and [i["name"] for i in first.json()["installed"]] == ["beta"]
    gone = await client.post("/api/skill/install", json={**body, "fetch_id": preview["fetch_id"], "skills": ["alpha"]})
    assert gone.status_code == 400 and "expired" in gone.text  # one install per fetch
    again = (await client.post("/api/skill/preview", json=body)).json()
    assert {f["name"]: f["installed"] for f in again["found"]} == {"alpha": False, "beta": True}
    kept = await client.post("/api/skill/install", json={**body, "fetch_id": again["fetch_id"], "skills": ["beta"]})
    assert kept.status_code == 400 and "already installed" in kept.text
    again = (await client.post("/api/skill/preview", json=body)).json()
    (project / ".agents" / "skills" / "beta" / "local-edit.txt").write_text("mine")
    done = await client.post("/api/skill/install", json={**body, "fetch_id": again["fetch_id"], "skills": ["alpha", "beta"], "overwrite": True})
    assert done.status_code == 200
    assert {i["name"]: i["replaced"] for i in done.json()["installed"]} == {"alpha": False, "beta": True}
    assert not (project / ".agents" / "skills" / "beta" / "local-edit.txt").exists()
    empty = (await client.post("/api/skill/preview", json=body)).json()
    none = await client.post("/api/skill/install", json={**body, "fetch_id": empty["fetch_id"], "skills": []})
    assert none.status_code == 400 and "at least one" in none.text
    await rt.shutdown()
