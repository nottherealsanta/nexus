"""Phase 4 P4-C: quarantine — validate exact bytes before they are imported.

Covers the plan section 6.4 gates and the honest boundary from section 11:

* the frozen bytes are the single source of truth (root/symlink/size/UTF-8/
  hash), and a race between validation and execution is refused, not raced;
* static checks: ``ast.parse``, contract-symbol shape, warn-list warnings;
* the isolated import contains syntax errors, import crashes, ``os._exit``, and
  import-time hangs without ever touching the parent;
* schema/name/case-fold/builtin collisions are refused;
* no diagnostic ever carries control characters, a source body, or a secret.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from nexus.ext.quarantine import (
    CHILD_BOOTSTRAP,
    DEFAULT_MAX_FILE_BYTES,
    Quarantine,
    QuarantineCode,
    QuarantineError,
    sanitize_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extensions"


def fixture(name: str) -> Path:
    return FIXTURES / name


def quarantiner(**overrides) -> Quarantine:
    kwargs = {
        "max_file_bytes": 100_000,
        "timeout_s": 2.0,
        "root": FIXTURES,
        "stage_root": None,
    }
    kwargs.update(overrides)
    return Quarantine(**kwargs)


# ---------------------------------------------------------------------------
# Stage 1: the bounded, exact read
# ---------------------------------------------------------------------------


def test_open_freezes_bytes_and_hashes_them():
    q = quarantiner()
    staged = q.open(fixture("echo_spec.py"))
    expected = hashlib.sha256(fixture("echo_spec.py").read_bytes()).hexdigest()
    assert staged.sha256 == expected
    assert staged.size == len(staged.data)
    assert staged.data == fixture("echo_spec.py").read_bytes()
    assert staged.text.startswith('"""')


def test_open_reports_missing_file():
    q = quarantiner()
    outcome = q.inspect_only(FIXTURES / "does_not_exist.py")
    assert outcome.code is QuarantineCode.MISSING
    assert outcome.ok is False


def test_open_refuses_a_symlinked_file(tmp_path: Path):
    real = tmp_path / "real.py"
    real.write_text("SPEC = {}\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    q = quarantiner(root=tmp_path)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(link)
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


def test_open_refuses_a_symlinked_parent_below_the_root(tmp_path: Path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "tool.py").write_text("SPEC = {}\n", encoding="utf-8")
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)
    q = quarantiner(root=tmp_path)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(linked_dir / "tool.py")
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


def test_open_refuses_a_directory():
    q = quarantiner()
    outcome = q.inspect_only(FIXTURES)
    assert outcome.code is QuarantineCode.NOT_A_FILE


def test_open_refuses_an_oversize_file_then_inspect_bytes_agrees():
    q = quarantiner(max_file_bytes=32)
    outcome = q.inspect_only(fixture("echo_spec.py"))
    assert outcome.code is QuarantineCode.OVERSIZE
    assert q.inspect_bytes(b"x" * 64).code is QuarantineCode.OVERSIZE


def test_max_file_bytes_validation():
    with pytest.raises(QuarantineError):
        Quarantine(max_file_bytes=0)
    with pytest.raises(QuarantineError):
        Quarantine(max_file_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(QuarantineError):
        Quarantine(timeout_s=0)
    with pytest.raises(QuarantineError):
        Quarantine(timeout_s=-1.0)


def test_open_refuses_non_utf8(tmp_path: Path):
    bad = tmp_path / "latin.py"
    bad.write_bytes(b"SPEC = {}\n# \xff\xfe not utf8\n")
    q = quarantiner(root=tmp_path)
    outcome = q.inspect_only(bad)
    assert outcome.code is QuarantineCode.NOT_UTF8


def test_open_refuses_a_nul_byte(tmp_path: Path):
    bad = tmp_path / "nul.py"
    bad.write_bytes(b"SPEC = {}\x00\n")
    q = quarantiner(root=tmp_path)
    outcome = q.inspect_only(bad)
    assert outcome.code is QuarantineCode.NUL_BYTE


def test_read_bounded_refuses_a_file_that_changed_size(tmp_path: Path, monkeypatch):
    import nexus.ext.quarantine as mod

    target = tmp_path / "changing.py"
    target.write_text("SPEC = {}\n" * 100, encoding="utf-8")
    q = quarantiner(root=tmp_path)

    real_open = Path.open

    class _Shrunk:
        """A file handle that returns EOF before the recorded size."""

        def __init__(self, handle):
            self._handle = handle
            self._returned = False

        def read(self, size=-1):
            if self._returned:
                return b""
            self._returned = True
            return self._handle.read(4)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def close(self):
            self._handle.close()

    def fake_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if "r" in mode and "b" in mode:
            return _Shrunk(handle)
        return handle

    monkeypatch.setattr(mod.Path, "open", fake_open)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(target)
    assert excinfo.value.code is QuarantineCode.HASH_MISMATCH  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Stage 2: static checks
# ---------------------------------------------------------------------------


def test_syntax_error_is_caught_statically():
    q = quarantiner()
    outcome = q.inspect(q.open(fixture("broken_syntax.py")))
    assert outcome.code is QuarantineCode.SYNTAX_ERROR
    assert outcome.error_type == "SyntaxError"
    # The compiler's own one-liner is fine; the offending source *line* is not.
    assert "description" not in outcome.detail
    assert "BrokenFixture" not in outcome.detail


def test_no_contract_is_detected_from_the_ast():
    q = quarantiner()
    outcome = q.inspect(q.open(fixture("no_contract.py")))
    assert outcome.code is QuarantineCode.NO_CONTRACT


def test_both_contracts_is_syntactically_ambiguous():
    q = quarantiner()
    outcome = q.inspect(q.open(fixture("ambiguous.py")))
    assert outcome.code is QuarantineCode.OK
    assert outcome.declaration == "ambiguous"


def test_warn_list_flags_import_time_side_effects():
    q = quarantiner()
    outcome = q.inspect(q.open(fixture("warns.py")))
    assert outcome.ok is True
    assert any("subprocess" in warning for warning in outcome.warnings)
    assert any("getstatusoutput" in warning for warning in outcome.warnings)


def test_warn_list_ignores_plain_declarations():
    q = quarantiner()
    outcome = q.inspect(q.open(fixture("echo_spec.py")))
    assert outcome.warnings == ()


def test_warn_list_skips_the_main_guard():
    source = (
        b"import subprocess\n"
        b"SPEC = {'name': 'G', 'description': 'd', "
        b"'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        b"async def run(args, ctx):\n    return None\n"
        b"if __name__ == '__main__':\n    subprocess.getstatusoutput('true')\n"
    )
    q = quarantiner()
    outcome = q.inspect_bytes(source)
    assert outcome.ok is True
    assert outcome.warnings == ("import-time import of 'subprocess'",)


# ---------------------------------------------------------------------------
# Stage 3: isolated subprocess import
# ---------------------------------------------------------------------------


def test_isolated_import_succeeds_for_spec_form():
    q = quarantiner()
    staged = q.open(fixture("echo_spec.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.OK
    assert outcome.declaration == "spec"
    assert outcome.tools == ("EchoFixture",)


def test_isolated_import_succeeds_for_register_form():
    q = quarantiner()
    staged = q.open(fixture("register_tool.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.OK
    assert outcome.declaration == "register"
    assert outcome.tools == ("RegisterFixture",)


def test_isolated_import_contains_a_hang(tmp_path: Path):
    q = quarantiner(timeout_s=1.0)
    staged = q.open(fixture("hangs.py"))
    started = time.monotonic()
    outcome = q.run_isolated(staged)
    elapsed = time.monotonic() - started
    assert outcome.code is QuarantineCode.IMPORT_TIMEOUT
    assert elapsed < 5.0
    assert outcome.detail == "import timed out"
    # No orphaned child survives the TERM/KILL process-group teardown.
    time.sleep(0.1)
    listing = subprocess.run(
        ["ps", "-A", "-o", "command"], capture_output=True, text=True, check=False
    ).stdout
    assert "hangs.py" not in listing


def test_isolated_import_contains_os_exit():
    q = quarantiner()
    staged = q.open(fixture("hard_exit.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.IMPORT_EXIT
    assert "code 7" in outcome.detail


def test_isolated_import_reports_an_import_exception_without_the_traceback():
    q = quarantiner()
    staged = q.open(fixture("import_error.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.IMPORT_EXIT
    assert outcome.detail == "RuntimeError"
    assert "boom" not in outcome.detail


def test_isolated_import_rejects_a_sync_run():
    q = quarantiner()
    staged = q.open(fixture("sync_run.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.SYNC_RUN


def test_isolated_import_rejects_an_ambiguous_module():
    q = quarantiner()
    staged = q.open(fixture("ambiguous.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.AMBIGUOUS_CONTRACT


def test_isolated_import_rejects_a_bad_schema():
    q = quarantiner()
    staged = q.open(fixture("bad_schema.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.BAD_SPEC
    assert "input_schema" in outcome.detail


def test_isolated_import_of_a_no_contract_module():
    q = quarantiner()
    staged = q.open(fixture("no_contract.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.NO_CONTRACT


def test_isolated_child_is_run_with_a_minimal_environment(monkeypatch, tmp_path: Path):
    # A parent environment variable must not reach the child. The child is a
    # fresh interpreter with an allow-listed env, so a poisoned PYTHONPATH or a
    # credential never crosses the boundary.
    monkeypatch.setenv("NEXUS_TEST_SECRET", "sk-should-never-cross")
    source = (
        b"import os\n"
        b"SPEC = {'name': 'Env', 'description': 'd', "
        b"'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        b"LEAK = os.environ.get('NEXUS_TEST_SECRET', '')\n"
        b"assert LEAK == '', LEAK\n"
        b"async def run(args, ctx):\n    return None\n"
    )
    probe = tmp_path / "envprobe.py"
    probe.write_bytes(source)
    q = quarantiner(root=tmp_path)
    outcome = q.run_isolated(q.open(probe))
    assert outcome.code is QuarantineCode.OK
    assert outcome.tools == ("Env",)


def test_child_bootstrap_does_not_import_nexus():
    assert "nexus" not in CHILD_BOOTSTRAP.replace("nexus_ext", "")


def test_output_oversize_is_a_distinct_refusal(tmp_path: Path):
    source = (
        b"import sys\n"
        b"sys.stderr.write('x' * 200000)\n"
        b"SPEC = {'name': 'Noisy', 'description': 'd', "
        b"'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        b"async def run(args, ctx):\n    return None\n"
    )
    noisy = tmp_path / "noisy.py"
    noisy.write_bytes(source)
    q = quarantiner(root=tmp_path, output_limit=1024)
    staged = q.open(noisy)
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.OUTPUT_OVERSIZE


# ---------------------------------------------------------------------------
# Stage 4: staging and exact-hash verification
# ---------------------------------------------------------------------------


def test_stage_copies_exact_bytes_and_verifies(tmp_path: Path):
    q = quarantiner(stage_root=tmp_path / "stage")
    staged = q.open(fixture("echo_spec.py"))
    copy = q.stage(staged)
    assert copy.path != staged.path
    assert copy.data == staged.data
    assert copy.sha256 == staged.sha256
    assert q.verify(copy, staged.sha256) is True
    # Idempotent: staging twice yields the same content-addressed path.
    again = q.stage(staged)
    assert again.path == copy.path


def test_finalize_refuses_a_changed_stage(tmp_path: Path):
    q = quarantiner(stage_root=tmp_path / "stage")
    staged = q.open(fixture("echo_spec.py"))
    copy = q.stage(staged)
    copy.path.write_bytes(copy.data + b"\n# edited\n")
    assert q.verify(copy, staged.sha256) is False
    with pytest.raises(QuarantineError) as excinfo:
        q.finalize(copy, staged.sha256)
    assert excinfo.value.code is QuarantineCode.HASH_MISMATCH  # type: ignore[attr-defined]


def test_verify_returns_false_when_the_stage_is_gone(tmp_path: Path):
    q = quarantiner(stage_root=tmp_path / "stage")
    staged = q.open(fixture("echo_spec.py"))
    copy = q.stage(staged)
    copy.path.unlink()
    assert q.verify(copy, staged.sha256) is False


def test_stage_marks_the_copy_private_and_discard_removes_it(tmp_path: Path):
    q = quarantiner(stage_root=tmp_path / "stage")
    staged = q.open(fixture("echo_spec.py"))
    assert staged.private is False
    copy = q.stage(staged)
    assert copy.private is True
    assert copy.path.exists()
    assert q.discard_staged(copy) is True
    assert copy.path.exists() is False
    # Idempotent: a second discard finds nothing.
    assert q.discard_staged(copy) is False


def test_discard_never_touches_a_workspace_source(tmp_path: Path):
    # A source read directly (not staged) is the user's file; cleanup must not
    # be able to delete it.
    source = tmp_path / "workspace_tool.py"
    source.write_text("SPEC = {}\n", encoding="utf-8")
    q = quarantiner(root=tmp_path)
    staged = q.open(source)
    assert staged.private is False
    assert q.discard_staged(staged) is False
    assert source.exists()


def test_source_identity_is_stable_absolute_and_location_sensitive(tmp_path: Path):
    q = quarantiner(root=tmp_path)
    first = tmp_path / "a" / "tool.py"
    second = tmp_path / "b" / "tool.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("SPEC = {}\n", encoding="utf-8")
    second.write_text("SPEC = {}\n", encoding="utf-8")
    staged_a = q.open(first)
    staged_b = q.open(second)
    assert Path(staged_a.source_identity).is_absolute()
    # Same stem, different location: the identities (and therefore the module
    # names derived from them) differ.
    assert staged_a.source_identity != staged_b.source_identity
    assert staged_a.source_identity == str(first.resolve())


# ---------------------------------------------------------------------------
# Name, schema, and collision checks
# ---------------------------------------------------------------------------


def _spec(**overrides):
    from nexus.tools.spec import ToolSpec

    kwargs = {
        "name": "Good",
        "description": "d",
        "input_schema": {"type": "object"},
        "bundle": "fs",
    }
    kwargs.update(overrides)
    return ToolSpec(**kwargs)


def test_check_names_refuses_a_builtin_collision():
    q = quarantiner(builtin_names=["Read", "Write"])
    with pytest.raises(QuarantineError) as excinfo:
        q.check_names([_spec(name="Read")])
    assert excinfo.value.code is QuarantineCode.BUILTIN_COLLISION  # type: ignore[attr-defined]


def test_check_names_refuses_a_reserved_collision():
    q = quarantiner()
    with pytest.raises(QuarantineError) as excinfo:
        q.check_names([_spec(name="Taken")], reserved_names=["Taken"])
    assert excinfo.value.code is QuarantineCode.DUPLICATE_TOOL  # type: ignore[attr-defined]


def test_check_names_refuses_an_intra_batch_duplicate():
    q = quarantiner()
    with pytest.raises(QuarantineError) as excinfo:
        q.check_names([_spec(name="Same"), _spec(name="Same")])
    assert excinfo.value.code is QuarantineCode.DUPLICATE_TOOL  # type: ignore[attr-defined]


def test_check_names_refuses_a_casefold_collision():
    q = quarantiner()
    with pytest.raises(QuarantineError) as excinfo:
        q.check_names([_spec(name="Read"), _spec(name="read")])
    assert excinfo.value.code is QuarantineCode.CASE_COLLISION  # type: ignore[attr-defined]


def test_check_names_accepts_distinct_names():
    q = quarantiner()
    q.check_names([_spec(name="Alpha"), _spec(name="Beta")])


# ---------------------------------------------------------------------------
# Diagnostics: sanitized, control-free, no secrets, no source body
# ---------------------------------------------------------------------------


def test_sanitize_text_strips_controls_and_redacts_secrets():
    raw = "line1\nline2\t\x00 api_key=sk-supersecretvalue12345"
    cleaned = sanitize_text(raw)
    assert "\n" not in cleaned and "\t" not in cleaned and "\x00" not in cleaned
    assert "sk-supersecretvalue" not in cleaned
    assert "***" in cleaned


def test_secret_in_an_import_error_never_reaches_the_outcome():
    q = quarantiner()
    staged = q.open(fixture("secret_error.py"))
    outcome = q.run_isolated(staged)
    assert outcome.code is QuarantineCode.IMPORT_EXIT
    blob = repr(outcome.to_dict())
    assert "sk-supersecretvalue" not in blob
    assert "api_key" not in blob
    assert outcome.error_type == "IsolatedImport"


def test_outcome_to_dict_is_json_safe():
    import json

    q = quarantiner()
    for name in ("echo_spec.py", "broken_syntax.py", "bad_schema.py"):
        staged = q.open(fixture(name))
        outcome = q.inspect(staged)
        json.dumps(outcome.to_dict())
        assert "\x00" not in repr(outcome.to_dict())


def test_nested_mapping_is_frozen_and_paths_are_absolute():
    q = quarantiner()
    staged = q.open(fixture("echo_spec.py"))
    assert staged.path.is_absolute()
    assert isinstance(staged.data, bytes)


def test_default_max_file_bytes_matches_config_default():
    assert DEFAULT_MAX_FILE_BYTES == 262_144


# ---------------------------------------------------------------------------
# Parent-symlink consistency: lexical roots, OS prefixes, and rootless mode
# ---------------------------------------------------------------------------


def test_open_allows_a_symlinked_root_and_refuses_below_it(tmp_path: Path):
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    (real_root / "tool.py").write_text("SPEC = {}\n", encoding="utf-8")
    linked_root = tmp_path / "linked_root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    q = quarantiner(root=linked_root)  # a lexical root that resolves elsewhere
    # The symlink that *is* the root is host layout and is allowed.
    assert q.open(linked_root / "tool.py").sha256

    # A symlinked component strictly below the root is refused.
    outside = tmp_path / "outside"
    outside.mkdir()
    (real_root / "sub_link").symlink_to(outside, target_is_directory=True)
    (outside / "x.py").write_text("SPEC = {}\n", encoding="utf-8")
    with pytest.raises(QuarantineError) as excinfo:
        q.open(linked_root / "sub_link" / "x.py")
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


def test_open_rootless_refuses_a_symlinked_parent(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "tool.py").write_text("SPEC = {}\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    # No root means no boundary to exempt: every symlinked component is refused
    # rather than the check being skipped entirely.
    q = Quarantine(max_file_bytes=100_000, timeout_s=0.3)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(link / "tool.py")
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


@pytest.mark.skipif(
    not os.path.islink("/tmp"), reason="/tmp is not a symlink on this platform"
)
def test_open_handles_a_symlinked_os_prefix(tmp_path: Path):
    lexical_root = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        (lexical_root / "tool.py").write_text("SPEC = {}\n", encoding="utf-8")
        q = quarantiner(root=lexical_root)
        # ``/tmp`` is a symlink to ``/private/tmp`` on macOS; it is a lexical
        # component of the configured root, so the prefix is tolerated while the
        # file is still read. The policy is lexical, not target-based: the
        # symlink's target is never resolved.
        assert q.open(lexical_root / "tool.py").sha256
    finally:
        shutil.rmtree(lexical_root, ignore_errors=True)


def test_open_refuses_an_in_tree_symlink_to_an_ancestor(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SPEC = {}\n", encoding="utf-8")
    # ``ws/up -> ws.parent`` is a symlink strictly below the root that points at
    # an ancestor; its target contains the root, but lexically it is an in-tree
    # link and must be refused.
    (workspace / "up").symlink_to(tmp_path, target_is_directory=True)
    q = quarantiner(root=workspace)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(workspace / "up" / "outside.py")
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


def test_open_refuses_an_in_tree_symlink_to_the_filesystem_root(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    victim = workspace / "victim.py"
    victim.write_text("SPEC = {}\n", encoding="utf-8")
    # ``ws/rootlink -> /`` points at the filesystem root; the final component is
    # a regular file reached through it, but the symlinked component is below
    # the root and is refused.
    (workspace / "rootlink").symlink_to(os.sep, target_is_directory=True)
    candidate = workspace / "rootlink" / victim.relative_to(Path(os.sep))
    q = quarantiner(root=workspace)
    with pytest.raises(QuarantineError) as excinfo:
        q.open(candidate)
    assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]


@pytest.mark.skipif(
    not os.path.islink("/tmp"), reason="/tmp is not a symlink on this platform"
)
def test_open_refuses_a_symlink_below_a_lexical_tmp_root(tmp_path: Path):
    lexical_root = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        real = lexical_root / "real"
        real.mkdir()
        (real / "tool.py").write_text("SPEC = {}\n", encoding="utf-8")
        # The ``/tmp`` prefix is host layout and allowed, but a link *inside*
        # the root stays refused even though the root itself is spelled through
        # a symlinked OS prefix.
        (lexical_root / "link").symlink_to(real, target_is_directory=True)
        q = quarantiner(root=lexical_root)
        with pytest.raises(QuarantineError) as excinfo:
            q.open(lexical_root / "link" / "tool.py")
        assert excinfo.value.code is QuarantineCode.SYMLINK  # type: ignore[attr-defined]
    finally:
        shutil.rmtree(lexical_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Process-group teardown reaches descendants after the leader exits
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - own process group
        return True


def _wait_for_pidfile(path: Path, *, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return int(path.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.02)
    raise AssertionError("grandchild pid file was never written")


def _wait_for_death(pid: int, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.02)
    assert not _pid_alive(pid), f"descendant {pid} survived the process-group kill"


def _forking_source(pidfile: Path, *, leader_sleeps: bool) -> str:
    tail = "    time.sleep(30)\n" if leader_sleeps else "    pass\n"
    return (
        "import os, time\n"
        f"_pidfile = {str(pidfile)!r}\n"
        "_child = os.fork()\n"
        "if _child == 0:\n"
        "    with open(_pidfile, 'w') as handle:\n"
        "        handle.write(str(os.getpid()))\n"
        "    while True:\n"
        "        time.sleep(1)\n"
        "else:\n" + tail
    )


def test_timeout_kills_a_hung_grandchild(tmp_path: Path):
    pidfile = tmp_path / "grandchild.pid"
    probe = tmp_path / "forks.py"
    probe.write_text(_forking_source(pidfile, leader_sleeps=True), encoding="utf-8")
    q = quarantiner(root=tmp_path, timeout_s=0.8)

    outcome = q.run_isolated(q.open(probe))

    assert outcome.code is QuarantineCode.IMPORT_TIMEOUT
    _wait_for_death(_wait_for_pidfile(pidfile))


def test_leader_exit_still_reaps_a_hung_grandchild(tmp_path: Path):
    pidfile = tmp_path / "grandchild.pid"
    probe = tmp_path / "forks_exit.py"
    probe.write_text(_forking_source(pidfile, leader_sleeps=False), encoding="utf-8")
    q = quarantiner(root=tmp_path, timeout_s=5.0)

    # The leader exits immediately (the module declares no contract); only the
    # teardown of the whole process group can reap the still-running grandchild.
    outcome = q.run_isolated(q.open(probe))

    assert outcome.code is QuarantineCode.NO_CONTRACT
    _wait_for_death(_wait_for_pidfile(pidfile))
