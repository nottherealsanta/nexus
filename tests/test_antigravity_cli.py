"""Offline tests for the Antigravity CLI helper and its provider-auth handling (fake ``agy`` only)."""
from __future__ import annotations

import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host_support import provider_auth
from nexus.model.providers.antigravity_cli import (
    AntigravityCli,
    AntigravityError,
    clean_text,
    parse_models,
    parse_usage,
    resolve_executable,
)


def _fake(tmp_path, body: str, name: str = "agy") -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _env(tmp_path, **extra) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **extra}


MODELS = "gemini-3.8-flash-high\nclaude-sonnet-4-6\n\nclaude-sonnet-4-6\nnot a model line\n"
USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t75%\t2026-10-21T03:20:08Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t12.5%\t2026-10-14T08:20:08Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-21T03:20:08Z\n"
)
GOOD = (
    "log = os.environ.get('AGY_LOG')\n"
    "if log:\n    open(log, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    "if sys.argv[1:] == ['--version']:\n    print('agy 1.3.4')\n"
    "elif sys.argv[1:] == ['models']:\n    sys.stdout.write(%r)\n"
    "elif sys.argv[1:] == ['-p', '/usage']:\n    sys.stdout.write(%r)\n"
    "else:\n    sys.exit(2)\n" % (MODELS, USAGE)
)


def test_parse_models_keeps_single_token_ids_once():
    assert parse_models(MODELS) == [("gemini-3.8-flash-high",) * 2, ("claude-sonnet-4-6",) * 2]
    assert parse_models("\x1b[1mgpt-oss-120b-medium\x1b[0m\n; rm -rf /\n") == [("gpt-oss-120b-medium",) * 2]


def test_parse_usage_inverts_remaining_and_keeps_resets():
    parsed = parse_usage(USAGE)
    assert [w["label"] for w in parsed["windows"]] == [
        "Gemini Models · Weekly", "Gemini Models · 5-hour", "Claude and GPT models · Weekly"]
    assert [w["used_percent"] for w in parsed["windows"]] == [25.0, 87.5, 0.0]
    assert [w["period_seconds"] for w in parsed["windows"]] == [604800, 18000, 604800]
    assert parsed["windows"][0]["resets_at"] == 1792552808


def test_parse_usage_rejects_invalid_rows_without_fabricating_counters():
    parsed = parse_usage("Group\tDaily\t1%\t2026-10-21T03:20:08Z\nGroup\tWeekly Limit Remaining\t101%\tbad\n")
    assert parsed["windows"] == []
    assert len(parsed["notes"]) == 2


def test_clean_text_strips_ansi_and_redacts():
    text = clean_text("\x1b[31mfailed\x1b[0m Bearer abcdefghijklmnop1234567890 sk-live-supersecret123456\n" + "x" * 900)
    assert "\x1b" not in text and "supersecret" not in text and len(text) <= 300


def test_resolve_executable_refuses_untrusted_overrides(tmp_path):
    exe = _fake(tmp_path, "pass")
    env = _env(tmp_path)
    assert resolve_executable(exe, env) == exe
    assert resolve_executable(None, {"PATH": str(tmp_path)}) == exe
    assert resolve_executable("agy", {"PATH": str(tmp_path)}) == exe
    for bad in ("relative/agy", "./agy", "evil", f"{tmp_path}/../{tmp_path.name}/agy", f"{exe}\n", str(tmp_path / "missing")):
        assert resolve_executable(bad, env) is None
    (tmp_path / "plain").write_text("x")
    assert resolve_executable(str(tmp_path / "plain"), env) is None


async def test_models_version_and_status_with_fake_agy(tmp_path):
    log = tmp_path / "calls.log"
    # The child env is scrubbed (no AGY_LOG reaches agy), so bake the log path into the fake.
    cli = AntigravityCli(_fake(tmp_path, GOOD.replace("os.environ.get('AGY_LOG')", repr(str(log)))), _env(tmp_path))
    assert await cli.version() == "1.3.4"
    assert await cli.models() == [("gemini-3.8-flash-high",) * 2, ("claude-sonnet-4-6",) * 2]
    usage = await cli.usage()
    assert usage["windows"][0]["label"] == "Gemini Models · Weekly"
    assert usage["windows"][0]["used_percent"] == 25.0
    assert await cli.status() is False
    view = await cli.status_view()
    assert view["cli_present"] is True and view["auth_verified"] is False and view["version"] == "1.3.4"
    assert "agy" in view["message"]
    assert set(log.read_text().split("\n")) <= {"--version", "models", "-p /usage", ""}


async def test_child_env_is_minimal_and_disables_auto_update(tmp_path):
    out = tmp_path / "env.txt"
    body = (f"open({str(out)!r}, 'w').write(repr((os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE'),"
            " os.environ.get('OPENAI_API_KEY'), os.environ.get('ANTHROPIC_API_KEY'))))\nprint('1.0.0')")
    cli = AntigravityCli(_fake(tmp_path, body), _env(tmp_path, OPENAI_API_KEY="sk-x", ANTHROPIC_API_KEY="sk-y"))
    await cli.version()
    assert out.read_text() == "('true', None, None)"


async def test_missing_cli_is_honest(tmp_path):
    cli = AntigravityCli(None, {"PATH": str(tmp_path)})
    view = await cli.status_view()
    assert view["cli_present"] is False and view["auth_verified"] is False
    with pytest.raises(AntigravityError, match="not found"):
        await cli.models()


async def test_failure_redacts_stderr(tmp_path):
    body = "sys.stderr.write('\\x1b[31mboom sk-live-supersecret123456\\x1b[0m')\nsys.exit(3)"
    cli = AntigravityCli(_fake(tmp_path, body), _env(tmp_path))
    with pytest.raises(AntigravityError) as info:
        await cli.models()
    assert "supersecret" not in str(info.value) and "\x1b" not in str(info.value) and "boom" in str(info.value)


async def test_oversized_output_is_rejected(tmp_path):
    cli = AntigravityCli(_fake(tmp_path, "sys.stdout.write('a' * 200000)"), _env(tmp_path))
    with pytest.raises(AntigravityError, match="too much"):
        await cli.models()


async def test_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    from nexus.model.providers import antigravity_cli

    pidfile = tmp_path / "child.pid"
    body = (f"import subprocess, time\nc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid))\ntime.sleep(60)")
    monkeypatch.setattr(antigravity_cli, "_PROBE_TIMEOUT", 1.0)
    cli = AntigravityCli(_fake(tmp_path, body), _env(tmp_path))
    with pytest.raises(AntigravityError, match="in time"):
        await cli.models()
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived")


async def test_logout_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="never signs"):
        await AntigravityCli(None, {}).logout()


# --- provider auth ---------------------------------------------------------------------------


class _Fake:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def status(self):
        return False

    async def status_view(self):
        return {"cli_present": True, "version": "1.3.4", "auth_verified": False, "message": "agy is installed"}


async def _false():
    return False


def _runtime(**overrides):
    off = type("Off", (), {"status": lambda self: _false()})
    values = dict(
        _environ={}, _antigravity_cli_factory=_Fake,
        _codex_auth_factory=lambda **_: off(), _copilot_auth_factory=lambda **_: off(),
        _api_key_auth_factory=lambda provider, **_: off(), _xai_auth_factory=lambda **_: off(),
        _claude_auth_factory=lambda **_: off(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_login_cli_method_is_manual_with_no_url_or_credential():
    runtime = _runtime()
    result = await provider_auth.provider_login(runtime, "antigravity", "cli")
    assert result["status"] == "manual" and "agy" in result["message"]
    assert result["url"] == "" and result["user_code"] == "" and result["code_entry"] is False
    assert (await provider_auth.provider_login(runtime, "antigravity"))["status"] == "manual"
    assert (await provider_auth.provider_login_poll(runtime, result["login_id"]))["status"] == "manual"
    with pytest.raises(ConfigError):
        await provider_auth.provider_login(runtime, "antigravity", "browser")
    with pytest.raises(ConfigError, match="not waiting"):
        await provider_auth.provider_login_code(runtime, result["login_id"], "abcdefgh")


async def test_logout_is_never_performed():
    with pytest.raises(ConfigError, match="never signs"):
        await provider_auth.provider_logout(_runtime(), "antigravity")


async def test_status_row_reports_presence_and_auth_separately():
    runtime = _runtime()
    result = await provider_auth.providers_status(runtime)
    row = next(row for row in result["providers"] if row["id"] == "antigravity")
    assert row["methods"] == ["cli"] and row["connected"] is False and row["can_logout"] is False
    assert row["cli_present"] is True and row["auth_verified"] is False
    assert row["detail"] == "agy is installed"
    # Recheck is just another status call; it reflects the CLI's current state.
    class Gone(_Fake):
        async def status_view(self):
            return {"cli_present": False, "version": "", "auth_verified": False, "message": "agy was not found"}

    again = await provider_auth.providers_status(_runtime(_antigravity_cli_factory=Gone))
    row = next(row for row in again["providers"] if row["id"] == "antigravity")
    assert row["cli_present"] is False and row["auth_verified"] is False
