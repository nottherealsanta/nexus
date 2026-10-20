"""Gateway command policy: default deny, scope per command, bounded fields."""
from __future__ import annotations

import typing

import pytest

from nexus.gateway import policy
from nexus.host import protocol as p

ALL = ("read", "prompt", "approve", "voice", "live")

#: The complete allow-list, by name. Adding a command here is a security review.
EXPECTED_ALLOWED = {
    "read": {
        "SessionList", "SessionState", "ContextInspect", "ModelsList", "AgentsList",
        "AgentCurrent", "AgentTranscript", "LiveStatus", "VoiceStatus", "Health",
        "ToolsList", "GitDiff",
    },
    "prompt": {
        "SessionOpen", "SessionStart", "SessionEnqueue", "SessionCancel",
        "SessionQueueMove", "SessionQueueRemove", "SessionQueueSetMode", "ModelSelect",
        "ReasoningEffortSelect", "AgentSelect", "AgentCancel", "SessionArchive",
        "SessionUnarchive",
    },
    "approve": {"PermissionResolve", "QuestionAnswer"},
    "voice": {"VoiceCancel"},
    "live": {"LiveStart", "LiveMute", "LivePause", "LiveStop", "LiveHelperEvent"},
}


def command_types() -> list[type]:
    return list(typing.get_args(p.Command))


def test_the_allow_list_is_exactly_the_reviewed_set():
    actual: dict[str, set[str]] = {}
    for command_type, scope in policy.COMMAND_SCOPES.items():
        actual.setdefault(scope, set()).add(command_type.__name__)
    assert actual == EXPECTED_ALLOWED


def test_every_allowed_command_is_a_real_protocol_command():
    known = set(command_types())
    assert set(policy.COMMAND_SCOPES) <= known


def test_everything_else_is_default_deny():
    allowed = set(policy.COMMAND_SCOPES) | {p.LiveConsent}
    denied = [t for t in command_types() if t not in allowed]
    assert len(denied) > 20
    for command_type in denied:
        assert policy.COMMAND_SCOPES.get(command_type) is None, command_type.__name__
    names = {t.__name__ for t in denied}
    for must_deny in (
        "ProjectSessionsList", "ProjectSessionOpen", "SessionShell", "SettingsInventory",
        "SettingsRead", "SkillsInstall", "SettingsMcpAdd", "ProviderKeySet", "WebLaunch", "Shutdown", "SessionDelete",
        "SessionExport", "SessionRestore", "LogsRead", "AttachmentPrepare",
    ):
        assert must_deny in names, must_deny


@pytest.mark.parametrize(
    "command",
    [
        p.ProjectSessionsList(),
        p.ProjectSessionOpen(workspace="/tmp/other", session="s"),
        p.SessionShell(session="s", command="id"),
        p.SettingsInventory(scope="global"),
        p.SkillsInstall(scope="global", source="owner/repo"),
        p.SettingsMcpAdd(scope="global", name="x", entry={"env": {"K": "sk-secret"}, "command": "x"}),
        p.ProviderKeySet(provider="openai", key="sk-secret"),
        p.WebLaunch(),
        p.Shutdown(),
        p.SessionDelete(session="s"),
        p.SessionExport(session="s"),
        p.LogsRead(),
        p.AttachmentPrepare(path="/etc/passwd"),
    ],
    ids=lambda command: type(command).__name__,
)
def test_denied_commands_fail_even_with_every_scope(command):
    assert policy.required_scope(command) is None
    with pytest.raises(policy.CommandDenied) as raised:
        policy.authorize(command, ALL, "dev_1")
    assert raised.value.code == "command_denied"
    assert "sk-secret" not in raised.value.message


def test_a_missing_scope_is_a_distinct_denial():
    with pytest.raises(policy.CommandDenied) as raised:
        policy.authorize(p.SessionStart(session="s", content="hi"), ("read",), "dev_1")
    assert raised.value.code == "scope_required"
    assert "prompt" in raised.value.message
    assert policy.authorize(p.SessionStart(session="s", content="hi"), ("prompt",), "dev_1")
    assert policy.authorize(p.SessionList(), ("read",), "dev_1")
    with pytest.raises(policy.CommandDenied):
        policy.authorize(p.SessionList(), ("prompt", "approve", "voice", "live"), "dev_1")


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        (p.SessionList(), "read"),
        (p.Health(), "read"),
        (p.SessionOpen(session="s"), "prompt"),
        (p.SessionCancel(session="s"), "prompt"),
        (p.PermissionResolve(session="s", request_id="r1", decision="allow"), "approve"),
        (p.VoiceCancel(request_id="abc"), "voice"),
        (p.LiveStart(session="s", offer_sdp="v=0"), "live"),
        (p.LiveMute(session="s"), "live"),
        (p.LivePause(session="s"), "live"),
        (p.LiveStop(session="s"), "live"),
        (p.LiveHelperEvent(session="s", kind="ready"), "live"),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else value,
)
def test_each_command_needs_exactly_its_scope(command, scope):
    assert policy.required_scope(command) == scope
    assert policy.authorize(command, (scope,), "dev_1") is not None
    others = tuple(s for s in ALL if s != scope)
    with pytest.raises(policy.CommandDenied) as raised:
        policy.authorize(command, others, "dev_1")
    assert raised.value.code == "scope_required"


def test_live_consent_read_is_read_scope_but_writing_it_needs_live():
    assert policy.required_scope(p.LiveConsent(accept=None)) == "read"
    assert policy.required_scope(p.LiveConsent()) == "read"
    assert policy.required_scope(p.LiveConsent(accept=True)) == "live"
    assert policy.required_scope(p.LiveConsent(accept=False)) == "live"
    assert policy.authorize(p.LiveConsent(), ("read",), "dev_1")
    for accept in (True, False):
        with pytest.raises(policy.CommandDenied):
            policy.authorize(p.LiveConsent(accept=accept), ("read", "prompt", "approve", "voice"), "dev_1")
        assert policy.authorize(p.LiveConsent(accept=accept), ("live",), "dev_1")


def test_only_reads_are_safe_to_retry():
    assert policy.is_read_only(p.SessionList())
    assert policy.is_read_only(p.LiveConsent())
    assert not policy.is_read_only(p.LiveConsent(accept=True))
    assert not policy.is_read_only(p.SessionStart(session="s", content="x"))
    assert not policy.is_read_only(p.PermissionResolve(session="s", request_id="r", decision="allow"))
    assert not policy.is_read_only(p.Shutdown())
    assert not policy.is_read_only(p.LiveStart(session="s", offer_sdp="v=0"))


def test_permission_lease_holder_is_the_paired_device_not_the_client_choice():
    command = p.PermissionResolve(session="s", request_id="r1", decision="allow", client_id="attacker")
    rewritten = policy.authorize(command, ("approve",), "dev_abc")
    assert rewritten.client_id == "gateway:dev_abc"
    assert (rewritten.session, rewritten.request_id, rewritten.decision) == ("s", "r1", "allow")
    assert command.client_id == "attacker"
    assert policy.authorize(p.PermissionResolve(session="s", request_id="r1", decision="deny"), ("approve",), "dev_x").client_id == "gateway:dev_x"


# -- bounds -----------------------------------------------------------------------


def invalid(command, scopes=ALL):
    with pytest.raises(policy.CommandInvalid):
        policy.authorize(command, scopes, "dev_1")


@pytest.mark.parametrize("session", ["", "../etc", "a/b", "a b", "x" * 129, "s\n", "é", "a\x00"])
def test_session_ids_are_restricted(session):
    invalid(p.SessionOpen(session=session))
    invalid(p.SessionState(session=session), ("read",))
    invalid(p.LiveMute(session=session))
    invalid(p.LivePause(session=session))


def test_session_id_boundaries_are_accepted():
    for session in ("a", "x" * 128, "session-2026.10:13_a"):
        assert policy.authorize(p.SessionOpen(session=session), ("prompt",), "d")


def test_prompt_content_is_bounded_and_plain_text_only():
    ok = p.SessionStart(session="s", content="x" * policy.MAX_TEXT_CHARS)
    assert policy.authorize(ok, ("prompt",), "d")
    invalid(p.SessionStart(session="s", content="x" * (policy.MAX_TEXT_CHARS + 1)))
    invalid(p.SessionStart(session="s", content="bad\x00char"))
    invalid(p.SessionStart(session="s", content="x", attachments=["/etc/passwd"]))
    invalid(p.SessionStart(session="s", content="x", attachment_labels=["a"]))
    invalid(p.SessionEnqueue(session="s", mode="steer", content="x", attachments=["a"]))
    invalid(p.SessionStart(session="s", blocks=[{"type": "image", "text": "x"}]))
    invalid(p.SessionStart(session="s", blocks=[{"type": "text", "text": "x", "extra": 1}]))
    invalid(p.SessionStart(session="s", blocks=[{"type": "text", "text": 5}]))
    invalid(p.SessionStart(session="s", blocks=[{"type": "text", "text": "x"}] * (policy.MAX_BLOCKS + 1)))
    invalid(p.SessionStart(session="s", content="x" * policy.MAX_TEXT_CHARS, blocks=[{"type": "text", "text": "y"}]))
    assert policy.authorize(p.SessionStart(session="s", content="a\nb\tc", blocks=[{"type": "text", "text": "ok"}]), ("prompt",), "d")


def test_enqueue_mode_and_queue_operations_are_checked():
    for mode in ("queue", "steer", "interrupt"):
        assert policy.authorize(p.SessionEnqueue(session="s", mode=mode, content="x"), ("prompt",), "d")
    invalid(p.SessionEnqueue(session="s", mode="rm -rf", content="x"))
    assert policy.authorize(p.SessionQueueMove(session="s", queued_id="q1", offset=-1), ("prompt",), "d")
    invalid(p.SessionQueueMove(session="s", queued_id="q1", offset=0))
    invalid(p.SessionQueueMove(session="s", queued_id="q1", offset=2))
    invalid(p.SessionQueueMove(session="s", queued_id="bad id", offset=1))
    invalid(p.SessionQueueRemove(session="s", queued_id=""))
    assert policy.authorize(p.SessionQueueSetMode(session="s", queued_id="q1", mode="steer"), ("prompt",), "d")
    invalid(p.SessionQueueSetMode(session="s", queued_id="q1", mode="queue"))


def test_single_line_fields_refuse_newlines_and_oversize():
    invalid(p.SessionCancel(session="s", reason="a\nb"))
    invalid(p.SessionCancel(session="s", reason="x" * 201))
    invalid(p.ModelSelect(session="s", ref=""))
    invalid(p.ModelSelect(session="s", ref="m\n"))
    assert policy.authorize(p.ModelSelect(session="s", ref="openai/gpt"), ("prompt",), "d")
    invalid(p.ReasoningEffortSelect(session="s", effort="high effort"))
    assert policy.authorize(p.ReasoningEffortSelect(session="s", effort=None), ("prompt",), "d")
    invalid(p.AgentSelect(session="s", name=""))
    invalid(p.AgentCancel(session="s", agent_id="a/b"))
    invalid(p.GitDiff(ref="x" * 201), ("read",))
    invalid(p.ModelsList(search="a\nb"), ("read",))


def test_state_cursor_is_bounded():
    assert policy.authorize(p.SessionState(session="s", from_seq=0), ("read",), "d")
    invalid(p.SessionState(session="s", from_seq=-1), ("read",))
    invalid(p.SessionState(session="s", from_seq=2**53 + 1), ("read",))


def test_approval_fields_are_checked():
    invalid(p.PermissionResolve(session="s", request_id="r 1", decision="allow"), ("approve",))
    invalid(p.PermissionResolve(session="s", request_id="r1", decision="allow; rm"), ("approve",))
    invalid(p.QuestionAnswer(session="s", answer="x" * (policy.MAX_TEXT_CHARS + 1)), ("approve",))
    invalid(p.QuestionAnswer(session="s", answer="ok", call_id="bad id"), ("approve",))
    assert policy.authorize(p.QuestionAnswer(session="s", answer="yes", call_id="call_1"), ("approve",), "d")


def test_voice_and_live_fields_are_checked():
    invalid(p.VoiceCancel(request_id=""), ("voice",))
    invalid(p.VoiceCancel(request_id="a b"), ("voice",))
    invalid(p.LiveStart(session="s", offer_sdp=""), ("live",))
    invalid(p.LiveStart(session="s", offer_sdp="v" * (policy.MAX_OFFER_SDP_BYTES + 1)), ("live",))
    invalid(p.LiveStart(session="s", offer_sdp="é" * (policy.MAX_OFFER_SDP_BYTES // 2 + 1)), ("live",))
    invalid(p.LiveStart(session="s", offer_sdp="v=0", voice="a\nb"), ("live",))
    assert policy.authorize(p.LiveStart(session="s", offer_sdp="v" * policy.MAX_OFFER_SDP_BYTES), ("live",), "d")
    invalid(p.LiveStop(session="s", reason="x" * 41), ("live",))
    for kind in sorted(policy.HELPER_KINDS):
        assert policy.authorize(p.LiveHelperEvent(session="s", kind=kind), ("live",), "d")
    invalid(p.LiveHelperEvent(session="s", kind="exec"), ("live",))
    invalid(p.LiveHelperEvent(session="s", kind="ready", detail="x" * (policy.MAX_HELPER_DETAIL + 1)), ("live",))


def test_helper_kinds_mirror_the_live_manager():
    from nexus.live import manager

    assert policy.HELPER_KINDS == frozenset(manager.HELPER_KINDS)
    assert policy.MAX_HELPER_DETAIL == manager.MAX_HELPER_DETAIL


def test_field_less_commands_pass_unchanged():
    command = p.SessionList()
    assert policy.authorize(command, ("read",), "d") is command
    assert policy.authorize(p.Health(), ("read",), "d")
