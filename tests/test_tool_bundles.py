"""Bundle and profile definitions (plan section 5.3)."""
from __future__ import annotations

import pytest

from nexus.errors import ConfigError
from nexus.tools.bundles import (
    BUNDLE_NAMES,
    BUNDLES,
    DEFAULT_PROFILE,
    PROFILE_NAMES,
    PROFILES,
    UnknownBundleError,
    UnknownProfileError,
    all_bundles,
    bundle_tools,
    get_bundle,
    get_profile,
    profile_names,
    profile_tools,
)


def test_the_bundles_are_defined():
    assert BUNDLE_NAMES == frozenset({"fs", "shell", "task", "meta", "ext", "mcp"})
    assert all_bundles() is BUNDLES


def test_bundle_contents_match_the_plan():
    assert bundle_tools("fs") == ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS")
    assert bundle_tools("shell") == ("Bash", "BashOutput", "KillShell")
    assert bundle_tools("task") == ("Task", "TodoWrite")
    assert bundle_tools("meta") == (
        "ReloadExtensions",
        "ListExtensions",
        "WriteTool",
    )
    assert bundle_tools("ext") == ("Skill",)
    # The MCP bundle owns no static names: bridged tools join it dynamically.
    assert bundle_tools("mcp") == ()


def test_profile_table():
    assert PROFILE_NAMES == frozenset({"coding", "research", "chat", "ops"})
    assert DEFAULT_PROFILE == "coding"
    assert profile_names() == ("coding", "research", "chat", "ops")


def test_coding_profile_covers_all_phase2_tools():
    expected = set()
    for name in BUNDLE_NAMES:
        expected.update(bundle_tools(name))
    assert profile_tools("coding") == expected


def test_research_profile_is_read_search_only():
    # Phase 6 adds Task: research may delegate, but read_only still strips every
    # mutating and shell tool, so a child can never write.
    assert profile_tools("research") == frozenset(
        {"Read", "Glob", "Grep", "LS", "Task"}
    )
    assert profile_tools("research").isdisjoint(
        {"Write", "Edit", "MultiEdit", "Bash"}
    )


def test_chat_profile_has_no_tools():
    assert profile_tools("chat") == frozenset()


def test_ops_profile_is_shell_only():
    assert profile_tools("ops") == frozenset(bundle_tools("shell"))


def test_unknown_profiles_fail_closed():
    with pytest.raises(UnknownProfileError):
        get_profile("does-not-exist")
    with pytest.raises(UnknownProfileError):
        profile_tools("")
    with pytest.raises(UnknownProfileError):
        profile_tools(None)  # type: ignore[arg-type]
    assert issubclass(UnknownProfileError, ConfigError)


def test_unknown_bundles_fail_closed():
    with pytest.raises(UnknownBundleError):
        get_bundle("net")
    with pytest.raises(UnknownBundleError):
        bundle_tools("nope")
    assert issubclass(UnknownBundleError, ConfigError)


def test_profile_definitions_are_frozen():
    profile = PROFILES["coding"]
    with pytest.raises(AttributeError):
        profile.name = "other"  # type: ignore[misc]
