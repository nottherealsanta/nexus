"""Offline regression tests for the standalone benchmark harness."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_BENCH_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "bench.py"
_BENCH_SPEC = importlib.util.spec_from_file_location(
    "nexus_benchmark_bench", _BENCH_PATH
)
assert _BENCH_SPEC is not None and _BENCH_SPEC.loader is not None
bench = importlib.util.module_from_spec(_BENCH_SPEC)
_BENCH_SPEC.loader.exec_module(bench)


@pytest.fixture
def benchmark_root(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    root = artifacts / "benchmark"
    monkeypatch.setattr(bench, "ARTIFACTS", artifacts)
    monkeypatch.setattr(bench, "ROOT", root)
    return root


def _setup(monkeypatch, name=None):
    """Exercise setup filesystem behavior without depending on local config."""

    def write_config(_name, workspace, *, allow_shell=False):
        workspace.joinpath("nexus.toml").write_bytes(
            (
                "# benchmark config\nallow_shell = " + str(allow_shell).lower() + "\n"
            ).encode()
        )

    def write_agent(workspace):
        agent = workspace / ".nexus" / "agents"
        agent.mkdir(parents=True)
        (agent / "general.md").write_bytes(b"benchmark agent\n")

    monkeypatch.setattr(bench, "_write_workspace_config", write_config)
    monkeypatch.setattr(bench, "_write_root_agent", write_agent)
    argv = ["setup"] + ([] if name is None else [name])
    return bench.main(argv)


def _valid_shell_events():
    call_id = "call-1"
    return [
        {"type": "tool.requested", "data": {"call_id": call_id, "tool": "bash"}},
        {
            "type": "tool.input",
            "data": {"call_id": call_id, "input": {"command": bench.SHELL_COMMAND}},
        },
        {"type": "tool.started", "data": {"call_id": call_id, "tool": "bash"}},
        {
            "type": "tool.completed",
            "data": {
                "call_id": call_id,
                "tool": "bash",
                "executed": True,
                "is_error": False,
            },
        },
        {
            "type": "tool.result",
            "data": {
                "tool_use_id": call_id,
                "content": [
                    {"text": "BENCHMARK_BASH_RAN\nexit_code: 0\nstatus: completed"}
                ],
            },
        },
    ]


def _write_events(run_dir: Path, events):
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def test_list_cli_setup_check_and_reset_both_scenarios(
    benchmark_root, monkeypatch, capsys
):
    assert bench.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert all(name in listing for name in bench.SCENARIOS)

    assert _setup(monkeypatch) == 0
    for name, seed_name in (
        ("file-edit", "input.txt"),
        ("shell-command", "instructions.txt"),
    ):
        scenario = benchmark_root / name
        workspace = scenario / "workspace"
        assert (scenario / bench.MARKER).read_bytes() == bench.MARKER_CONTENT
        assert (workspace / bench.MARKER).read_bytes() == bench.MARKER_CONTENT
        assert (scenario / "run" / bench.MARKER).read_bytes() == bench.MARKER_CONTENT
        seed = (
            Path(bench.__file__).parent / "scenarios" / name / "workspace" / seed_name
        ).read_bytes()
        assert (workspace / seed_name).read_bytes() == seed
        assert (workspace / "nexus.toml").is_file()
        assert (
            workspace / ".nexus" / "agents" / "general.md"
        ).read_bytes() == b"benchmark agent\n"

    # A newly-created setup does not grade until the exact outputs/evidence exist.
    assert bench.main(["check"]) == 1
    (benchmark_root / "file-edit" / "workspace" / "result.txt").write_bytes(
        bench._expected("file-edit")
    )
    shell_workspace = benchmark_root / "shell-command" / "workspace"
    (shell_workspace / "result.txt").write_bytes(bench._expected("shell-command"))
    _write_events(benchmark_root / "shell-command" / "run", _valid_shell_events())
    assert bench.main(["check"]) == 0

    neighbor = benchmark_root / "unrelated-neighbor"
    neighbor.mkdir()
    (neighbor / "keep.txt").write_bytes(b"leave me\n")
    assert bench.main(["reset", "file-edit"]) == 0
    assert not (benchmark_root / "file-edit").exists()
    assert (benchmark_root / "shell-command").exists()
    assert (neighbor / "keep.txt").read_bytes() == b"leave me\n"
    assert bench.main(["reset"]) == 0
    assert not (benchmark_root / "shell-command").exists()
    assert (neighbor / "keep.txt").read_bytes() == b"leave me\n"


@pytest.mark.parametrize("name", bench.SCENARIOS)
def test_grade_requires_expected_bytes_and_detects_tampering(
    benchmark_root, monkeypatch, name
):
    _setup(monkeypatch, name)
    workspace = benchmark_root / name / "workspace"
    output = workspace / "result.txt"
    assert not bench._grade(name, workspace)["passed"]

    output.write_bytes(bench._expected(name))
    if name == "shell-command":
        _write_events(benchmark_root / name / "run", _valid_shell_events())
    assert bench._grade(name, workspace)["passed"]

    # Same visible text with a byte-level difference must not pass.
    output.write_bytes(bench._expected(name).rstrip(b"\n"))
    assert not bench._grade(name, workspace)["passed"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda events: events[1:],  # requested command but no start/completion
        lambda events: [
            *events[:4],
            {
                **events[4],
                "data": {
                    **events[4]["data"],
                    "content": [{"text": "exit_code: 0 status: completed"}],
                },
            },
        ],
        lambda events: [
            *events[:3],
            {**events[3], "data": {**events[3]["data"], "executed": False}},
            events[4],
        ],
        lambda events: [
            *events[:4],
            {
                **events[4],
                "data": {
                    **events[4]["data"],
                    "content": [
                        {"text": "BENCHMARK_BASH_RAN exit_code: 1 status: completed"}
                    ],
                },
            },
        ],
    ],
)
def test_shell_grading_requires_command_start_completion_and_success_output(
    benchmark_root, monkeypatch, mutate
):
    _setup(monkeypatch, "shell-command")
    workspace = benchmark_root / "shell-command" / "workspace"
    (workspace / "result.txt").write_bytes(bench._expected("shell-command"))
    run_dir = benchmark_root / "shell-command" / "run"
    _write_events(run_dir, mutate(_valid_shell_events()))
    grade = bench._grade("shell-command", workspace, run_dir)
    assert not grade["passed"]


def test_shell_grading_rejects_multiple_or_unexpected_bash_calls():
    events = _valid_shell_events()
    second_id = "call-2"
    second_call = [
        {"type": "tool.requested", "data": {"call_id": second_id, "tool": "bash"}},
        {
            "type": "tool.input",
            "data": {"call_id": second_id, "input": {"command": "echo other"}},
        },
        {"type": "tool.started", "data": {"call_id": second_id, "tool": "bash"}},
        {
            "type": "tool.completed",
            "data": {
                "call_id": second_id,
                "tool": "bash",
                "executed": True,
                "is_error": False,
            },
        },
    ]
    assert not bench._shell_evidence(events + second_call)[0]
    assert not bench._shell_evidence(
        [
            *events,
            {"type": "tool.requested", "data": {"call_id": second_id, "tool": "read"}},
        ]
    )[0]


def test_shell_evidence_requires_request_and_exact_input():
    events = _valid_shell_events()
    assert bench._shell_evidence(events)[0]
    altered = [dict(event) for event in events]
    altered[1] = {
        **altered[1],
        "data": {
            **altered[1]["data"],
            "input": {"command": bench.SHELL_COMMAND, "env": {"X": "1"}},
        },
    }
    assert not bench._shell_evidence(altered)[0]


def test_jsonl_redaction_preserves_exit_code_and_json_structure(monkeypatch):
    secret = "private-token-value-123456"
    credential = "private-credential-value-987654"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    monkeypatch.setenv("SERVICE_CREDENTIAL", credential)
    raw = (
        json.dumps(
            {
                "type": "tool.result",
                "data": {
                    "content": [
                        {
                            "text": (
                                f"{secret}\n{credential}\nexit_code: 0\nstatus: completed"
                            )
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    redacted = bench._redact_runtime_secrets(raw)
    parsed = json.loads(redacted)
    text = parsed["data"]["content"][0]["text"]
    assert secret not in redacted
    assert credential not in redacted
    assert "exit_code: 0" in text.splitlines()
    assert "status: completed" in text.splitlines()
    substring_raw = json.dumps({"token": secret, "message": f"prefix-{secret}-suffix"})
    substring_redacted = json.loads(bench._redact_runtime_secrets(substring_raw))
    assert substring_redacted["token"] == "***"
    assert substring_redacted["message"] == "prefix-***-suffix"


def test_failed_run_result_does_not_persist_mismatching_output(
    benchmark_root, monkeypatch, capsys
):
    _setup(monkeypatch, "file-edit")
    monkeypatch.setattr(bench, "_preflight_policy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bench, "_stop_workspace_daemon", lambda *_args: None)
    credential = "wrong-file-credential-value-12345"

    class FakeProcess:
        pid = 123
        returncode = 0

        def __init__(self, command, **_kwargs):
            self.workspace = Path(command[4])

        def communicate(self, timeout=None):
            (self.workspace / "result.txt").write_text(credential, encoding="utf-8")
            return b"{}\n", b""

    monkeypatch.setattr(bench.subprocess, "Popen", FakeProcess)
    assert bench.main(["run", "file-edit", "--timeout", "2"]) == 1

    result_path = benchmark_root / "file-edit" / "run" / "result.json"
    result_text = result_path.read_text(encoding="utf-8")
    result = json.loads(result_text)
    assert not result["grading"]["passed"]
    assert "does not exactly match the required contents" in result["grading"]["reason"]
    assert "bytes" in result["grading"]["reason"]
    assert credential not in result_text
    assert credential not in capsys.readouterr().out


def test_workspace_config_refuses_literal_provider_credentials(tmp_path, monkeypatch):
    source = tmp_path / "source.toml"
    source.write_text(
        '[model]\ndefault = "demo"\n[providers.demo]\nkind = "openai"\napi_key = "literal-secret-value"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bench, "REPO", tmp_path)
    from nexus.config import Config

    monkeypatch.setattr(
        Config, "load", staticmethod(lambda _path: SimpleNamespace(source=str(source)))
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(bench.BenchError, match="literal credential"):
        bench._write_workspace_config("file-edit", workspace)
    assert not (workspace / "nexus.toml").exists()


def test_workspace_config_keeps_env_reference_without_resolving_it(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.toml"
    source.write_text(
        '[model]\ndefault = "demo"\n[providers.demo]\nkind = "openai"\napi_key = "${env:DEMO_API_KEY}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bench, "REPO", tmp_path)
    from nexus.config import Config

    monkeypatch.setattr(
        Config, "load", staticmethod(lambda _path: SimpleNamespace(source=str(source)))
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bench._write_workspace_config("file-edit", workspace)
    generated = (workspace / "nexus.toml").read_text(encoding="utf-8")
    assert 'api_key = "${env:DEMO_API_KEY}"' in generated


def test_workspace_config_refuses_literal_provider_environment_values(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.toml"
    source.write_text(
        '[model]\ndefault = "demo"\n[providers.demo]\nkind = "agent"\n'
        '[providers.demo.env]\nSERVICE_TOKEN = "literal-env-secret"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bench, "REPO", tmp_path)
    from nexus.config import Config

    monkeypatch.setattr(
        Config, "load", staticmethod(lambda _path: SimpleNamespace(source=str(source)))
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(bench.BenchError, match="literal credential"):
        bench._write_workspace_config("file-edit", workspace)
    assert not (workspace / "nexus.toml").exists()


@pytest.mark.parametrize(
    ("mode", "allow", "expected_error"),
    [
        ("allow", [], "permissions.mode"),
        ("deny", ["bash", "read(.*)", "write(.*)"], "broad or additional grants"),
    ],
)
def test_preflight_rejects_inherited_permissive_or_broad_policy(
    tmp_path, monkeypatch, mode, allow, expected_error
):
    from nexus.config import Config
    from nexus.config.schema import PermissionsSection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nexus.toml").write_text("config_version = 2\n", encoding="utf-8")
    permissions = PermissionsSection(
        mode=mode,
        allow=allow,
        write_roots=["./"],
        on_unattended="deny",
    )
    monkeypatch.setattr(
        Config,
        "load",
        staticmethod(
            lambda _path: SimpleNamespace(v2=SimpleNamespace(permissions=permissions))
        ),
    )
    monkeypatch.setattr(
        bench, "_write_workspace_config", lambda *_args, **_kwargs: None
    )
    problem = bench._preflight_policy("shell-command", workspace, allow_shell=True)
    assert problem and expected_error in problem


def test_preflight_rejects_unbounded_workspace_write_roots(tmp_path, monkeypatch):
    from nexus.config import Config
    from nexus.config.schema import PermissionsSection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = PermissionsSection(
        mode="deny",
        allow=bench._allow_rules("file-edit", workspace),
        write_roots=["/"],
        on_unattended="deny",
    )
    monkeypatch.setattr(
        Config,
        "load",
        staticmethod(
            lambda _path: SimpleNamespace(v2=SimpleNamespace(permissions=permissions))
        ),
    )
    problem = bench._preflight_policy("file-edit", workspace, allow_shell=False)
    assert problem and "write_roots" in problem


def test_reset_refuses_parent_symlink_swap(benchmark_root, monkeypatch, tmp_path):
    _setup(monkeypatch, "file-edit")
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    (outside / "sentinel").write_text("kept", encoding="utf-8")
    real_root = benchmark_root
    swap = benchmark_root.parent / "benchmark-held"
    real_root.rename(swap)
    real_root.symlink_to(outside, target_is_directory=True)
    assert bench.main(["reset", "file-edit"]) == 2
    assert (outside / "sentinel").read_text(encoding="utf-8") == "kept"
    real_root.unlink()
    swap.rename(real_root)


def test_reset_marker_is_an_accident_guard_not_unforgeable_ownership(
    benchmark_root, monkeypatch, tmp_path
):
    _setup(monkeypatch, "file-edit")
    scenario = benchmark_root / "file-edit"
    held = benchmark_root / "held"
    scenario.rename(held)
    forged = tmp_path / "forged-scenario"
    forged.mkdir()
    (forged / bench.MARKER).write_bytes(bench.MARKER_CONTENT)
    (forged / "only-targeted-file").write_text("remove", encoding="utf-8")
    forged.rename(scenario)
    neighbor = benchmark_root / "neighbor"
    neighbor.mkdir()
    (neighbor / "keep").write_text("keep", encoding="utf-8")

    assert bench.main(["reset", "file-edit"]) == 0
    assert not scenario.exists()
    assert held.exists()
    assert (neighbor / "keep").read_text(encoding="utf-8") == "keep"


def test_shell_run_refuses_without_explicit_opt_in(benchmark_root, monkeypatch, capsys):
    _setup(monkeypatch, "shell-command")
    monkeypatch.setattr(
        bench,
        "_preflight_policy",
        lambda *_args, **_kwargs: pytest.fail("preflight must not run"),
    )
    monkeypatch.setattr(
        bench,
        "_stop_workspace_daemon",
        lambda *_args: pytest.fail("daemon must not run"),
    )
    monkeypatch.setattr(
        bench.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("process must not spawn"),
    )
    assert bench.main(["run", "shell-command"]) == 2
    assert "requires explicit --allow-shell" in capsys.readouterr().err


@pytest.mark.parametrize(("exit_code", "expected_status"), [(0, 0), (7, 1)])
def test_run_uses_fresh_session_and_subprocess_argv(
    benchmark_root, monkeypatch, capsys, exit_code, expected_status
):
    _setup(monkeypatch, "shell-command")
    monkeypatch.setattr(bench, "_preflight_policy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bench, "_stop_workspace_daemon", lambda *_args: None)
    # Grading reads raw event objects before log sanitization.
    monkeypatch.setattr(
        bench,
        "_redact_runtime_secrets",
        lambda text: text.replace("exit_code: 0", "exit_code: 9"),
    )
    invocations = []

    class FakeProcess:
        pid = 123

        def __init__(self, command, **kwargs):
            invocations.append((command, kwargs))
            self.returncode = exit_code

        def communicate(self, timeout=None):
            workspace = Path(invocations[-1][0][4])
            (workspace / "result.txt").write_bytes(bench._expected("shell-command"))
            return (
                "\n".join(json.dumps(event) for event in _valid_shell_events()) + "\n"
            ).encode(), b"offline stderr"

    monkeypatch.setattr(bench.subprocess, "Popen", FakeProcess)
    for _ in range(2):
        assert (
            bench.main(["run", "shell-command", "--allow-shell", "--timeout", "2"])
            == expected_status
        )

    assert len(invocations) == 2
    sessions = []
    for command, kwargs in invocations:
        assert command[:4] == [sys.executable, "-m", "nexus", "--workspace"]
        assert command[4] == str(
            (benchmark_root / "shell-command" / "workspace").resolve()
        )
        assert command[5:8] == ["run", "--json", "--session"]
        assert command[-1].endswith(bench.SHELL_COMMAND + "\n```")
        sessions.append(command[8])
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["cwd"] == bench.REPO
    assert sessions[0].startswith("bench-") and sessions[1].startswith("bench-")
    assert sessions[0] != sessions[1]
    assert (
        json.loads(
            (benchmark_root / "shell-command" / "run" / "result.json").read_text()
        )["process"]["exit_code"]
        == exit_code
    )
    assert "WARNING:" in capsys.readouterr().err
    assert "exit_code: 9" in (
        benchmark_root / "shell-command" / "run" / "events.jsonl"
    ).read_text(encoding="utf-8")


def test_reset_refuses_symlinked_scenario_and_symlinked_descendant(
    benchmark_root, monkeypatch
):
    _setup(monkeypatch, "file-edit")
    scenario = benchmark_root / "file-edit"
    outside = benchmark_root.parent / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_bytes(b"outside data")

    # Symlink in a marked tree is detected before rmtree can follow or remove it.
    (scenario / "workspace" / "escape").symlink_to(outside, target_is_directory=True)
    assert bench.main(["reset", "file-edit"]) == 2
    assert keep.read_bytes() == b"outside data"
    assert scenario.exists()

    (scenario / "workspace" / "escape").unlink()
    scenario.rename(benchmark_root / "owned-real")
    (benchmark_root / "file-edit").symlink_to(
        benchmark_root / "owned-real", target_is_directory=True
    )
    assert bench.main(["reset", "file-edit"]) == 2
    assert keep.read_bytes() == b"outside data"
    assert (benchmark_root / "owned-real").exists()


def test_reset_requires_valid_ownership_marker_and_preserves_neighbors(
    benchmark_root, monkeypatch
):
    _setup(monkeypatch, "file-edit")
    neighbor = benchmark_root / "neighbor"
    neighbor.mkdir()
    (neighbor / "sentinel").write_text("intact", encoding="utf-8")
    marker = benchmark_root / "file-edit" / bench.MARKER
    marker.write_bytes(b"tampered\n")
    assert bench.main(["reset", "file-edit"]) == 2
    assert (benchmark_root / "file-edit").exists()
    assert (neighbor / "sentinel").read_text(encoding="utf-8") == "intact"

    marker.unlink()
    marker.symlink_to(neighbor / "sentinel")
    assert bench.main(["reset", "file-edit"]) == 2
    assert (benchmark_root / "file-edit").exists()
    assert (neighbor / "sentinel").read_text(encoding="utf-8") == "intact"


def test_root_lock_excludes_concurrent_commands_and_releases(benchmark_root):
    benchmark_root.parent.mkdir(parents=True)
    benchmark_root.mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with bench.RootLock():
            entered.set()
            assert release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(2)
    try:
        with (
            pytest.raises(bench.BenchError, match="Benchmark is locked"),
            bench.RootLock(),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert not (benchmark_root / ".lock").exists()


def test_lock_and_root_symlinks_are_refused(benchmark_root, tmp_path):
    benchmark_root.parent.mkdir(parents=True)
    benchmark_root.mkdir(parents=True)
    outside = tmp_path / "outside-lock"
    outside.write_text("do not touch", encoding="utf-8")
    (benchmark_root / ".lock").symlink_to(outside)
    with pytest.raises(bench.BenchError, match="symlinked lock"), bench.RootLock():
        pass
    assert outside.read_text(encoding="utf-8") == "do not touch"

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    (benchmark_root / ".lock").unlink()
    benchmark_root.rmdir()
    benchmark_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(bench.BenchError, match="symlinked path"):
        bench._prepare_root()
