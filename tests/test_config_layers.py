from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.layers import (
    build_v2,
    deep_merge,
    detect_version,
    env_overlay_v2,
    load_effective,
    normalize_v1_to_v2,
)
from nexus.config.schema import ConfigV2, SessionsSection, SettingsSection
from nexus.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_defaults_when_no_files(tmp_path):
    config = Config.load(tmp_path, home=_home(tmp_path), environ={})
    assert config.version == 1
    assert config.executable == "codex"
    assert config.sandbox == "workspace-write"
    assert config.context_chars == 64000
    assert config.v2 is None
    assert ConfigV2().sessions.auto_archive_days == 2


def test_nested_git_workspace_inherits_project_models_without_changing_workspace(tmp_path):
    home = _home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "codex/gpt-test"\n'
        '[providers.codex]\nauth = "chatgpt_oauth"\napi = "responses"\n',
        encoding="utf-8",
    )
    workspace = project / "benchmark"
    workspace.mkdir()
    config = Config.load(workspace, home=home, environ={})
    assert config.version == 2
    assert config.model == "codex/gpt-test"
    assert config.v2.models_configured()
    assert "codex" in config.v2.providers
    assert config.source == str(project / "nexus.toml")

    (workspace / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "codex/local"\n', encoding="utf-8"
    )
    assert Config.load(workspace, home=home, environ={}).model == "codex/local"


def test_git_config_does_not_cross_repository_or_unrelated_parent(tmp_path):
    home = _home(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "nexus.toml").write_text('model = "parent"\n', encoding="utf-8")
    workspace = parent / "unrelated"
    workspace.mkdir()
    assert Config.load(workspace, home=home, environ={}).model is None

    (parent / ".git").mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    (workspace / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert Config.load(nested, home=home, environ={}).model is None


@pytest.mark.parametrize("days", [-1, 3651, True, 1.5])
def test_auto_archive_days_must_be_a_bounded_integer(days):
    with pytest.raises(ValueError, match="sessions.auto_archive_days"):
        ConfigV2(sessions=SessionsSection(auto_archive_days=days))


def test_auto_archive_days_can_be_disabled():
    assert ConfigV2(sessions=SessionsSection(auto_archive_days=0)).sessions.auto_archive_days == 0


def test_sessions_auto_archive_days_loads_from_plural_config_section(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n\n[sessions]\nauto_archive_days = 0\n",
        encoding="utf-8",
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.sessions.auto_archive_days == 0


def test_settings_confirm_edits_loads_from_config(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n\n[settings]\nconfirm_edits = true\n",
        encoding="utf-8",
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.settings.confirm_edits is True
    assert ConfigV2(settings=SettingsSection(confirm_edits=True)).settings.confirm_edits


def test_scoped_project_settings_config_overlays_legacy_workspace_config(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n\n[agent]\nname = 'general'\n\n[tools]\nbash_timeout_s = 10\n",
        encoding="utf-8",
    )
    settings_dir = tmp_path / ".nexus"
    settings_dir.mkdir()
    (settings_dir / "nexus.toml").write_text(
        "config_version = 2\n\n[tools]\nbash_timeout_s = 5\n",
        encoding="utf-8",
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.agent.name == "general"
    assert config.v2.tools.bash_timeout_s == 5


def test_flat_v1_loads_and_unknown_key_rejected(tmp_path):
    home = _home(tmp_path)
    config_path = tmp_path / "nexus.toml"
    config_path.write_text('executable = "mycodex"\nmodel = "custom"\n')
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 1
    assert config.executable == "mycodex"
    assert config.model == "custom"

    config_path.write_text("typo = 1\n")
    try:
        Config.load(tmp_path, home=home, environ={})
        assert False
    except ConfigError as exc:
        assert "Unknown" in str(exc)


def test_v1_file_is_not_rewritten(tmp_path):
    home = _home(tmp_path)
    config_path = tmp_path / "nexus.toml"
    original = 'executable = "mycodex"\n'
    config_path.write_text(original)
    Config.load(tmp_path, home=home, environ={})
    assert config_path.read_text() == original


def test_precedence_user_then_workspace_then_env(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text(
        'executable = "user"\ncontext_chars = 2048\n'
    )
    (tmp_path / "nexus.toml").write_text('executable = "workspace"\n')
    config = Config.load(tmp_path, home=home, environ={})
    assert config.executable == "workspace"
    assert config.context_chars == 2048

    config = Config.load(
        tmp_path,
        home=home,
        environ={"NEXUS_EXECUTABLE": "env", "NEXUS_CONTEXT_CHARS": "4096"},
    )
    assert config.executable == "env"
    assert config.context_chars == 4096


def test_flags_and_session_have_highest_precedence(tmp_path):
    home = _home(tmp_path)
    config = Config.load(
        tmp_path,
        home=home,
        environ={"NEXUS_CONTEXT_CHARS": "4096"},
        flags={"context_chars": 8192},
        session={"context_chars": 16384},
    )
    assert config.context_chars == 16384


def test_v2_loads_sections_and_derives_legacy_facade(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        """
config_version = 2
[agent]
instructions_file = "GUIDE.md"
memory_file = "NOTES.md"
max_iterations = 12
[model]
default = "anthropic/claude-opus-5"
[context]
max_tokens = 100000
[providers.codex]
executable = "codex"
timeout_seconds = 42
"""
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 2
    assert config.v2 is not None
    assert config.v2.agent.max_iterations == 12
    assert config.v2.agent.name == "build"
    assert config.v2.context.max_tokens == 100000
    assert config.instructions_file == "GUIDE.md"
    assert config.memory_file == "NOTES.md"
    assert config.model == "anthropic/claude-opus-5"
    assert config.timeout_seconds == 42
    assert config.context_chars == 400000


def test_checked_in_workspace_config_uses_codex_responses_api(tmp_path):
    config = Config.load(REPO_ROOT, home=_home(tmp_path), environ={})

    assert config.version == 2
    assert config.v2 is not None
    assert config.v2.models.default == "codex/gpt-6-luna"
    assert config.v2.agent.sandbox == "workspace-write"
    assert config.v2.agent.instructions_file == "SOUL.md"
    assert config.v2.agent.memory_file == "MEMORY.md"
    assert config.v2.context.max_tokens == 16_000
    provider = config.v2.providers["codex"]
    assert provider.api_key is None
    assert provider.api == "responses"
    assert provider.auth == "chatgpt_oauth"
    assert provider.profile == "default"
    assert provider.executable is None


def test_v2_unknown_key_rejected(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text("config_version = 2\n[agent]\nbogus = 1\n")
    try:
        Config.load(tmp_path, home=home, environ={})
        assert False
    except ConfigError as exc:
        assert "Invalid v2" in str(exc)


def test_models_reasoning_effort_overrides_are_optional_and_validated(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text("config_version = 2\n")
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.models.reasoning_efforts == {}

    (tmp_path / "nexus.toml").write_text(
        '''config_version = 2
[models.reasoning_efforts]
"openai/o3" = ["max", "xhigh", "low"]
'''
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.models.reasoning_efforts == {
        "openai/o3": ["max", "xhigh", "low"]
    }


@pytest.mark.parametrize(
    "mapping",
    [
        {"o3": ["low"]},
        {"/o3": ["low"]},
        {"openai/": ["low"]},
        {"openai/o3": "low"},
        {"openai/o3": ["ultra"]},
        {"openai/o3": ["MAX"]},
        {"openai/o3": ["none", "minimal", "low", "medium", "high", "xhigh", "low"]},
        {"openai/o3": ["low", "low"]},
    ],
)
def test_models_reasoning_effort_overrides_reject_invalid_entries(tmp_path, mapping):
    from nexus.config.layers import build_v2

    with pytest.raises(ConfigError, match="reasoning_efforts"):
        build_v2({"config_version": 2, "models": {"reasoning_efforts": mapping}})


def test_mixed_v1_v2_in_one_document_rejected(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text('config_version = 2\nexecutable = "codex"\n')
    try:
        Config.load(tmp_path, home=home, environ={})
        assert False
    except ConfigError as exc:
        assert "Mixed" in str(exc)


def test_explicit_v1_config_version_loads(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text('config_version = 1\nexecutable = "mycodex"\n')
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 1
    assert config.executable == "mycodex"
    assert config.v2 is None


def test_v1_workspace_with_v2_home_is_bridged(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text(
        'config_version = 2\n[model]\ndefault = "home-model"\n'
    )
    (tmp_path / "nexus.toml").write_text(
        'executable = "mycodex"\ncontext_chars = 32000\n'
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 2
    assert config.model == "home-model"  # v2 home supplies the default
    assert config.executable == "mycodex"  # v1 workspace still wins its keys
    assert config.context_chars == 32000
    assert config.v2 is not None


def test_v2_home_sandbox_survives_v1_workspace(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text(
        'config_version = 2\n[agent]\nsandbox = "read-only"\n'
    )
    (tmp_path / "nexus.toml").write_text('executable = "mycodex"\n')
    config = Config.load(tmp_path, home=home, environ={})
    assert config.sandbox == "read-only"
    assert config.executable == "mycodex"


def test_v1_home_with_v2_workspace_is_bridged(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text('model = "home-model"\n')
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n[context]\nmax_tokens = 1000\n"
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 2
    assert config.model == "home-model"
    assert config.context_chars == 4000


def test_v1_workspace_unknown_key_still_rejected_with_v2_home(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text("config_version = 2\n")
    (tmp_path / "nexus.toml").write_text("typo = 1\n")
    try:
        Config.load(tmp_path, home=home, environ={})
        assert False
    except ConfigError as exc:
        assert "Unknown" in str(exc)


def test_config_hashable_and_legacy_equality():
    first, second = Config(), Config()
    assert first == second
    assert hash(first) == hash(second)
    assert {first: "value"}[second] == "value"

    bridged = Config(executable="codex", version=2, v2=build_v2({"config_version": 2}))
    assert bridged == Config(executable="codex")
    assert hash(bridged) == hash(Config(executable="codex"))


def test_v2_field_excluded_from_repr_but_not_equality():
    secret = "sk-ant-SUPER-SECRET-1234567890"
    v2 = build_v2(
        {"config_version": 2, "providers": {"anthropic": {"api_key": secret}}}
    )
    config = Config(executable="codex", version=2, v2=v2)
    assert config.v2.providers["anthropic"].api_key == secret
    assert secret not in repr(config)
    assert "v2=" not in repr(config)
    # Excluding v2 from repr must not disturb equality/hash compatibility.
    assert config == Config(executable="codex")
    assert hash(config) == hash(Config(executable="codex"))


def test_env_coercion_is_schema_aware_for_legacy(tmp_path):
    home = _home(tmp_path)
    config = Config.load(
        tmp_path,
        home=home,
        environ={
            "NEXUS_MODEL": "123",
            "NEXUS_TIMEOUT_SECONDS": "2.5",
            "NEXUS_CONTEXT_CHARS": "2048",
        },
    )
    assert config.model == "123"  # string field is not coerced to a number
    assert config.timeout_seconds == 2.5
    assert config.context_chars == 2048


def test_v2_env_coercion_preserves_credentials_and_model(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text("config_version = 2\n")
    config = Config.load(
        tmp_path,
        home=home,
        environ={
            "NEXUS_MODEL__DEFAULT": "123",
            "NEXUS_PROVIDERS__ANTHROPIC__API_KEY": "123456789",
            "NEXUS_CONTEXT__MAX_TOKENS": "999",
            "NEXUS_EXT__ENABLED": "false",
        },
    )
    assert config.v2.model.default == "123"
    assert config.v2.providers["anthropic"].api_key == "123456789"
    assert config.v2.context.max_tokens == 999
    assert config.v2.ext.enabled is False


def test_normalize_v1_to_v2_fragment():
    assert normalize_v1_to_v2(
        {
            "executable": "codex",
            "model": "m",
            "sandbox": "read-only",
            "context_chars": 8000,
        }
    ) == {
        "providers": {"codex": {"executable": "codex"}},
        "model": {"default": "m"},
        "agent": {"sandbox": "read-only"},
        "context": {"max_tokens": 2000},
    }


def test_sections_require_explicit_v2(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text("[agent]\nmax_iterations = 5\n")
    try:
        Config.load(tmp_path, home=home, environ={})
        assert False
    except ConfigError as exc:
        assert "config_version" in str(exc)


def test_permission_lists_append_and_dedupe_across_layers(tmp_path):
    home = _home(tmp_path)
    (home / ".nexus").mkdir()
    (home / ".nexus" / "config.toml").write_text(
        'config_version = 2\n[permissions]\nallow = ["Read(**)"]\n'
    )
    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n[permissions]\nallow = ["Glob(**)", "Read(**)"]\n'
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.permissions.allow == ["Read(**)", "Glob(**)"]


def test_v2_env_overlay(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text("config_version = 2\n")
    config = Config.load(
        tmp_path,
        home=home,
        environ={
            "NEXUS_CONTEXT__MAX_TOKENS": "12345",
            "NEXUS_AGENT__MAX_ITERATIONS": "7",
            "UNRELATED": "x",
        },
    )
    assert config.v2.context.max_tokens == 12345
    assert config.v2.agent.max_iterations == 7


def test_deep_merge_tables_append_scalars_replace():
    base = {"context": {"max_tokens": 1, "limits": {"memory": 2}}, "model": "a"}
    overlay = {"context": {"limits": {"environment": 3}}, "model": "b"}
    assert deep_merge(base, overlay) == {
        "context": {"max_tokens": 1, "limits": {"memory": 2, "environment": 3}},
        "model": "b",
    }


def test_env_overlay_v2_nesting():
    overlay = env_overlay_v2(
        {"NEXUS_CONTEXT__MAX_TOKENS": "123", "NEXUS_AGENT__MAX_ITERATIONS": "5", "X": "y"}
    )
    assert overlay == {
        "context": {"max_tokens": 123},
        "agent": {"max_iterations": 5},
    }


def test_detect_version_variants():
    assert detect_version({}, source="x") == 1
    assert detect_version({"model": "m"}, source="x") == 1
    assert detect_version({"config_version": 2}, source="x") == 2
    assert detect_version({"config_version": 1}, source="x") == 1
    try:
        detect_version({"config_version": 3}, source="x")
        assert False
    except ConfigError:
        pass


def test_build_v2_populates_defaults():
    config = build_v2({"config_version": 2})
    assert config.context.max_tokens is None  # the model window applies
    assert config.permissions.write_roots == ["./"]
    assert config.telemetry.log_level == "info"


def test_read_returns_file_within_workspace(tmp_path):
    (tmp_path / "SOUL.md").write_text("rules")
    assert Config().read(tmp_path, "SOUL.md") == "rules"
    assert Config().read(tmp_path, "MISSING.md") == ""


def test_read_rejects_parent_escape(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.md").write_text("x")
    try:
        Config().read(workspace, "../outside.md")
        assert False
    except ConfigError as exc:
        assert "inside workspace" in str(exc)


def test_read_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret")
    link = workspace / "link.md"
    try:
        link.symlink_to(secret)
    except OSError:
        return
    try:
        Config().read(workspace, "link.md")
        assert False
    except ConfigError as exc:
        assert "inside workspace" in str(exc)


def test_load_effective_does_not_touch_real_home(tmp_path):
    home = _home(tmp_path)
    effective = load_effective(tmp_path, home, {})
    assert effective.version == 1
    assert effective.source is None


def test_permissions_read_denyroots_default_and_parsing(tmp_path):
    home = _home(tmp_path)
    assert build_v2({"config_version": 2}).permissions.read_denyroots == []
    (tmp_path / "nexus.toml").write_text(
        """
config_version = 2
[permissions]
read_denyroots = ["~/.ssh", "~/.nexus/credentials.json"]
"""
    )
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.permissions.read_denyroots == [
        "~/.ssh",
        "~/.nexus/credentials.json",
    ]


def test_permissions_mode_and_unattended_validated(tmp_path):
    home = _home(tmp_path)
    for body in (
        '[permissions]\nmode = "sometimes"\n',
        '[permissions]\non_unattended = "maybe"\n',
    ):
        (tmp_path / "nexus.toml").write_text(f"config_version = 2\n{body}")
        try:
            Config.load(tmp_path, home=home, environ={})
            assert False, body
        except ConfigError:
            pass


def test_tool_numeric_fields_validated(tmp_path):
    home = _home(tmp_path)
    for body in (
        "[tools]\nmax_parallel = 0\n",
        "[tools]\nmax_parallel = -1\n",
        "[tools]\nbash_timeout_s = 0\n",
        "[tools]\nbash_timeout_s = -3\n",
        "[tools]\nbash_timeout_s = nan\n",
        "[tools]\nmax_result_tokens = 0\n",
        "[tools]\nmax_result_tokens = -10\n",
    ):
        (tmp_path / "nexus.toml").write_text(f"config_version = 2\n{body}")
        try:
            Config.load(tmp_path, home=home, environ={})
            assert False, body
        except ConfigError:
            pass


def test_tool_section_defaults_unchanged_for_legacy(tmp_path):
    home = _home(tmp_path)
    config = Config.load(tmp_path, home=home, environ={})
    assert config.version == 1
    assert config.v2 is None
    # A valid v2 document keeps the plan defaults.
    (tmp_path / "nexus.toml").write_text("config_version = 2\n")
    config = Config.load(tmp_path, home=home, environ={})
    assert config.v2.tools.bash_timeout_s == 120
    assert config.v2.tools.max_result_tokens == 25000
    assert config.v2.tools.max_parallel == 8


def test_web_tool_config_defaults_and_availability_reason():
    web = build_v2({"config_version": 2}).tools.web
    assert web.searxng_instances == []
    assert web.allowed_origins == []
    assert web.fetch_enabled is True
    assert web.search_timeout_s == 10.0
    assert web.fetch_timeout_s == 15.0
    assert web.max_results == 5
    assert web.max_query_length == 512
    assert web.max_output_bytes == 512_000
    assert web.search_available is False
    assert "No HTTPS SearXNG instance" in web.search_unavailable_reason


def test_web_tool_config_toml_and_env_overrides(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        '''config_version = 2
[tools.web]
searxng_instances = ["https://search.example/search"]
allowed_origins = ["https://docs.example", "https://docs.example:8443"]
search_timeout_s = 8.5
fetch_timeout_s = 12
max_results = 8
max_query_length = 700
max_output_bytes = 64000
'''
    )
    config = Config.load(
        tmp_path,
        home=home,
        environ={
            "NEXUS_TOOLS__WEB__FETCH_ENABLED": "false",
            "NEXUS_TOOLS__WEB__SEARCH_TIMEOUT_S": "6.5",
            "NEXUS_TOOLS__WEB__FETCH_TIMEOUT_S": "20",
            "NEXUS_TOOLS__WEB__MAX_RESULTS": "10",
            "NEXUS_TOOLS__WEB__MAX_QUERY_LENGTH": "1200",
            "NEXUS_TOOLS__WEB__MAX_OUTPUT_BYTES": "80000",
        },
    )
    web = config.v2.tools.web
    assert web.searxng_instances == ["https://search.example/search"]
    assert web.allowed_origins == ["https://docs.example", "https://docs.example:8443"]
    assert web.fetch_enabled is False
    assert web.search_timeout_s == 6.5
    assert web.fetch_timeout_s == 20
    assert web.max_results == 10
    assert web.max_query_length == 1200
    assert web.max_output_bytes == 80000
    assert web.search_available is True
    assert web.search_unavailable_reason is None


@pytest.mark.parametrize(
    "web",
    [
        {"searxng_instances": ["http://search.example"]},
        {"searxng_instances": ["https://user:pass@search.example"]},
        {"searxng_instances": ["https://search.example/#fragment"]},
        {"searxng_instances": ["https://search.example/search/extra"]},
        {"searxng_instances": ["https://search.example/?q=secret"]},
        {"searxng_instances": ["https://localhost/search"]},
        {"searxng_instances": ["https://127.0.0.1/search"]},
        {"searxng_instances": ["https://192.168.1.2/search"]},
        {"allowed_origins": ["https://user:pass@example.com"]},
        {"allowed_origins": ["https://example.com/path"]},
        {"allowed_origins": ["https://example.com?q=1"]},
        {"allowed_origins": ["http://127.0.0.1"]},
        {"search_timeout_s": 20.01},
        {"fetch_timeout_s": 0},
        {"max_results": 11},
        {"max_query_length": 4097},
        {"max_output_bytes": 2_000_001},
        {"fetch_enabled": "false"},
    ],
)
def test_web_tool_config_rejects_invalid_values(web):
    with pytest.raises(ConfigError, match="Invalid v2"):
        build_v2({"config_version": 2, "tools": {"web": web}})


def test_web_tool_numeric_environment_values_are_type_checked():
    with pytest.raises(ConfigError, match="Invalid v2"):
        build_v2(
            {
                "config_version": 2,
                "tools": {"web": {"search_timeout_s": "invalid"}},
            }
        )
