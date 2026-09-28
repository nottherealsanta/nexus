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
    assert BUNDLE_NAMES == frozenset({"fs", "patch", "shell", "legacy_shell", "legacy_fs", "task", "web", "meta", "ext", "mcp"})
    assert all_bundles() is BUNDLES


def test_bundle_contents_match_the_plan():
    assert bundle_tools("fs") == ("read", "glob", "grep", "edit", "write")
    assert bundle_tools("patch") == ("apply_patch",)
    assert bundle_tools("shell") == ("bash",)
    assert bundle_tools("legacy_fs") == ("ls", "multiedit")
    assert bundle_tools("legacy_shell") == ("BashOutput", "KillShell")
    assert bundle_tools("task") == ("subagent", "todowrite", "question")
    assert bundle_tools("web") == ("webfetch", "websearch")
    assert bundle_tools("meta") == (
        "ReloadExtensions",
        "ListExtensions",
        "WriteTool",
    )
    assert bundle_tools("ext") == ("skill",)
    # The MCP bundle owns no static names: bridged tools join it dynamically.
    assert bundle_tools("mcp") == ()


def test_profile_table():
    assert PROFILE_NAMES == frozenset({"coding", "coding_meta", "research", "chat", "ops"})
    assert DEFAULT_PROFILE == "coding"
    assert profile_names() == (
        "coding", "coding_meta", "research", "chat", "ops"
    )


def test_base_profile_advertises_only_implemented_canonical_tools():
    assert profile_tools("coding") == frozenset(
        {
            "read", "apply_patch", "glob", "grep", "edit", "write", "bash",
            "subagent", "todowrite", "question", "webfetch", "websearch", "skill",
        }
    )


def test_meta_tools_require_the_explicit_meta_profile():
    assert profile_tools("coding_meta") == profile_tools("coding") | frozenset(
        {"ReloadExtensions", "ListExtensions", "WriteTool"}
    )


def test_research_profile_is_read_search_only():
    # Phase 6 adds Task: research may delegate, but read_only still strips every
    # mutating and shell tool, so a child can never write.
    assert profile_tools("research") == frozenset(
        {"read", "glob", "grep", "subagent", "todowrite", "question", "webfetch", "websearch", "skill"}
    )
    assert profile_tools("research").isdisjoint(
        {"apply_patch", "write", "edit", "bash"}
    )


def test_chat_profile_has_no_tools():
    assert profile_tools("chat") == frozenset()


def test_ops_profile_is_shell_only():
    assert profile_tools("ops") == frozenset({"bash"})


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
