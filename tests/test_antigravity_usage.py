"""Offline tests for the Antigravity row in the Providers usage panel (fake ``agy`` only)."""
from __future__ import annotations

import os
import stat
import sys
from types import SimpleNamespace

import httpx

from nexus.host_support.provider_usage import providers_usage
from nexus.model.providers.antigravity_cli import AntigravityCli


class _Off:
    async def status(self):
        return False


def _runtime(**overrides):
    values = dict(
        workspace=None, _environ={},
        _codex_auth_factory=lambda **_: _Off(), _copilot_auth_factory=lambda **_: _Off(),
        _api_key_auth_factory=lambda provider, **_: _Off(), _claude_auth_factory=lambda **_: _Off(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def _usage(runtime):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        return await providers_usage(runtime, client)


def _fake_agy(tmp_path, log, usage: str | None = None, exit_code: int = 0):
    path = tmp_path / "agy"
    usage_literal = repr(usage or "")
    path.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['-p', '/usage']:\n"
        f"    sys.stdout.write({usage_literal})\n    sys.exit({exit_code})\n"
        "sys.exit(2)\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t75%\t2026-10-21T03:20:08Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t12.5%\t2026-10-14T08:20:08Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-21T03:20:08Z\n"
)


async def test_row_fetches_built_in_usage_with_configured_fake_cli(tmp_path):
    log = tmp_path / "calls.log"
    exe = _fake_agy(tmp_path, log, USAGE)
    runtime = _runtime(_antigravity_cli_factory=lambda **_: AntigravityCli(exe, {"PATH": os.environ.get("PATH", "")}))
    result = await _usage(runtime)
    [row] = result["providers"]
    assert row["id"] == "antigravity" and row["source"] == "agy CLI · /usage"
    assert row["error"] == "" and row["notes"] == [] and row["plan"] == ""
    assert [w["label"] for w in row["windows"]] == [
        "Gemini Models · Weekly", "Gemini Models · 5-hour", "Claude and GPT models · Weekly"]
    assert [w["used_percent"] for w in row["windows"]] == [25.0, 87.5, 0.0]
    assert [w["period_seconds"] for w in row["windows"]] == [604800, 18000, 604800]
    assert [w["kind"] for w in row["windows"]] == ["weekly", "five_hour", "weekly"]
    assert row["windows"][0]["resets_at"] == 1792552808
    assert log.read_text().split("\n") == ["-p /usage", ""]
    assert "Antigravity" not in result["not_connected"]


async def test_usage_parse_notes_do_not_fabricate_counters(tmp_path):
    log = tmp_path / "calls.log"
    exe = _fake_agy(tmp_path, log, "Group\tDaily\t1%\t2026-10-21T03:20:08Z\nGroup\tWeekly Limit Remaining\t101%\tbad\n")
    runtime = _runtime(_antigravity_cli_factory=lambda **_: AntigravityCli(exe, {"PATH": os.environ.get("PATH", "")}))
    [row] = (await _usage(runtime))["providers"]
    assert row["windows"] == []
    assert len(row["notes"]) == 2
    assert row["error"] == ""


async def test_usage_failure_is_unavailable_row(tmp_path):
    log = tmp_path / "calls.log"
    exe = _fake_agy(tmp_path, log, "nope", exit_code=7)
    runtime = _runtime(_antigravity_cli_factory=lambda **_: AntigravityCli(exe, {"PATH": os.environ.get("PATH", "")}))
    [row] = (await _usage(runtime))["providers"]
    assert row["id"] == "antigravity" and row["windows"] == [] and row["error"]
    assert log.read_text().split("\n") == ["-p /usage", ""]


async def test_missing_cli_is_an_honest_row_error(tmp_path):
    runtime = _runtime(_antigravity_cli_factory=lambda **_: AntigravityCli(None, {"PATH": str(tmp_path)}))
    [row] = (await _usage(runtime))["providers"]
    assert row["id"] == "antigravity" and row["windows"] == [] and "not found" in row["error"]


async def test_no_row_unless_configured():
    assert (await _usage(_runtime()))["providers"] == []


async def test_configured_section_adds_the_row(tmp_path):
    config = SimpleNamespace(v2=SimpleNamespace(providers={"antigravity": SimpleNamespace(executable=None)}))
    runtime = _runtime(_config=config, _environ={"PATH": str(tmp_path)})
    [row] = (await _usage(runtime))["providers"]
    assert row["id"] == "antigravity" and "not found" in row["error"]
