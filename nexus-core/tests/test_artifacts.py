"""Durable artifacts: MCP media is stored, served safely, and removed with its session (PLAN K8)."""
from __future__ import annotations

import base64
from types import SimpleNamespace

from test_server import setup as setup  # noqa: PLC0414

from nexus import artifacts
from nexus.mcp import bridge

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40


def _result(*items):
    return SimpleNamespace(content=list(items), structured_content=None, is_error=False)


def _image(raw=PNG, mime="image/png"):
    return SimpleNamespace(type="image", data=base64.b64encode(raw).decode(), mime_type=mime)


def test_bridge_names_the_artifact_without_claiming_what_the_model_sees():
    saved = []
    text, _ = bridge.convert_result(_result(_image()), server="s", tool="shot",
                                    save=lambda data, mime, kind: saved.append((mime, kind)) or "art_1")
    assert saved == [("image/png", "image")] and "artifact art_1" in text and "cannot" not in text
    text, _ = bridge.convert_result(_result(_image()), server="s", tool="shot")
    assert "not stored" in text
    bad = bridge.convert_result(_result(_image()), server="s", tool="shot",
                                save=lambda *a: (_ for _ in ()).throw(ValueError("artifact exceeds 1 bytes")))[0]
    assert "not stored: artifact exceeds 1 bytes" in bad


async def test_artifact_is_served_inert_and_deleted_with_its_session(setup):
    rt, client, _provider, workspace = setup
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    other = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    first = rt.save_artifact(sid, base64.b64encode(PNG).decode(), "image/png; charset=x", name="a.png", source="test")
    twin = rt.save_artifact(other, PNG, "text/html<script>", name="b", source="test")  # same bytes, hostile type
    assert len(list(artifacts.directory().iterdir())) == 1  # content-addressed
    served = await client.get(f"/api/artifact/{first}")
    assert served.content == PNG and served.headers["content-type"] == "image/png"
    assert served.headers["content-disposition"] == "attachment" and served.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(f"/api/artifact/{twin}")).headers["content-type"] == "application/octet-stream"
    assert [a["id"] for a in (await client.get(f"/api/session/{sid}/artifact")).json()] == [first]
    events = (await client.get(f"/api/session/{sid}/event")).json()["events"]
    assert any(e["type"] == "artifact.added" and e["data"]["id"] == first for e in events)
    await client.delete(f"/api/session/{sid}")
    assert (await client.get(f"/api/artifact/{first}")).status_code == 404
    assert (await client.get(f"/api/artifact/{twin}")).content == PNG  # still named by the other session
    await client.delete(f"/api/session/{other}")
    assert list(artifacts.directory().iterdir()) == []


def test_bad_media_is_refused():
    for data in ("%%%", ""):
        try:
            artifacts.write_blob(artifacts.decode(data))
        except ValueError:
            continue
        raise AssertionError("accepted bad media")
