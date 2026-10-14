"""``nexus gateway``: parser registration, lazy imports and the local-only commands."""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from nexus import cli
from nexus.gateway import cli as gateway_cli
from nexus.gateway.store import SCOPES, GatewayStore

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def gateway_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NEXUS_DEV", raising=False)
    return tmp_path / "home" / "gateway"


@pytest.fixture
def workspace(tmp_path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def gateway(*argv: str) -> tuple[int, str, str]:
    args = cli.build_parser().parse_args(["gateway", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = gateway_cli.run(args, out, err)
    return code, out.getvalue(), err.getvalue()


# -- registration -------------------------------------------------------------------


def test_gateway_is_listed_in_the_cli_help():
    text = cli.build_parser().format_help()
    assert "gateway" in text and "docs/gateway.md" in text


def test_every_gateway_action_is_registered_with_help(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["gateway", "--help"])
    assert raised.value.code == 0
    text = capsys.readouterr().out
    for action in ("serve", "allow", "disallow", "pair", "devices", "revoke", "status"):
        assert action in text


def test_an_action_is_required():
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["gateway"])
    assert raised.value.code == 2


def test_parser_accepts_each_actions_flags(tmp_path):
    parser = cli.build_parser()
    serve = parser.parse_args(["gateway", "serve", "--port", "9100", "--public-url", "https://h.ts.net"])
    assert (serve.command, serve.gateway_action, serve.port, serve.public_url) == ("gateway", "serve", 9100, "https://h.ts.net")
    default = parser.parse_args(["gateway", "serve"])
    assert default.port == gateway_cli.DEFAULT_PORT == 8765 and default.public_url == ""

    pair = parser.parse_args([
        "gateway", "pair", "--name", "phone", "--scopes", "read,live", "--ttl-days", "7",
        "--host", "a" * 16, "--host", "b" * 16, "--code-ttl", "60",
    ])
    assert (pair.name, pair.scopes, pair.ttl_days, pair.host, pair.code_ttl) == ("phone", "read,live", 7, ["a" * 16, "b" * 16], 60.0)
    bare = parser.parse_args(["gateway", "pair"])
    assert bare.scopes == ",".join(SCOPES) and bare.host is None and bare.code_ttl == 120.0 and bare.ttl_days == 90

    assert parser.parse_args(["gateway", "allow", str(tmp_path)]).path == str(tmp_path)
    assert parser.parse_args(["gateway", "disallow", "x"]).target == "x"
    assert parser.parse_args(["gateway", "revoke", "dev_1"]).device_id == "dev_1"
    with pytest.raises(SystemExit):
        parser.parse_args(["gateway", "serve", "--host", "0.0.0.0"])  # no listen-address option exists


def test_building_the_parser_does_not_load_the_server_or_http_client():
    script = (
        "import sys\n"
        "from nexus import cli\n"
        "cli.build_parser()\n"
        "bad = [m for m in ('nexus.gateway.runner', 'nexus.gateway.app', 'nexus.gateway.httpd', "
        "'nexus.gateway.upstream') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# -- dispatch through main ------------------------------------------------------------


def test_main_dispatches_gateway_commands(workspace, capsys):
    assert cli.main(["gateway", "allow", str(workspace)]) == 0
    assert "allowed" in capsys.readouterr().out
    assert cli.main(["gateway", "status"]) == 0
    assert str(workspace.resolve()) in capsys.readouterr().out


def test_main_serve_calls_the_runner_with_the_flags(monkeypatch):
    seen = {}

    async def fake_run_gateway(store, **kwargs):
        seen["store"] = store
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("nexus.gateway.runner.run_gateway", fake_run_gateway)
    assert cli.main(["gateway", "serve", "--port", "9333", "--public-url", "https://h.ts.net"]) == 0
    assert isinstance(seen["store"], GatewayStore)
    assert (seen["port"], seen["public_url"]) == (9333, "https://h.ts.net")


def test_serve_returns_the_runner_exit_code(monkeypatch):
    async def failing(store, **kwargs):
        return 1

    monkeypatch.setattr("nexus.gateway.runner.run_gateway", failing)
    assert cli.main(["gateway", "serve"]) == 1


# -- commands ---------------------------------------------------------------------------


def test_allow_pair_devices_revoke_disallow_round_trip(workspace, gateway_home):
    code, out, err = gateway("allow", str(workspace))
    assert code == 0 and err == ""
    host_id = out.split()[1]
    assert re.fullmatch(r"[0-9a-f]{16}", host_id)

    code, out, err = gateway("status")
    assert code == 0 and "serving: no" in out and "devices: 0  pending codes: 0" in out
    assert f"host {host_id}" in out

    code, out, err = gateway("pair", "--name", "phone", "--scopes", "read,prompt")
    assert code == 0 and err == ""
    pairing = re.search(r"pairing code: ([A-Z2-7-]+)", out)
    assert pairing
    assert "valid for 1" in out and "single use" in out  # 119 or 120 seconds
    assert "scopes: read,prompt" in out
    assert "live" not in out.split("scopes:")[1].split("\n")[0]
    assert "microphone" not in out  # the OpenAI consent note appears only with the live scope
    assert "pending codes: 1" in gateway("status")[1]

    store = GatewayStore()
    device, token = store.redeem(pairing.group(1), "Phone")
    code, out, _ = gateway("devices")
    assert code == 0
    assert device.device_id in out and "Phone" in out and "scopes=read,prompt" in out and host_id in out
    assert token not in out

    code, out, err = gateway("revoke", device.device_id)
    assert code == 0 and out == f"revoked {device.device_id}\n"
    code, out, err = gateway("revoke", device.device_id)
    assert code == 1 and "no such device" in err and out == ""
    assert gateway("devices")[1] == ""

    code, out, err = gateway("disallow", host_id)
    assert code == 0 and out.startswith("disallowed ")
    code, out, err = gateway("disallow", host_id)
    assert code == 1 and "no such allowed workspace" in err
    assert "no workspace is allowed" in gateway("status")[1]


def test_pair_with_live_scope_discloses_the_openai_consent(workspace):
    gateway("allow", str(workspace))
    code, out, _ = gateway("pair")
    assert code == 0
    assert "live" in out and "microphone" in out and "OpenAI" in out
    assert "not stored" in out  # the code is shown once


def test_pair_requires_an_allowed_workspace(gateway_home):
    code, out, err = gateway("pair")
    assert code == 1 and out == ""
    assert "nexus gateway allow" in err
    assert GatewayStore(gateway_home).pending_count() == 0


def test_pair_rejects_bad_scopes_ttl_and_hosts(workspace):
    gateway("allow", str(workspace))
    for argv in (
        ("--scopes", "admin"),
        ("--scopes", ""),
        ("--ttl-days", "0"),
        ("--ttl-days", "91"),
        ("--code-ttl", "0"),
        ("--code-ttl", "601"),
        ("--host", "f" * 16),
    ):
        code, out, err = gateway("pair", *argv)
        assert code == 1 and out == "" and err.startswith("Error: "), argv


def test_allow_rejects_missing_paths_and_files(tmp_path):
    code, out, err = gateway("allow", str(tmp_path / "missing"))
    assert code == 1 and "does not exist" in err
    plain = tmp_path / "f.txt"
    plain.write_text("x")
    code, out, err = gateway("allow", str(plain))
    assert code == 1 and "not a directory" in err


def test_status_reports_a_serving_gateway(workspace):
    gateway("allow", str(workspace))
    fd = GatewayStore().acquire_serve_lock()
    try:
        assert "serving: yes" in gateway("status")[1]
    finally:
        os.close(fd)
    assert "serving: no" in gateway("status")[1]


def test_unknown_action_is_a_programming_error():
    args = cli.build_parser().parse_args(["gateway", "status"])
    args.gateway_action = "bogus"
    with pytest.raises(ValueError):
        gateway_cli.run(args, io.StringIO(), io.StringIO())
