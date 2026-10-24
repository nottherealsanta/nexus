"""The none/ask/always worktree modes are gone: the host protocol no longer carries them."""
import msgspec

from nexus.host import PROTOCOL_VERSION, protocol as p


def test_mode_and_draft_selection_commands_are_removed_and_the_protocol_is_bumped():
    assert PROTOCOL_VERSION >= 7
    assert not hasattr(p, "SessionWorktreeChoice") and not hasattr(p, "SessionWorktreeSet")
    for removed in ("SessionWorktreeChoice", "SessionWorktreeSet"):
        payload = msgspec.json.encode({"type": removed, "session": "s"})
        try:
            msgspec.json.decode(payload, type=p.Command)
        except (msgspec.ValidationError, msgspec.DecodeError):
            continue
        raise AssertionError(f"{removed} still decodes as a command")


def test_list_and_move_commands_round_trip():
    for command in (p.SessionWorktreeList(session="s"), p.SessionWorktreeMove(session="s", to="main", reason="done")):
        assert msgspec.json.decode(msgspec.json.encode(command), type=p.Command) == command
