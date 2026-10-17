"""Opt-in ``--dangerously-skip-permissions`` for the Antigravity worker: default off, explicit on, disclosed.

Reuses the fake ``agy`` from ``test_antigravity_worker``; no real agy or model turn is involved.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nexus.agents.antigravity_worker import (
    SKIP_PERMISSIONS_FLAG,
    AntigravityWorker,
    WorkerConfig,
    WorkerPolicy,
    resolve_policy,
)
from nexus.config import Config
from nexus.config.schema import ConfigV2, ProviderSection
from nexus.errors import ConfigError, ProviderError
from nexus.runtime import Runtime
from nexus.ui_support.external_worker import header_rows

from test_antigravity_worker import INIT, MODEL, OK, init, make_fake, run_worker, worker_for

HELP = ("--input-format --output-format stream-json --model --mode auto|accept-edits|plan --print-timeout "
        f"{SKIP_PERMISSIONS_FLAG}")
ALWAYS = init(permission_mode="always-proceed")


# -- config -------------------------------------------------------------------


def test_default_is_off_everywhere():
    assert WorkerConfig().skip_permissions is False
    assert ProviderSection().worker_dangerously_skip_permissions is False
    assert resolve_policy(WorkerConfig(), read_only=False).skip_permissions is False
    for section in (None, SimpleNamespace(), SimpleNamespace(worker_dangerously_skip_permissions=None)):
        assert WorkerConfig.from_section(section).skip_permissions is False


def test_explicit_opt_in_reaches_the_policy():
    cfg = WorkerConfig.from_section(SimpleNamespace(worker_dangerously_skip_permissions=True))
    assert cfg.skip_permissions is True
    assert resolve_policy(cfg, read_only=False).skip_permissions is True


def test_non_boolean_values_are_rejected():
    with pytest.raises(ConfigError, match="boolean"):
        WorkerConfig(skip_permissions="yes")
    with pytest.raises(ValueError, match="boolean"):
        ProviderSection(kind="antigravity", worker_dangerously_skip_permissions="yes")
    with pytest.raises(ConfigError, match="true or false"):
        Runtime._validate_antigravity_section(
            "antigravity", SimpleNamespace(worker_dangerously_skip_permissions="yes"))


def test_opt_in_does_not_relax_other_refusals():
    on = WorkerConfig(skip_permissions=True)
    with pytest.raises(ConfigError, match="read-only"):
        resolve_policy(on, read_only=True)
    with pytest.raises(ConfigError, match="worker_inherits_agy_settings"):
        resolve_policy(WorkerConfig(skip_permissions=True, worker_inherits_agy_settings=False), read_only=False)
    with pytest.raises(ConfigError, match="auto, accept-edits or plan"):
        resolve_policy(WorkerConfig(skip_permissions=True, permission_mode="bypassPermissions"), read_only=False)
    with pytest.raises(ConfigError, match="cannot be enforced"):
        resolve_policy(WorkerConfig(skip_permissions=True, restricted=True), read_only=False)


def test_runtime_section_validation_accepts_agy_and_refuses_other_kinds():
    Runtime._validate_antigravity_section(
        "antigravity", ProviderSection(kind="antigravity", worker_dangerously_skip_permissions=True))
    with pytest.raises(ConfigError, match="only to kind = antigravity"):
        Runtime._validate_claude_section(
            "claude-agent", ProviderSection(kind="claude-agent", worker_dangerously_skip_permissions=True))
    # Off is the default and never trips other kinds.
    Runtime._validate_claude_section("claude-agent", ProviderSection(kind="claude-agent"))


# -- authority text -----------------------------------------------------------


def test_authority_discloses_bypass_only_when_enabled():
    off = resolve_policy(WorkerConfig(), read_only=False).authority("/w")
    on = resolve_policy(WorkerConfig(skip_permissions=True), read_only=False).authority("/w")
    assert SKIP_PERMISSIONS_FLAG not in off and "ALL tool actions" not in off
    assert SKIP_PERMISSIONS_FLAG in on and "auto-approves ALL tool actions" in on
    assert "did not mediate" in on and "no sandbox requested" in on


def test_ui_header_marks_bypass():
    base = {"provider": "antigravity", "model": MODEL, "workspace": "/w", "permission_mode": "auto"}
    assert ("Permissions", "BYPASSED: --dangerously-skip-permissions auto-approves ALL agy tool actions") in \
        header_rows({**base, "skip_permissions": True})
    assert not [r for r in header_rows({**base, "skip_permissions": False}) if r[0] == "Permissions"]


# -- argv ---------------------------------------------------------------------


def test_build_argv_default_has_no_bypass_flag():
    argv = AntigravityWorker.build_argv(resolve_policy(WorkerConfig(), read_only=False), model=MODEL,
                                        print_timeout=30)
    assert SKIP_PERMISSIONS_FLAG not in argv and not any("dangerous" in a for a in argv)


def test_build_argv_opt_in_appends_flag_once_after_documented_args():
    default = AntigravityWorker.build_argv(resolve_policy(WorkerConfig(), read_only=False), model=MODEL,
                                           print_timeout=30)
    argv = AntigravityWorker.build_argv(resolve_policy(WorkerConfig(skip_permissions=True), read_only=False),
                                        model=MODEL, print_timeout=30)
    expected = list(default)
    expected[expected.index("--mode") + 1] = "accept-edits"
    assert argv == [*expected, SKIP_PERMISSIONS_FLAG] and argv.count(SKIP_PERMISSIONS_FLAG) == 1


def test_default_run_launches_without_flag_and_is_not_marked(tmp_path):
    res, events, bin_dir = run_worker(tmp_path, {"lines": [INIT, OK]})
    assert res.status == "completed"
    assert SKIP_PERMISSIONS_FLAG not in json.loads((bin_dir / "record.json").read_text())["argv"]
    assert events[0][1]["skip_permissions"] is False and events[-1][1]["skip_permissions"] is False
    assert "auto-approved ALL" not in res.text and "auto-approves ALL" not in events[0][1]["authority"]


def test_opt_in_replaces_auto_with_documented_mode_only_for_auto():
    assert resolve_policy(WorkerConfig(), read_only=False).permission_mode == "auto"
    assert resolve_policy(WorkerConfig(skip_permissions=True), read_only=False).permission_mode == "accept-edits"
    for mode in ("accept-edits", "plan"):
        assert resolve_policy(WorkerConfig(skip_permissions=True, permission_mode=mode),
                              read_only=False).permission_mode == mode


def test_opt_in_run_never_passes_mode_auto(tmp_path):
    res, _, bin_dir = run_worker(tmp_path, {"help": HELP, "lines": [ALWAYS, OK]}, skip_permissions=True)
    assert res.status == "completed", (res.error, res.diagnostics)
    argv = json.loads((bin_dir / "record.json").read_text())["argv"]
    assert argv[argv.index("--mode") + 1] == "accept-edits" and "auto" not in argv


def test_opt_in_preflight_requires_the_substituted_mode(tmp_path):
    help_no_accept = f"--input-format --output-format stream-json --model --mode auto --print-timeout {SKIP_PERMISSIONS_FLAG}"
    with pytest.raises(ProviderError, match="accept-edits"):
        run_worker(tmp_path, {"help": help_no_accept, "lines": [ALWAYS, OK]}, skip_permissions=True)
    assert not (tmp_path / "bin" / "record.json").exists()  # never launched


def test_opt_in_run_launches_with_flag_and_discloses(tmp_path):
    res, events, bin_dir = run_worker(tmp_path, {"help": HELP, "lines": [ALWAYS, OK]}, skip_permissions=True)
    assert res.status == "completed", (res.error, res.diagnostics)
    argv = json.loads((bin_dir / "record.json").read_text())["argv"]
    assert argv[-1] == SKIP_PERMISSIONS_FLAG and argv.count(SKIP_PERMISSIONS_FLAG) == 1
    started, finished = events[0][1], events[-1][1]
    assert started["skip_permissions"] is True and "auto-approves ALL tool actions" in started["authority"]
    assert finished["skip_permissions"] is True and finished["agy_permission_mode"] == "always-proceed"
    assert any("auto-approved ALL" in n for n in finished["clip_notes"])
    assert "auto-approved ALL" in res.text and res.policy.skip_permissions is True


def test_opt_in_with_other_agy_mode_still_runs_and_discloses(tmp_path):
    res, events, _ = run_worker(tmp_path, {"help": HELP, "lines": [INIT, OK]}, skip_permissions=True)
    assert res.status == "completed" and events[-1][1]["agy_permission_mode"] == "request-review"
    assert "auto-approved ALL" in res.text


def test_opt_in_failure_results_are_also_disclosed(tmp_path):
    res, events, _ = run_worker(tmp_path, {"help": HELP, "lines": [ALWAYS]}, skip_permissions=True)
    assert res.status == "failed" and "auto-approved ALL" in res.text
    assert any("auto-approved ALL" in n for n in events[-1][1]["clip_notes"])


# -- always-proceed -----------------------------------------------------------


def test_always_proceed_refused_by_default_with_opt_in_hint(tmp_path):
    res, events, _ = run_worker(tmp_path, {"lines": [ALWAYS, OK]})
    assert res.status == "failed" and "always-proceed" in res.error
    assert "worker_dangerously_skip_permissions" in res.error and "done" not in res.text
    assert "auto-approved ALL" not in res.text


def test_always_proceed_accepted_only_when_enabled(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off, *_ = run_worker(tmp_path / "off", {"lines": [ALWAYS, OK]})
    on, *_ = run_worker(tmp_path / "on", {"help": HELP, "lines": [ALWAYS, OK]}, skip_permissions=True)
    assert off.status == "failed" and on.status == "completed" and on.text.startswith("done")


# -- preflight and policy integrity -------------------------------------------


def test_opt_in_requires_agy_to_advertise_the_flag(tmp_path):
    script = make_fake(tmp_path, lines=[INIT, OK])  # default help lacks the flag
    (tmp_path / "ws").mkdir()
    worker, policy = worker_for(script, tmp_path, skip_permissions=True)
    with pytest.raises(ProviderError, match="required headless flags") as refused:
        asyncio.run(worker.run(prompt="p", model=MODEL, workspace=tmp_path / "ws", policy=policy))
    assert SKIP_PERMISSIONS_FLAG in str(refused.value)
    assert not (tmp_path / "bin" / "record.json").exists()  # never launched


def test_policy_cannot_enable_bypass_beyond_the_workers_config(tmp_path):
    script = make_fake(tmp_path, help=HELP, lines=[INIT, OK])
    (tmp_path / "ws").mkdir()
    worker, _ = worker_for(script, tmp_path)  # config: off
    forged = WorkerPolicy(permission_mode="auto", executable=str(script), skip_permissions=True)
    with pytest.raises(ConfigError, match="not one resolve_policy"):
        asyncio.run(worker.run(prompt="p", model=MODEL, workspace=tmp_path / "ws", policy=forged))
    assert not (tmp_path / "bin" / "record.json").exists()


# -- config layering ----------------------------------------------------------

AGY = 'config_version = 2\n[providers.antigravity]\nkind = "antigravity"\n'


def _dirs(tmp_path):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    (workspace / ".agents").mkdir(parents=True)
    (workspace / ".nexus").mkdir()
    (home / ".nexus").mkdir(parents=True)
    return workspace, home


def test_user_level_config_can_enable(tmp_path):
    workspace, home = _dirs(tmp_path)
    (home / ".nexus" / "config.toml").write_text(AGY + "worker_dangerously_skip_permissions = true\n")
    (workspace / "nexus.toml").write_text(AGY)  # a project file that merely omits it must not reset it
    config = Config.load(workspace, home=home, environ={})
    section = config.v2.providers["antigravity"]
    assert section.worker_dangerously_skip_permissions is True
    assert WorkerConfig.from_section(section).skip_permissions is True


def test_omitted_everywhere_is_false(tmp_path):
    workspace, home = _dirs(tmp_path)
    (workspace / "nexus.toml").write_text(AGY)
    section = Config.load(workspace, home=home, environ={}).v2.providers["antigravity"]
    assert section.worker_dangerously_skip_permissions is False


@pytest.mark.parametrize("relative", ["nexus.toml", ".nexus/nexus.toml", ".agents/nexus.toml"])
@pytest.mark.parametrize("value", ["true", "false"])
def test_project_layers_cannot_set_it(tmp_path, relative, value):
    workspace, home = _dirs(tmp_path)
    (workspace / relative).write_text(AGY + f"worker_dangerously_skip_permissions = {value}\n")
    with pytest.raises(ConfigError, match="user-level config") as refused:
        Config.load(workspace, home=home, environ={})
    assert "worker_dangerously_skip_permissions" in str(refused.value) and relative in str(refused.value)


def test_project_layer_cannot_override_user_false_to_true(tmp_path):
    workspace, home = _dirs(tmp_path)
    (home / ".nexus" / "config.toml").write_text(AGY + "worker_dangerously_skip_permissions = false\n")
    (workspace / "nexus.toml").write_text(AGY + "worker_dangerously_skip_permissions = true\n")
    with pytest.raises(ConfigError, match="user-level config"):
        Config.load(workspace, home=home, environ={})


def test_session_override_can_set_it(tmp_path):
    """Session/flag layers are the user's own process input, not repository content."""
    workspace, home = _dirs(tmp_path)
    (workspace / "nexus.toml").write_text(AGY)
    session = {"providers": {"antigravity": {"worker_dangerously_skip_permissions": True}}}
    config = Config.load(workspace, home=home, environ={}, session=session)
    assert config.v2.providers["antigravity"].worker_dangerously_skip_permissions is True


def test_nexus_child_carries_opt_in_into_its_policy(tmp_path):
    from test_antigravity_runtime import REF, fake_runtime, spec_for
    from nexus.runtime import _ChildRuntime

    on = Config(v2=ConfigV2(providers={"antigravity": ProviderSection(
        kind="antigravity", worker_dangerously_skip_permissions=True)}))
    child = _ChildRuntime(fake_runtime(), spec_for(tmp_path, config=on, model=REF), object())
    assert child._external.skip_permissions is True and child._external.policy.skip_permissions is True
    assert child._external.worker.config.skip_permissions is True
    off = _ChildRuntime(fake_runtime(), spec_for(tmp_path, model=REF), object())
    assert off._external.skip_permissions is False and off._external.policy.skip_permissions is False
