"""Background provider-usage refresh: config, periodic poll, Settings control, shutdown."""
from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelsSection
from nexus.errors import ConfigError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host_support.usage_snapshot import UsageSnapshotService
from nexus.model.usage_gate import UsagePolicy
from nexus.runtime import Runtime
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui_support import settings_page as sp


def _result(at: float) -> dict:
    return {"providers": [{"id": "codex", "error": "", "windows": []}], "fetched_at": at}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _runtime(seconds=None, **overrides):
    snapshots: list = []
    values = dict(set_usage_snapshot=snapshots.append, snapshots=snapshots, _usage_policy=UsagePolicy.from_section(None))
    if seconds is not None:
        values["usage_refresh_seconds"] = lambda: seconds
    values.update(overrides)
    return SimpleNamespace(**values)


# -- config -------------------------------------------------------------------


def test_default_is_five_minutes_and_bounded():
    assert ConfigV2().models.usage_refresh_seconds == 300
    assert ModelsSection(usage_refresh_seconds=60).usage_refresh_seconds == 60
    assert ModelsSection(usage_refresh_seconds=3600).usage_refresh_seconds == 3600
    for bad in (59, 3601, 0, -1, True, 90.5, "300"):
        with pytest.raises(ValueError, match="usage_refresh_seconds"):
            ModelsSection(usage_refresh_seconds=bad)  # type: ignore[arg-type]


def test_toml_value_loads_and_a_bad_one_is_a_config_error(tmp_path):
    home = tmp_path / "home"
    (home / ".nexus").mkdir(parents=True)
    config = home / ".nexus" / "config.toml"
    config.write_text("config_version = 2\n[models]\nusage_refresh_seconds = 900\n")
    assert Config.load(tmp_path, home=home, environ={}).v2.models.usage_refresh_seconds == 900
    config.write_text("config_version = 2\n[models]\nusage_refresh_seconds = 5\n")
    with pytest.raises(ConfigError):
        Config.load(tmp_path, home=home, environ={})


def test_a_project_file_cannot_set_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nexus.toml").write_text("config_version = 2\n[models]\nusage_refresh_seconds = 60\n")
    with pytest.raises(ConfigError, match="user-level"):
        Config.load(workspace, home=home, environ={})


# -- periodic poll ------------------------------------------------------------


async def test_idle_poll_refreshes_repeatedly_without_any_kick_or_gate():
    calls: list[int] = []

    async def fetch(runtime):
        calls.append(1)
        return _result(len(calls))

    runtime = _runtime(0.05, _usage_policy=UsagePolicy.from_section({"enabled": False}))
    service = UsageSnapshotService(runtime, fetch=fetch, tick=0.01)
    service.start()
    await asyncio.sleep(0.3)
    await service.aclose()
    assert len(calls) >= 3 and len(runtime.snapshots) >= 3


async def test_poll_uses_the_default_five_minutes_without_a_configured_value():
    clock = _Clock()
    calls: list[int] = []

    async def fetch(runtime):
        calls.append(1)
        return _result(clock.now)

    service = UsageSnapshotService(SimpleNamespace(), fetch=fetch, clock=clock)
    assert service.interval() == 300
    service.poll()  # first poll refreshes at once
    await service._task
    clock.now += 299
    service.poll()
    assert service._task.done() and calls == [1]
    clock.now += 2
    service.poll()
    await service._task
    assert calls == [1, 1]


async def test_poll_follows_a_changed_interval_live():
    clock = _Clock()
    interval = {"seconds": 3600}
    calls: list[int] = []

    async def fetch(runtime):
        calls.append(1)
        return _result(clock.now)

    runtime = _runtime(usage_refresh_seconds=lambda: interval["seconds"])
    service = UsageSnapshotService(runtime, fetch=fetch, clock=clock)
    service.poll()
    await service._task
    clock.now += 120
    service.poll()
    assert calls == [1]  # an hour has not passed
    interval["seconds"] = 60  # saved in Settings
    service.poll()
    await service._task
    assert calls == [1, 1]


async def test_poll_does_not_overlap_an_in_flight_fetch():
    clock = _Clock()
    gate = asyncio.Event()
    calls: list[int] = []

    async def fetch(runtime):
        calls.append(1)
        await gate.wait()
        return _result(clock.now)

    service = UsageSnapshotService(_runtime(60), fetch=fetch, clock=clock)
    service.poll()
    await asyncio.sleep(0)
    clock.now += 500
    service.poll()
    service.kick()
    await asyncio.sleep(0)
    assert calls == [1]
    gate.set()
    await service._task


async def test_failed_poll_is_retried_next_interval_and_keeps_the_old_snapshot():
    clock = _Clock()
    runtime = _runtime(60)
    fail = False

    async def fetch(_runtime):
        if fail:
            raise RuntimeError("offline")
        return _result(clock.now)

    service = UsageSnapshotService(runtime, fetch=fetch, clock=clock)
    service.poll()
    await service._task
    fail = True
    clock.now += 61
    service.poll()
    await service._task
    assert len(runtime.snapshots) == 1
    fail = False
    clock.now += 61
    service.poll()
    await service._task
    assert len(runtime.snapshots) == 2


async def test_a_panel_fetch_resets_the_clock_and_an_older_result_is_dropped():
    clock = _Clock()
    runtime = _runtime(60)
    service = UsageSnapshotService(runtime, fetch=AsyncMock(), clock=clock)
    service.ingest(_result(2000.0))  # the usage panel's fresh numbers
    service.ingest(_result(1500.0))  # a slower background fetch started earlier
    assert [s.fetched_at for s in runtime.snapshots] == [2000.0]
    service.poll()  # fresh numbers just landed: nothing due
    assert service._task is None


async def test_aclose_cancels_the_poller_and_the_in_flight_fetch_and_stays_closed():
    started = asyncio.Event()

    async def fetch(runtime):
        started.set()
        await asyncio.sleep(60)

    service = UsageSnapshotService(_runtime(60), fetch=fetch, tick=0.01)
    service.start()
    await started.wait()
    poller, fetching = service._poller, service._task
    await service.aclose()
    assert poller.cancelled() and fetching.cancelled()
    service.start()
    service.kick()
    assert service._poller is None and service._task is None


# -- runtime / host / Settings -----------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)  # the settings writer targets ~/.nexus
    (home / ".nexus").mkdir(parents=True)
    (home / ".nexus" / "config.toml").write_text('config_version = 2\n\n[models]\ndefault = "scripted/m"\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return Runtime(workspace, home=home, config_loader=lambda: Config.load(workspace, home=home, environ={})), home


async def test_settings_round_trip_is_validated_persisted_and_live(live):
    runtime, home = live
    host = HostFacade(runtime)
    first = await host.handle(p.UsageRefreshSettings())
    assert isinstance(first, p.UsageRefreshSettingsResult)
    assert (first.seconds, first.minimum, first.maximum) == (300, 60, 3600)
    assert host.usage.interval() == 300

    saved = await host.handle(p.UsageRefreshSettingsSet(seconds=600))
    assert saved.seconds == 600
    assert tomllib.loads((home / ".nexus" / "config.toml").read_text())["models"]["usage_refresh_seconds"] == 600
    assert runtime.usage_refresh_seconds() == 600 and host.usage.interval() == 600  # no restart

    for bad in (59, 3601, True):
        result = await host.handle(p.UsageRefreshSettingsSet(seconds=bad))  # type: ignore[arg-type]
        assert isinstance(result, p.ErrorResult)
    assert runtime.usage_refresh_seconds() == 600
    await host.shutdown()
    await runtime.aclose()


async def test_shutdown_stops_the_started_poll(live):
    runtime, _ = live
    host = HostFacade(runtime)
    host.usage._fetch = AsyncMock(return_value=_result(1.0))
    host.start_usage_polling()
    poller = host.usage._poller
    assert poller is not None
    await asyncio.sleep(0)
    await host.shutdown()
    assert poller.done() and host.usage._poller is None
    await runtime.aclose()


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    shell.client.providers_status = AsyncMock(return_value=p.ProvidersStatusResult(providers=[]))
    shell.client.usage_refresh_settings = AsyncMock(return_value=p.UsageRefreshSettingsResult(seconds=300))
    shell.client.usage_refresh_settings_set = AsyncMock(return_value=p.UsageRefreshSettingsResult(seconds=600))
    return shell


def _row(page):
    return next(b for b in page["blocks"] if b.get("t") == "row" and b["id"] == "usage-refresh")


async def test_providers_page_exposes_and_saves_the_interval(shell):
    await shell.workflows.settings_area("providers")
    control = _row(shell.workflows.settings_page)["control"]
    assert control["value"] == "every 5 minutes"
    assert [value for _, value in control["options"]] == [60, 180, 300, 600, 900, 1800, 3600]
    await shell.workflows.operate(sp.op("providers", "usage_refresh", value=600))
    shell.client.usage_refresh_settings_set.assert_awaited_with(600)


async def test_providers_page_includes_a_custom_value_and_survives_an_older_daemon(shell):
    shell.client.usage_refresh_settings.return_value = p.UsageRefreshSettingsResult(seconds=240)
    await shell.workflows.settings_area("providers")
    assert _row(shell.workflows.settings_page)["control"]["value"] == "every 4 minutes"
    shell.client.usage_refresh_settings = AsyncMock(side_effect=RuntimeError("unknown command"))
    await shell.workflows.settings_area("providers")
    assert not any(b.get("id") == "usage-refresh" for b in shell.workflows.settings_page["blocks"])
