"""``[agents]`` background keys: defaults, validation, and layering (plan phase 1)."""

from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import AgentsSection
from nexus.errors import ConfigError

KEYS = {
    "max_live": 8,
    "max_pending_reports": 32,
    "max_report_batch": 16,
    "max_report_chars": 24000,
    "report_coalesce_ms": 750,
    "max_auto_wakes": 20,
    "max_followup_chars": 20000,
    "oneshot_wait_seconds": 600,
}


def test_defaults_are_background_and_persistent():
    section = AgentsSection()
    assert section.background is True
    for key, value in KEYS.items():
        assert getattr(section, key) == value


@pytest.mark.parametrize("key", sorted(KEYS))
def test_integer_keys_reject_bools_strings_and_out_of_range(key):
    for bad in (True, "3", 1.5, -1, 10**9):
        with pytest.raises(ValueError, match=f"agents.{key}"):
            AgentsSection(**{key: bad})


def test_zero_is_allowed_only_where_it_means_off():
    assert AgentsSection(max_auto_wakes=0).max_auto_wakes == 0
    assert AgentsSection(oneshot_wait_seconds=0).oneshot_wait_seconds == 0
    assert AgentsSection(report_coalesce_ms=0).report_coalesce_ms == 0
    with pytest.raises(ValueError):
        AgentsSection(max_live=0)


def test_background_must_be_boolean():
    with pytest.raises(ValueError, match="agents.background"):
        AgentsSection(background=1)  # type: ignore[arg-type]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load(tmp_path, user: str = "", project: str = "", environ=None):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    if user:
        _write(home / ".nexus" / "config.toml", user)
    if project:
        _write(ws / ".agents" / "nexus.toml", project)
    return Config.load(ws, home=home, environ=environ or {}).v2.agents


def test_user_config_sets_the_keys(tmp_path):
    section = _load(tmp_path, user="config_version = 2\n[agents]\nbackground = true\nmax_live = 12\nmax_auto_wakes = 50\n")
    assert section.background is True and section.max_live == 12 and section.max_auto_wakes == 50


def test_project_file_may_lower_but_not_raise_the_bounds(tmp_path):
    section = _load(
        tmp_path,
        user="config_version = 2\n[agents]\nmax_live = 10\nmax_followup_chars = 5000\n",
        project="config_version = 2\n[agents]\nmax_live = 99\nmax_followup_chars = 100\nmax_auto_wakes = 5000\n",
    )
    assert section.max_live == 10  # raised value ignored
    assert section.max_followup_chars == 100  # lowered value applies
    assert section.max_auto_wakes == 20  # default 20 is the ceiling too


def test_invalid_project_value_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        _load(tmp_path, project="config_version = 2\n[agents]\nmax_live = 0\n")


def test_env_overlay_reads_the_new_keys(tmp_path):
    section = _load(tmp_path, user="config_version = 2\n", environ={"NEXUS_AGENTS__BACKGROUND": "true", "NEXUS_AGENTS__MAX_LIVE": "3"})
    assert section.background is True and section.max_live == 3
