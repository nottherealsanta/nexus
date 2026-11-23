"""The `firecrawl` tool and its server process, against a stand-in `firecrawl-local`."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from nexus import firecrawl, runtime
from nexus.tools import ToolContext, ToolError
from nexus.tools.firecrawl import END
from nexus.tools.firecrawl import run as fc

FIXTURE = Path(__file__).parent / "fixtures" / "fake_firecrawl.py"


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(tmp_path)


@pytest.fixture
async def fake(tmp_path, monkeypatch):
    """A `firecrawl-local` that is a script; `record` lists every request the server saw."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    script = tmp_path / "firecrawl-local"
    script.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FIXTURE))} \"$@\"\n")
    script.chmod(0o755)
    record = home / "firecrawl" / "state" / "record.jsonl"
    monkeypatch.setattr(firecrawl, "binary", lambda: str(script))
    monkeypatch.setattr("nexus.tools.firecrawl.POLL_S", 0.01)
    try:
        yield lambda: [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    finally:
        await firecrawl.close()
        log = home / "logs" / "firecrawl.log"
        if log.exists():
            print(f"Fake Firecrawl subprocess log:\n{log.read_text(errors='replace')}")


async def test_scrape_is_fenced_and_header_has_no_query(fake, ctx):
    out = await fc({"action": "scrape", "url": "https://x.example/doc.pdf?key=secret#frag"}, ctx)
    head, _, rest = out.partition("<<< BEGIN UNTRUSTED WEB CONTENT >>>")
    assert "Source: https://x.example/doc.pdf" in head and "secret" not in head and "frag" not in head
    assert "Final URL: https://x.example/doc.pdf\n" in head and "Content-Type: application/pdf" in head
    assert "Title: The Doc" in head and "Engine: local-pdf" in head and "Warning: needs a browser" in head
    assert out.count(END) == 1 and out.rstrip().endswith(END)  # the forged marker in the page is defused
    assert "line 5" in rest


async def test_scrape_pages_by_lines(fake, ctx):
    out = await fc({"action": "scrape", "url": "https://x.example/d", "offset": 3, "limit": 2}, ctx)
    assert "line 1" in out or "line 2" in out
    out = await fc({"action": "scrape", "url": "https://x.example/d", "offset": 3, "limit": 2}, ctx)
    assert "[showing lines 3–4 of 8; use offset=5 to read on]" in out
    with pytest.raises(ToolError, match="past the end"):
        await fc({"action": "scrape", "url": "https://x.example/d", "offset": 99}, ctx)


async def test_server_is_started_once_private_and_without_the_cloud(fake, ctx):
    await fc({"action": "scrape", "url": "https://x.example/a"}, ctx)
    await fc({"action": "map", "url": "https://x.example/"}, ctx)
    seen = [r for r in fake() if r["path"] != "/health"]
    assert len(seen) == 2 and seen[0]["env_cloud"] == "off"
    assert all(r["auth"] and r["auth"].startswith("Bearer ") and len(r["auth"]) > 30 for r in seen)
    argv = seen[0]["argv"]
    assert argv[0] == "serve" and argv[argv.index("--host") + 1] == "127.0.0.1" and "--allow-private" not in argv
    assert not {"FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "ANTHROPIC_API_KEY"} & set(seen[0]["env_keys"])
    assert seen[0]["body"]["onlyMainContent"] is True and seen[0]["body"]["formats"] == ["markdown"]


async def test_server_restarts_after_it_died(fake, ctx):
    await fc({"action": "scrape", "url": "https://x.example/a"}, ctx)
    first = firecrawl.server().process
    first.kill()
    await first.wait()
    out = await fc({"action": "scrape", "url": "https://x.example/a"}, ctx)
    assert "The Doc" in out and firecrawl.server().process is not first


async def test_close_stops_the_process(fake, ctx):
    await fc({"action": "scrape", "url": "https://x.example/a"}, ctx)
    process = firecrawl.server().process
    await firecrawl.close()
    assert process.returncode is not None


async def test_refused_targets_and_http_errors(fake, ctx):
    with pytest.raises(ToolError, match="only public internet addresses"):
        await fc({"action": "scrape", "url": "https://blocked.example/"}, ctx)
    with pytest.raises(ToolError, match="HTTP 404"):
        await fc({"action": "scrape", "url": "https://x.example/404"}, ctx)
    before = len(fake())
    for url in ["ftp://x.example/", "https://u:p@x.example/", "https://x.example/a b", ""]:
        with pytest.raises(ToolError):
            await fc({"action": "scrape", "url": url}, ctx)
    assert len(fake()) == before  # a malformed URL never reaches the server


async def test_map_lists_clean_urls(fake, ctx):
    out = await fc({"action": "map", "url": "https://x.example/", "limit": 3, "search": "docs"}, ctx)
    assert "URLs: 3" in out and "1. https://a.example/p0" in out and "3. https://a.example/p2" in out
    assert "javascript" not in out and out.count("p0") == 1  # a bad scheme and a duplicate are dropped
    assert "(limit 3 reached" not in out or "narrow" in out
    sent = next(r for r in fake() if r["path"] == "/v2/map")["body"]
    assert sent == {"url": "https://x.example/", "limit": 3, "search": "docs"}


async def test_crawl_waits_and_reads_every_page(fake, ctx):
    out = await fc({"action": "crawl", "url": "https://x.example/", "limit": 5, "max_depth": 2,
                    "include_paths": ["^/docs"], "entire_domain": True}, ctx)
    assert "Pages: 3 (limit 5)" in out and "Pages that failed: 1" in out
    assert "### 1. P1" in out and "URL: https://a.example/2" in out and "page 3" in out
    sent = next(r for r in fake() if r["path"] == "/v2/crawl" and r["method"] == "POST")["body"]
    assert sent["limit"] == 5 and sent["maxDiscoveryDepth"] == 2 and sent["crawlEntireDomain"] is True
    assert sent["includePaths"] == ["^/docs"] and sent["scrapeOptions"]["onlyMainContent"] is True
    assert len(out) <= 30_000


async def test_crawl_stops_a_slow_job_and_keeps_what_it_read(fake, ctx, monkeypatch):
    monkeypatch.setattr("nexus.tools.firecrawl.CRAWL_TIMEOUT_S", 0.2)
    out = await fc({"action": "crawl", "url": "https://slow.example/"}, ctx)
    assert "the crawl was stopped after 0.2 seconds" in out and "page 1" in out
    assert any(r["method"] == "DELETE" for r in fake())


async def test_crawl_output_is_bounded_and_says_what_it_left_out(fake, ctx, monkeypatch):
    big = [{"markdown": "y" * 20_000, "metadata": {"title": f"T{i}", "url": f"https://a.example/{i}"}} for i in range(80)]
    from nexus.tools import firecrawl as tool
    out = tool._render_crawl("https://a.example/", big, 0, "completed", False, 80)
    assert len(out) <= 30_000 and "more pages were read but do not fit" in out and "[page cut at" in out


async def test_argument_validation(fake, ctx):
    bad = [{}, {"action": "search", "url": "https://x.example/"}, {"action": "crawl", "url": "https://x.example/", "limit": 51},
           {"action": "map", "url": "https://x.example/", "limit": 0}, {"action": "crawl", "url": "https://x.example/", "max_depth": 11},
           {"action": "crawl", "url": "https://x.example/", "include_paths": ["("]},
           {"action": "crawl", "url": "https://x.example/", "exclude_paths": "x"},
           {"action": "scrape", "url": "https://x.example/", "full_page": "yes"}]
    for args in bad:
        with pytest.raises(ToolError):
            await fc(args, ctx)


async def test_not_installed_tells_the_model_not_to_retry(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(firecrawl, "binary", lambda: None)
    with pytest.raises(ToolError, match="not installed.*Do not install it yourself or retry.*web_fetch.*nexus firecrawl init"):
        await fc({"action": "scrape", "url": "https://x.example/"}, ctx)
    assert firecrawl.status()["state"] == "missing"
    from nexus import doctor
    assert doctor.check_firecrawl()["status"] == "warn"


def test_status_reports_a_working_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    script = tmp_path / "firecrawl-local"
    script.write_text("#!/bin/sh\necho firecrawl-local 0.1.1\n")
    script.chmod(0o755)
    monkeypatch.setattr(firecrawl, "binary", lambda: str(script))
    assert firecrawl.status() == {"state": "ready", "path": str(script), "version": "firecrawl-local 0.1.1", "detail": None}
    script.write_text("#!/bin/sh\necho firecrawl-local 0.0.9\n")
    stale = firecrawl.status()
    assert stale["state"] == "ready" and "pins" in stale["detail"]
    script.write_text("#!/bin/sh\nexit 3\n")
    assert firecrawl.status()["state"] == "broken"


def test_the_tool_is_default_asks_in_ask_sessions_and_is_not_auto_approved():
    from nexus import permissions
    from nexus.tools import DEFAULT_TOOLS
    assert "firecrawl" in [t.spec.name for t in DEFAULT_TOOLS] and "firecrawl" not in runtime.AUTO_APPROVED
    assert permissions.call_keys("firecrawl", {"action": "map", "url": "https://Docs.Python.org/3/?q=1#x"}, "/ws") == \
        ["https://docs.python.org/3/"]


def test_binary_is_found_in_the_installer_prefix_without_path(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("PREFIX", str(tmp_path / "prefix"))
    assert firecrawl.binary() is None
    installed = tmp_path / "prefix" / "bin" / "firecrawl-local"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n")
    assert firecrawl.binary() is None  # not executable
    installed.chmod(0o755)
    assert firecrawl.binary() == str(installed)
    own = tmp_path / "home" / "firecrawl" / "bin" / "firecrawl-local"
    own.parent.mkdir(parents=True)
    own.write_text("#!/bin/sh\n")
    own.chmod(0o755)
    assert firecrawl.binary() == str(own)  # `nexus firecrawl init` wins
