"""``SettingsMcpAdd``: validate, insert one server byte-for-byte, hot-reload, never leak a secret."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.client.protocol import Client
from nexus.errors import ConfigError
from nexus.ext.manager import _strict_json_object
from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.host_support import settings_inventory as si

ENTRY = {"command": "b"}
SECRET_FIELD = "plain-words-secret-zq"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Extensions:
    def __init__(self, files=(), failed=()):
        self.reloads = []
        self.files = files
        self.failed = failed

    async def reload(self, trigger="api", sink=None):
        self.reloads.append(trigger)
        return SimpleNamespace(loaded=["mcp:b"], unloaded=[], failed=list(self.failed))

    def mcp_config_files(self):
        return tuple((scope, path, str(path)) for scope, path in self.files)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("NEXUS_HOME", str(home / ".nexus"))
    runtime = SimpleNamespace(workspace=workspace, extensions=_Extensions())
    return SimpleNamespace(home=home, workspace=workspace, runtime=runtime, tmp=tmp_path,
                           project=workspace / ".agents" / "mcp.json", user=home / ".nexus" / "mcp.json")


def _put(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


# -- the byte-preserving insert ------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    # multi-line with comments: the new property lands on its own line, the comma before a trailing comment
    ('{\n  // my servers\n  "mcpServers": {\n    "a": { "command": "a" } // keep\n  }\n}\n',
     '{\n  // my servers\n  "mcpServers": {\n    "a": { "command": "a" }, // keep\n    "b": {\n      "command": "b"\n    }\n  }\n}\n'),
    # a trailing comma stays a trailing comma
    ('{\n  "servers": {\n    "a": {"command": "a"},\n  },\n}\n',
     '{\n  "servers": {\n    "a": {"command": "a"},\n    "b": {\n      "command": "b"\n    },\n  },\n}\n'),
    # one line stays on one line
    ('{"mcpServers":{"a":{"command":"a"}}}', '{"mcpServers":{"a":{"command":"a"}, "b": {"command": "b"}}}'),
    ('{"servers":{}}', '{"servers":{"b": {"command": "b"}}}'),
    ('{"servers":{"a":{"command":"a"},}}', '{"servers":{"a":{"command":"a"}, "b": {"command": "b"},}}'),
    # no map yet: mcpServers is appended to the root object, other keys untouched
    ('{\n  "other": 1\n}\n', '{\n  "other": 1,\n  "mcpServers": {\n    "b": {\n      "command": "b"\n    }\n  }\n}\n'),
    ('{}', '{"mcpServers": {"b": {"command": "b"}}}'),
    # a block comment before the closing brace and a map of a different dialect
    ('{\n  "context_servers": {\n    "a": {"command": "a"}\n    /* end */\n  }\n}',
     '{\n  "context_servers": {\n    "a": {"command": "a"},\n    /* end */\n    "b": {\n      "command": "b"\n    }\n  }\n}'),
    ('{\n  "mcp": {\n  }\n}\n', '{\n  "mcp": {\n    "b": {\n      "command": "b"\n    }\n  }\n}\n'),
], ids=["comments", "trailing-comma", "one-line", "empty-map", "one-line-trailing", "new-map", "empty-root", "block-comment", "empty-multiline"])
def test_insert_preserves_every_other_byte(body, expected):
    result, key, note = si.insert_mcp_server(body, "b", ENTRY)
    assert result == expected
    parsed = _strict_json_object(result)
    assert parsed[key]["b"] == ENTRY
    assert note == ""


@pytest.mark.parametrize("body", ["", "   \n\t ", None])
def test_missing_or_blank_file_gets_a_pretty_document(body):
    result, key, _ = si.insert_mcp_server(body or "", "b", ENTRY)
    assert result == '{\n  "mcpServers": {\n    "b": {\n      "command": "b"\n    }\n  }\n}\n' and key == "mcpServers"


def test_crlf_files_stay_crlf():
    body = '{\r\n  "servers": {\r\n    "a": {"command": "a"}\r\n  }\r\n}\r\n'
    result, _, _ = si.insert_mcp_server(body, "b", ENTRY)
    assert result.count("\n") == result.count("\r\n") and _strict_json_object(result)["servers"]["b"] == ENTRY


def test_several_maps_use_the_first_in_loader_order_and_say_so():
    body = '{"mcpServers":{},"servers":{}}'
    result, key, note = si.insert_mcp_server(body, "b", ENTRY)
    assert key == "servers" and result == '{"mcpServers":{},"servers":{"b": {"command": "b"}}}'
    assert "several server maps" in note and "servers" in note


def test_the_entry_keeps_its_dialect_and_unicode():
    entry = {"type": "local", "command": ["npx", "-y", "srv"], "environment": {"K": "${env:K}"}, "description": "naïve ✓"}
    result, _, _ = si.insert_mcp_server('{"mcpServers": {}}', "b", entry)
    assert "naïve ✓" in result and _strict_json_object(result)["mcpServers"]["b"] == entry


def test_existing_names_in_any_map_exist_and_folded_names_collide():
    with pytest.raises(si.McpServerExists, match="already exists in mcpServers"):
        si.insert_mcp_server('{"servers":{},"mcpServers":{"b":{}}}', "b", ENTRY)
    with pytest.raises(ConfigError, match="collides"):
        si.insert_mcp_server('{"servers":{"my.srv":{}}}', "my_srv", ENTRY)
    with pytest.raises(ConfigError, match="collides"):
        si.insert_mcp_server('{"servers":{"my_srv":{}}}', "my-srv", ENTRY)
    with pytest.raises(ConfigError, match="collides"):
        si.insert_mcp_server('{"mcp":{"a-b":{}}}', "a.b", ENTRY)


@pytest.mark.parametrize("body", [
    '{"servers": {"a": ', '{"servers":{"a":{},"a":{}}}', '[]', '{"servers": []}', '{"servers": {}, "servers": {}}',
    "﻿" + '{"servers":{}}', '{"servers": {"a": NaN}}', 'null',
])
def test_unusable_files_are_refused_not_guessed_at(body):
    with pytest.raises(ConfigError):
        si.insert_mcp_server(body, "b", ENTRY)


def test_a_json_string_that_looks_like_a_comment_or_brace_is_not_mistaken_for_structure():
    body = '{\n  "servers": {\n    "a": {"command": "a", "args": ["// not a comment", "/* nor this */", "}", "{"]}\n  }\n}\n'
    result, _, _ = si.insert_mcp_server(body, "b", ENTRY)
    parsed = _strict_json_object(result)["servers"]
    assert parsed["a"]["args"] == ["// not a comment", "/* nor this */", "}", "{"] and parsed["b"] == ENTRY


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize("entry", [
    {"command": "uvx", "args": ["srv"]},
    {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${env:ZZ_UNSET_TOKEN_1}"}},
    {"type": "sse", "url": "https://example.com/sse"},
    {"serverUrl": "https://example.com/mcp"},
    {"type": "local", "command": ["npx", "-y", "srv"], "environment": {"A": "1"}},
    {"command": {"path": "srv", "args": ["x"], "env": {"A": "b"}}},
    {"command": "srv", "envFile": "/definitely/not/there/.env"},
    {"command": "srv", "enabled": False, "tool_loading": "all", "autoApprove": ["x"]},
    {"command": "${env:ZZ_UNSET_CMD}", "args": ["${workspaceFolder}", "${VAR:-fallback}"]},
])
def test_valid_entries_in_every_dialect_are_accepted(entry, env):
    result = si.add_mcp_server(env.runtime, "project", "srv", entry)
    assert result["status"] == "written"
    assert _strict_json_object(env.project.read_text())["mcpServers"]["srv"] == entry


@pytest.mark.parametrize("entry, why", [
    ({"command": "x", "bogus": 1}, "unknown keys"),
    ({"type": "ws", "url": "ws://x"}, "not supported"),
    ({"transport": "carrier-pigeon", "command": "x"}, "not supported"),
    ({"command": "x", "env": {"K": "${input:key}"}}, "prompts"),
    ({"url": "https://h/${input:path}"}, "prompts"),
    ({"command": "x", "env": {"K": "[redacted]"}}, "masked"),
    ({"command": "x", "headers": {"[redacted]": "v"}}, "masked"),
    ({"command": "x", "args": ["--token", "***"]}, "masked"),
    ({"args": ["x"]}, "requires command"),
    ({"type": "http"}, "requires url"),
    ({"url": "https://h", "command": "x"}, "must not set"),
    ({"command": "x", "url": "https://h"}, "must not set url"),
    ({"command": "x", "envFile": ".env", "env_file": ".env"}, "only one"),
    ({"command": "x", "envFile": 3}, "envFile"),
    ({"command": "x", "enabled": "yes"}, "enabled"),
    ({"command": "x", "tool_loading": "some"}, "tool_loading"),
    ({"command": "x", "args": [1]}, "args"),
    ({"command": "x", "env": "notamap"}, "env"),
    ({"command": "x", "connect_timeout_s": -1}, "positive"),
    ({"command": 5}, "command"),
    ({"command": "x" * 20_000}, "16 KiB"),
    ({}, "non-empty"),
])
def test_invalid_entries_write_nothing_and_say_why(entry, why, env):
    with pytest.raises(ConfigError, match=why):
        si.add_mcp_server(env.runtime, "project", "srv", entry)
    assert not env.project.exists() and not env.project.parent.exists()


@pytest.mark.parametrize("entry", ["srv", ["x"], None, 3])
def test_a_non_object_entry_is_refused(entry, env):
    with pytest.raises(ConfigError, match="non-empty object"):
        si.add_mcp_server(env.runtime, "project", "srv", entry)


@pytest.mark.parametrize("name", ["", "-a", ".a", "_a", "a b", "a/b", "a\n", "a" * 33, "é", "a:b", None, 3])
def test_bad_names_are_refused(name, env):
    with pytest.raises(ConfigError, match="name"):
        si.add_mcp_server(env.runtime, "project", name, {"command": "x"})
    assert not env.project.exists()


@pytest.mark.parametrize("name", ["a", "A1", "my-server", "my.server", "my_server", "a" * 32])
def test_good_names_are_accepted(name, env):
    assert si.add_mcp_server(env.runtime, "project", name, {"command": "x"})["status"] == "written"


def test_validation_never_reads_the_env_file_or_the_environment(env, monkeypatch):
    monkeypatch.delenv("ZZ_UNSET_TOKEN_1", raising=False)
    (env.workspace / ".env").write_text("SECRET=leak\n")
    entry = {"command": "x", "envFile": ".env", "env": {"A": "${env:ZZ_UNSET_TOKEN_1}"}}
    assert si.add_mcp_server(env.runtime, "project", "s", entry)["status"] == "written"


def test_errors_never_echo_a_draft_secret(env):
    secret = "hunter2-very-secret-value"
    entry = {"type": secret, "command": "x", "env": {"TOKEN": secret}}
    with pytest.raises(ConfigError) as raised:
        si.add_mcp_server(env.runtime, "project", "s", entry)
    assert secret not in str(raised.value) and "not supported" in str(raised.value)
    url_entry = {"url": "https://user:pa55w0rd-long@h/x?key=qs-secret-value", "command": "x"}
    with pytest.raises(ConfigError) as raised:
        si.add_mcp_server(env.runtime, "project", "s", url_entry)
    assert "pa55w0rd-long" not in str(raised.value) and "qs-secret-value" not in str(raised.value)


@pytest.mark.parametrize("entry", [
    {"command": "x", "mystery": SECRET_FIELD},
    {"command": "x", "args": [SECRET_FIELD, 3]},
    {"command": "x", "timeout": SECRET_FIELD},
    {"command": "x", "enabled": SECRET_FIELD},
    {"command": "x", "transportType": SECRET_FIELD},
    {"command": "x", "tool_loading": SECRET_FIELD},
    {"command": "x", "mystery": 1, "env": {"A": {"nested": [SECRET_FIELD]}}},
    {"url": SECRET_FIELD, "headers": [SECRET_FIELD]},
    {"type": SECRET_FIELD, "command": "x"},
    {"command": "x", "envFile": {"p": SECRET_FIELD}},
])
def test_errors_never_echo_unknown_or_invalid_field_values(entry, env):
    assert SECRET_FIELD in si._draft_secrets(entry)
    with pytest.raises(ConfigError) as raised:
        si.add_mcp_server(env.runtime, "project", "s", entry)
    assert SECRET_FIELD not in str(raised.value)
    assert not env.project.exists()


def test_nested_env_values_are_coerced_and_redacted_on_failure(env):
    entry = {"command": "x", "env": {"A": {"nested": [SECRET_FIELD]}}}
    assert SECRET_FIELD in si._draft_secrets(entry)
    env.runtime.extensions.failed = [f"s: bad env {SECRET_FIELD}"]
    result = _dispatch(env, scope="project", name="s", entry=entry, expected_sha256="")
    assert result.status == "written"
    assert all(SECRET_FIELD not in row for row in result.failed)


@pytest.mark.parametrize("short_entry, leaked", [
    ({"type": "bad", "command": "x"}, "bad"),
    ({"transportType": "foo", "command": "x"}, "foo"),
    ({"type": "pw", "command": "x"}, "pw"),
])
def test_errors_never_echo_short_draft_secrets(short_entry, leaked, env):
    assert leaked in si._draft_secrets(short_entry)
    with pytest.raises(ConfigError) as raised:
        si.add_mcp_server(env.runtime, "project", "s", short_entry)
    assert leaked not in str(raised.value)
    assert not env.project.exists()


def test_command_error_path_redacts_short_secrets(env):
    facade = HostFacade(env.runtime)
    result = asyncio.run(facade.handle(p.SettingsMcpAdd(scope="project", name="s", entry={"type": "bad", "command": "x"})))
    assert isinstance(result, p.ErrorResult)
    assert "bad" not in result.message


def test_reload_failures_redact_non_env_string_values(env):
    env.runtime.extensions.failed = [f"s: bad field {SECRET_FIELD!r}", f"s: {SECRET_FIELD} refused"]
    result = _dispatch(env, scope="project", name="s", entry={"command": "x", "args": [SECRET_FIELD]}, expected_sha256="")
    assert result.status == "written" and len(result.failed) == 2
    assert all(SECRET_FIELD not in row for row in result.failed) and SECRET_FIELD not in result.message


def test_repr_masks_the_entry():
    command = p.SettingsMcpAdd(scope="global", name="s", entry={"env": {"K": "sk-live-secretvalue"}, "command": "x"}, expected_sha256="")
    text = repr(command)
    assert "sk-live-secretvalue" not in text and "<masked>" in text and "name='s'" in text
    assert "sk-live-secretvalue" not in str(command) and "sk-live-secretvalue" not in f"{command!r}"


# -- the file: hashes, legacy, symlinks ------------------------------------------


def test_write_creates_the_project_file_and_returns_its_hash(env):
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "x"}, "")
    text = env.project.read_text()
    assert result["status"] == "written" and result["sha256"] == _sha(text)
    assert result["message"].startswith("s added to ") and "mcp.json" in result["message"]
    assert text == '{\n  "mcpServers": {\n    "s": {\n      "command": "x"\n    }\n  }\n}\n'
    assert not (env.workspace / ".nexus").exists()


def test_optional_hash_semantics(env):
    body = _put(env.project, '{"servers": {"a": {"command": "a"}}}')
    assert si.add_mcp_server(env.runtime, "project", "s", {"command": "x"}, "")["status"] == "conflict"   # "" = must not exist
    wrong = si.add_mcp_server(env.runtime, "project", "s", {"command": "x"}, "0" * 64)
    assert wrong["status"] == "conflict" and wrong["sha256"] == _sha(body) and env.project.read_text() == body
    assert si.add_mcp_server(env.runtime, "project", "s", {"command": "x"}, _sha(body))["status"] == "written"
    assert si.add_mcp_server(env.runtime, "project", "t", {"command": "x"}, None)["status"] == "written"   # None = current file
    assert si.add_mcp_server(env.runtime, "project", "u", {"command": "x"}, "")["status"] == "conflict"


def test_a_missing_file_with_a_hash_is_a_conflict(env):
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "x"}, "a" * 64)
    assert result["status"] == "conflict" and result["sha256"] == "" and not env.project.exists()


def test_an_existing_name_reports_exists_and_writes_nothing(env):
    body = _put(env.project, '{"mcpServers": {"s": {"command": "old"}}}')
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "new"})
    assert result["status"] == "exists" and "already exists" in result["message"] and result["sha256"] == _sha(body)
    assert env.project.read_text() == body


def test_a_file_that_changes_between_read_and_replace_is_a_conflict(env, monkeypatch):
    _put(env.project, '{"servers": {}}')
    real = si.insert_mcp_server

    def racing(body, name, entry):
        out = real(body, name, entry)
        env.project.write_text('{"servers": {"theirs": {"command": "t"}}}')
        return out

    monkeypatch.setattr(si, "insert_mcp_server", racing)
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    assert result["status"] == "conflict" and "theirs" in env.project.read_text() and '"s"' not in env.project.read_text()
    assert not [path for path in env.project.parent.iterdir() if path.name != "mcp.json"], "no temp file is left behind"


def test_legacy_project_servers_are_never_hidden_by_a_new_agents_file(env):
    legacy = _put(env.workspace / ".nexus" / "mcp.json", '{"servers": {"old": {"command": "o"}}}')
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    assert result["status"] == "legacy" and "legacy" in result["message"] and ".agents/mcp.json" in result["message"]
    assert not env.project.parent.exists() and (env.workspace / ".nexus" / "mcp.json").read_text() == legacy
    _put(env.project, '{"servers": {"old": {"command": "o"}}}')  # once migrated the add goes to .agents
    assert si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})["status"] == "written"
    assert (env.workspace / ".nexus" / "mcp.json").read_text() == legacy


def test_an_empty_legacy_folder_does_not_block_the_add(env):
    (env.workspace / ".nexus").mkdir()
    assert si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})["status"] == "written"


def test_siblings_are_neither_validated_nor_touched(env):
    body = ('{\n  // broken sibling and a literal secret that must stay exactly as written\n'
            '  "servers": {"bad": {"command": 5, "weird": true}, "ok": {"command": "o", "env": {"API_KEY": "real-secret-1234"}}}\n}\n')
    _put(env.project, body)
    result = si.add_mcp_server(env.runtime, "project", "s", {"command": "x", "env": {"API_KEY": "mine-secret-5678"}})
    text = env.project.read_text()
    assert result["status"] == "written" and "real-secret-1234" in text and "mine-secret-5678" in text
    assert body.splitlines()[1] in text and '"bad": {"command": 5, "weird": true}' in text


def test_the_new_entry_is_stored_verbatim_not_redaction_restored(env):
    _put(env.project, '{"servers": {"a": {"command": "a", "env": {"API_KEY": "old-secret-0000"}}}}')
    si.add_mcp_server(env.runtime, "project", "s", {"command": "x", "env": {"API_KEY": "new-secret-1111"}})
    servers = _strict_json_object(env.project.read_text())["servers"]
    assert servers["a"]["env"]["API_KEY"] == "old-secret-0000" and servers["s"]["env"]["API_KEY"] == "new-secret-1111"


@pytest.mark.parametrize("body", ["{", '{"servers": {"a": {}, "a": {}}}', "[]"])
def test_an_unusable_existing_file_is_left_alone(body, env):
    _put(env.project, body)
    with pytest.raises(ConfigError):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    assert env.project.read_text() == body


def test_oversized_and_non_utf8_files_are_refused(env):
    env.project.parent.mkdir(parents=True)
    env.project.write_bytes(b" " * (si.MAX_BODY + 1))
    with pytest.raises(ConfigError, match="256 KiB"):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    env.project.write_bytes(b'{"servers": {}, "x": "\xff"}')
    with pytest.raises(ConfigError, match="UTF-8"):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})


def test_a_directory_in_place_of_the_file_is_refused(env):
    env.project.mkdir(parents=True)
    with pytest.raises(ConfigError, match="regular file"):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})


def test_symlinks_are_refused_at_the_root_the_file_and_the_parent(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "mcp.json").write_text('{"servers": {}}')
    env.project.parent.symlink_to(outside)                      # .agents -> outside
    with pytest.raises(ConfigError, match="symlink"):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    env.project.parent.unlink()
    env.project.parent.mkdir()
    env.project.symlink_to(outside / "mcp.json")                # mcp.json -> outside
    with pytest.raises(ConfigError, match="symlink"):
        si.add_mcp_server(env.runtime, "project", "s", {"command": "x"})
    assert (outside / "mcp.json").read_text() == '{"servers": {}}'
    env.project.unlink()
    (env.home / ".nexus").symlink_to(outside)                   # ~/.nexus -> outside
    with pytest.raises(ConfigError, match="symlink"):
        si.add_mcp_server(env.runtime, "global", "s", {"command": "x"})
    assert sorted(path.name for path in outside.iterdir()) == ["mcp.json"]


def test_bad_scope_is_refused(env):
    with pytest.raises(ConfigError, match="scope"):
        si.add_mcp_server(env.runtime, "elsewhere", "s", {"command": "x"})


# -- global scope and NEXUS_HOME ----------------------------------------------


def test_global_add_writes_the_user_file(env):
    original = '{\n  "servers": {\n    "a": {"command": "a"}\n  }\n}\n'
    _put(env.user, original)
    result = si.add_mcp_server(env.runtime, "global", "s", {"command": "x"}, _sha(original))
    assert result["status"] == "written"
    text = env.user.read_text()
    assert text.startswith('{\n  "servers": {\n    "a": {"command": "a"},\n    "s": {') and not env.project.exists()


def test_global_add_refuses_when_the_host_reads_another_folder(env, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(env.tmp / "elsewhere"))
    with pytest.raises(ConfigError, match="NEXUS_HOME"):
        si.add_mcp_server(env.runtime, "global", "s", {"command": "x"})
    assert not env.user.exists() and not (env.tmp / "elsewhere").exists()
    # The folder the running host really reads decides, not the environment.
    env.runtime.extensions.files = (("global", env.user),)
    assert si.add_mcp_server(env.runtime, "global", "s", {"command": "x"})["status"] == "written"


# -- host command: reload, redaction, shadow notes ------------------------------


def _dispatch(env, **kwargs):
    command = p.SettingsMcpAdd(**kwargs)
    return asyncio.run(si.dispatch_settings(command, env.runtime))


def test_dispatch_reloads_once_and_reports_loaded_and_redacted_failures(env):
    secret = "hunter2-very-secret-value"
    env.runtime.extensions.failed = [f"s: connect failed token={secret}", f"s: {secret} refused"]
    result = _dispatch(env, scope="project", name="s", entry={"command": "x", "env": {"A": secret}}, expected_sha256="")
    assert isinstance(result, p.SettingsWriteResult) and result.status == "written"
    assert env.runtime.extensions.reloads == ["settings"] and result.loaded == ["mcp:b"]
    assert len(result.failed) == 2 and all(secret not in row for row in result.failed)
    assert secret not in result.message and secret in env.project.read_text()


@pytest.mark.parametrize("status", ["exists", "conflict", "legacy"])
def test_dispatch_does_not_reload_when_nothing_was_written(status, env):
    if status == "exists":
        _put(env.project, '{"servers": {"s": {"command": "o"}}}')
    elif status == "legacy":
        _put(env.workspace / ".nexus" / "mcp.json", "{}")
    else:
        _put(env.project, "{}")
    expected = "0" * 64 if status == "conflict" else None
    result = _dispatch(env, scope="project", name="s", entry={"command": "x"}, expected_sha256=expected)
    assert result.status == status and result.message and env.runtime.extensions.reloads == []


def test_dispatch_notes_scope_shadowing_after_the_reload(env):
    env.runtime.extensions.files = (("global", env.user), ("project", env.project))
    _put(env.project, '{"servers": {"dup": {"command": "p"}}}')
    result = _dispatch(env, scope="global", name="dup", entry={"command": "g"})
    assert result.status == "written" and "project server named 'dup' shadows" in result.message
    _put(env.user, '{"servers": {"other": {"command": "g"}}}')
    result = _dispatch(env, scope="project", name="other", entry={"command": "p"})
    assert result.status == "written" and "replaces the global 'other'" in result.message
    result = _dispatch(env, scope="project", name="unrelated", entry={"command": "p"})
    assert "replaces" not in result.message and "shadows" not in result.message


def test_a_runtime_without_extensions_still_writes(env):
    env.runtime.extensions = None
    assert _dispatch(env, scope="project", name="s", entry={"command": "x"}).status == "written"


def test_dispatch_errors_are_sanitized(env):
    secret = "hunter2-very-secret-value"
    with pytest.raises(ConfigError) as raised:
        _dispatch(env, scope="project", name="s", entry={"type": secret, "command": "x", "env": {"A": secret}})
    assert secret not in str(raised.value)


# -- client / facade / daemon / gateway ------------------------------------------


def test_client_round_trip_through_the_facade(env):
    class Transport:
        def __init__(self, facade):
            self.facade = facade

        async def request(self, command):
            return await self.facade.handle(command)

        async def aclose(self):
            pass

    runtime = type("RuntimeStub", (), {"workspace": env.workspace, "extensions": _Extensions()})()
    client = Client(Transport(HostFacade(runtime)))
    result = asyncio.run(client.settings_mcp_add("project", "s", {"command": "x"}, ""))
    assert isinstance(result, p.SettingsWriteResult) and result.status == "written"
    again = asyncio.run(client.settings_mcp_add("project", "s", {"command": "x"}))
    assert again.status == "exists"
    assert json.loads(env.project.read_text())["mcpServers"]["s"] == {"command": "x"}


# -- dry-run preview (the Settings surface never loads MCP code) -----------------


def test_preview_writes_nothing_starts_nothing_and_masks_values(env):
    entry = {"command": "/opt/bin/pg-mcp", "args": ["--ro", "x"], "env": {"PASSWORD": "sk-live-super-secret", "OK": "${env:OK}"}}
    result = _dispatch(env, scope="project", name="db", entry=entry, expected_sha256="", dry_run=True)
    assert result.status == "preview" and result.transport == "stdio"
    text = "\n".join(result.lines)
    for expected in ("Name: db", "<project>/.agents/mcp.json (project)", "Transport: stdio", "Command: pg-mcp · 2 arguments",
                     "Environment: PASSWORD, OK (values hidden)", "may be committed", "starts this command"):
        assert expected in text, expected
    assert "sk-live-super-secret" not in text and "/opt/bin" not in text
    assert not env.project.exists() and env.runtime.extensions.reloads == []


def test_preview_url_server_reports_transport_and_hides_credentials(env):
    entry = {"url": "https://u:pw-secret-1@Host.example:8443/mcp?key=qs-secret-2", "headers": {"Authorization": "Bearer hdr-secret-3"}}
    result = _dispatch(env, scope="global", name="api", entry=entry, dry_run=True)
    text = "\n".join(result.lines)
    assert result.status == "preview" and result.transport != "stdio"
    assert "URL: https://host.example:8443/mcp" in text and "~/.nexus/mcp.json (global)" in text and "connects to this URL" in text
    for secret in ("pw-secret-1", "qs-secret-2", "hdr-secret-3"):
        assert secret not in text
    assert not env.user.exists()


def test_preview_reports_exists_conflict_legacy_and_validation_errors_before_any_confirmation(env):
    _put(env.project, '{"servers": {"s": {"command": "o"}}}')
    assert _dispatch(env, scope="project", name="s", entry={"command": "x"}, dry_run=True).status == "exists"
    assert _dispatch(env, scope="project", name="t", entry={"command": "x"}, expected_sha256="0" * 64, dry_run=True).status == "conflict"
    env.project.unlink()
    _put(env.workspace / ".nexus" / "mcp.json", "{}")
    assert _dispatch(env, scope="project", name="t", entry={"command": "x"}, dry_run=True).status == "legacy"
    secret = "hunter2-very-secret-value"
    with pytest.raises(ConfigError) as raised:
        _dispatch(env, scope="project", name="t", entry={"type": secret, "command": "x", "env": {"A": secret}}, dry_run=True)
    assert secret not in str(raised.value)
    assert env.runtime.extensions.reloads == []


def test_preview_applies_the_global_root_refusal(env, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(env.tmp / "elsewhere"))
    with pytest.raises(ConfigError, match="NEXUS_HOME"):
        _dispatch(env, scope="global", name="s", entry={"command": "x"}, dry_run=True)


def test_client_preview_round_trip_and_default_denied_to_devices(env):
    class Transport:
        def __init__(self, facade):
            self.facade = facade

        async def request(self, command):
            return await self.facade.handle(command)

        async def aclose(self):
            pass

    runtime = type("RuntimeStub", (), {"workspace": env.workspace, "extensions": _Extensions()})()
    client = Client(Transport(HostFacade(runtime)))
    result = asyncio.run(client.settings_mcp_add_preview("project", "s", {"command": "x"}))
    assert result.status == "preview" and result.transport == "stdio" and not env.project.exists()
    from nexus.gateway import policy
    command = p.SettingsMcpAdd(scope="global", name="s", entry={"command": "x"}, dry_run=True)
    assert "secret" not in repr(command) and "dry_run=True" in repr(command)
    with pytest.raises(policy.CommandDenied):
        policy.authorize(command, {"read", "prompt", "approve", "voice", "live"}, "dev_1")


def test_protocol_union_codec_and_defaults():
    command = p.SettingsMcpAdd(scope="global", name="s", entry={"command": "x", "args": ["-y"]})
    assert command.expected_sha256 is None and command in [command] and p.SettingsMcpAdd in p.COMMANDS
    assert p.decode_command(p.encode_command(command)) == command
    assert "SettingsMcpAdd" in p.__all__
    assert p.SettingsWriteResult(status="written").message == ""
    with pytest.raises(Exception):
        p.decode_command(p.encode_command(command).replace(b'"global"', b'"galaxy"'))


def test_it_is_a_slow_daemon_command_and_denied_to_devices():
    from nexus.gateway import policy
    from nexus.host import daemon
    assert p.SettingsMcpAdd in daemon.SLOW_COMMANDS
    command = p.SettingsMcpAdd(scope="global", name="s", entry={"command": "x"})
    assert policy.required_scope(command) is None
    with pytest.raises(policy.CommandDenied):
        policy.authorize(command, {"read", "prompt", "approve", "voice", "live"}, "dev_1")
