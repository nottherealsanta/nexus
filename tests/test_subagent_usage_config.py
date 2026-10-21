"""``[models.subagent_usage]``: defaults, validation, and project-layer rules."""
from __future__ import annotations

from pathlib import Path

import msgspec
import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelsSection, SubagentUsageSection
from nexus.errors import ConfigError


def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir(exist_ok=True)
    return path


def write_v2(workspace: Path, body: str) -> None:
    (workspace / "nexus.toml").write_text(
        "config_version = 2\n" + body, encoding="utf-8"
    )


def write_user(target: Path, body: str) -> None:
    h = target if target.name == "home" else home(target)
    directory = h / ".nexus"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(
        "config_version = 2\n" + body, encoding="utf-8"
    )


def load_v2(workspace: Path, *, home: Path | None = None) -> SubagentUsageSection:
    h = home if home is not None else (workspace / "home")
    h.mkdir(exist_ok=True)
    config = Config.load(workspace, home=h, environ={})
    return config.v2.models.subagent_usage


load = load_v2


def test_defaults():
    section = ConfigV2().models.subagent_usage
    assert section.enabled is True
    assert section.five_hour_min_remaining == 10
    assert section.weekly_min_remaining == 5
    assert section.monthly_min_remaining == 2
    assert section.refresh_seconds == 180
    assert section.providers == {}
    assert ConfigV2().models_configured() is False
    assert ModelsSection() == ModelsSection(subagent_usage=SubagentUsageSection())


def test_toml_round_trip(tmp_path):
    write_user(
        tmp_path / "home",
        """
[models.subagent_usage]
enabled = false
five_hour_min_remaining = 20
weekly_min_remaining = 7.5
monthly_min_remaining = 0
refresh_seconds = 300

[models.subagent_usage.providers.github-copilot]
monthly_min_remaining = 10
""",
    )
    section = load_v2(tmp_path, home=tmp_path / "home")
    assert section.enabled is False
    assert section.five_hour_min_remaining == 20
    assert section.weekly_min_remaining == 7.5
    assert section.monthly_min_remaining == 0
    assert section.refresh_seconds == 300
    override = section.providers["github-copilot"]
    assert override.monthly_min_remaining == 10
    assert override.five_hour_min_remaining is None
    assert msgspec.convert(msgspec.to_builtins(section), SubagentUsageSection) == section


def test_models_table_without_subagent_usage_gets_defaults(tmp_path):
    write_v2(tmp_path, '[models]\ndefault = "medium"\n')
    assert load(tmp_path) == SubagentUsageSection()


@pytest.mark.parametrize(
    "body",
    [
        "five_hour_min_remaining = true",
        "weekly_min_remaining = nan",
        "monthly_min_remaining = inf",
        "five_hour_min_remaining = -inf",
        "five_hour_min_remaining = -1",
        "weekly_min_remaining = 100.5",
        'monthly_min_remaining = "5"',
        "refresh_seconds = 59",
        "refresh_seconds = 3601",
        "refresh_seconds = true",
        "refresh_seconds = 90.5",
        "bogus = 1",
        "[models.subagent_usage.providers.x]\nmonthly_min_remaining = true",
        "[models.subagent_usage.providers.x]\nmonthly_min_remaining = nan",
        "[models.subagent_usage.providers.x]\nweekly_min_remaining = 101",
        "[models.subagent_usage.providers.x]\nbogus = 1",
    ],
)
def test_invalid_values_rejected(tmp_path, body):
    if body.startswith("["):
        write_v2(tmp_path, "\n" + body + "\n")
    else:
        write_v2(tmp_path, "\n[models.subagent_usage]\n" + body + "\n")
    with pytest.raises(ConfigError):
        Config.load(tmp_path, home=home(tmp_path), environ={})


def test_boundaries_accepted(tmp_path):
    write_user(
        tmp_path / "home",
        "\n[models.subagent_usage]\nfive_hour_min_remaining = 0\n"
        "weekly_min_remaining = 100\nrefresh_seconds = 60\n",
    )
    assert load_v2(tmp_path, home=tmp_path / "home").refresh_seconds == 60
    write_user(tmp_path / "home", "\n[models.subagent_usage]\nrefresh_seconds = 3600\n")
    assert load_v2(tmp_path, home=tmp_path / "home").refresh_seconds == 3600


def test_project_layer_cannot_lower_thresholds(tmp_path):
    write_user(
        tmp_path,
        """
[models.subagent_usage]
five_hour_min_remaining = 30
weekly_min_remaining = 1

[models.subagent_usage.providers.github-copilot]
monthly_min_remaining = 10
""",
    )
    write_v2(
        tmp_path,
        """
[models.subagent_usage]
five_hour_min_remaining = 15
weekly_min_remaining = 20
monthly_min_remaining = 0

[models.subagent_usage.providers.github-copilot]
monthly_min_remaining = 4
weekly_min_remaining = 1

[models.subagent_usage.providers.project-only]
five_hour_min_remaining = 50
""",
    )
    section = load(tmp_path)
    assert section.five_hour_min_remaining == 30  # user value kept
    assert section.weekly_min_remaining == 20  # project raised it
    assert section.monthly_min_remaining == 2  # default kept, 0 ignored
    copilot = section.providers["github-copilot"]
    assert copilot.monthly_min_remaining == 10
    assert copilot.weekly_min_remaining == 20  # floor is the effective section value
    assert section.providers["project-only"].five_hour_min_remaining == 50


def test_project_layer_cannot_lower_below_defaults(tmp_path):
    write_v2(tmp_path, "\n[models.subagent_usage]\nfive_hour_min_remaining = 1\n")
    assert load(tmp_path).five_hour_min_remaining == 10


@pytest.mark.parametrize("key, value", [("enabled", "false"), ("refresh_seconds", "120")])
def test_project_layer_user_only_keys_refused(tmp_path, key, value):
    write_v2(tmp_path, f"\n[models.subagent_usage]\n{key} = {value}\n")
    with pytest.raises(ConfigError, match=f"models.subagent_usage.{key}"):
        Config.load(tmp_path, home=home(tmp_path), environ={})


def test_user_layer_may_set_enabled_and_refresh(tmp_path):
    write_user(tmp_path, "\n[models.subagent_usage]\nenabled = false\nrefresh_seconds = 600\n")
    section = load(tmp_path)
    assert section.enabled is False
    assert section.refresh_seconds == 600
