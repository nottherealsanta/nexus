"""O3: `~/.nexus` becomes Nexus 0.10's home only through a confirmed `nexus migrate`."""
import json
import os
import subprocess
import sys

import pytest

from nexus import migrate
from nexus.cli import main
from nexus.paths import MARKER, HomeNotMigrated, home


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake `$HOME` (also the conftest's), with no explicit Nexus home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEXUS10_HOME", raising=False)
    return tmp_path


def _old_nexus(machine):
    path = machine / ".nexus"
    path.mkdir()
    (path / "nexus.db").write_text("old nexus database")
    return path


def _prerelease(machine):
    path = machine / ".nexus10"
    path.mkdir(mode=0o700)
    (path / "auth.json").write_text('{"codex": {}}')
    (path / "nexus.db").write_text("0.10 database")
    return path


def test_a_fresh_machine_gets_a_marked_private_home(machine):
    assert home() == machine / ".nexus"
    assert (machine / ".nexus" / MARKER).is_file()
    assert (machine / ".nexus").stat().st_mode & 0o777 == 0o700
    assert home() == machine / ".nexus"


def test_old_nexus_is_never_used_before_migrating(machine):
    _old_nexus(machine)
    with pytest.raises(HomeNotMigrated, match="nexus migrate"):
        home()
    assert not (machine / ".nexus" / MARKER).exists()


def test_a_prerelease_home_is_not_silently_abandoned(machine):
    _prerelease(machine)
    with pytest.raises(HomeNotMigrated, match="pre-release"):
        home()
    assert not (machine / ".nexus").exists()


def test_a_path_inside_an_unmigrated_home_is_refused_too(machine, monkeypatch):
    _old_nexus(machine)
    monkeypatch.setenv("NEXUS10_HOME", str(machine / ".nexus" / "sub"))
    with pytest.raises(HomeNotMigrated):
        home()


def test_an_explicit_home_elsewhere_is_used_as_given(machine, monkeypatch):
    _old_nexus(machine)
    monkeypatch.setenv("NEXUS10_HOME", str(machine / "scratch"))
    assert home() == machine / "scratch"
    assert not (machine / "scratch").exists()


def test_migrating_moves_old_nexus_aside_and_the_prerelease_home_in(machine):
    old, pre = _old_nexus(machine), _prerelease(machine)
    asked = []

    plan = migrate.migrate(lambda p: asked.append(p) or True)

    assert asked == [plan] and plan.needed
    assert plan.aside_to is not None and plan.aside_to.name.startswith(".nexus.old-")
    assert (plan.aside_to / "nexus.db").read_text() == "old nexus database"
    assert (machine / ".nexus" / "nexus.db").read_text() == "0.10 database"
    assert (machine / ".nexus" / "auth.json").exists()
    assert not pre.exists()
    record = json.loads((machine / ".nexus" / MARKER).read_text())
    assert record["from"] == str(pre) and record["old_nexus"] == str(plan.aside_to)
    assert home() == old
    assert not (plan.aside_to / MARKER).exists()


def test_migrating_with_only_old_nexus_leaves_a_fresh_home(machine):
    _old_nexus(machine)
    plan = migrate.migrate(lambda p: True)
    assert plan.aside_to is not None and (plan.aside_to / "nexus.db").exists()
    assert sorted(p.name for p in (machine / ".nexus").iterdir()) == [MARKER]
    assert (machine / ".nexus").stat().st_mode & 0o777 == 0o700


def test_migrating_with_only_a_prerelease_home_moves_it_in(machine):
    _prerelease(machine)
    plan = migrate.migrate(lambda p: True)
    assert plan.aside_to is None
    assert (machine / ".nexus" / "nexus.db").read_text() == "0.10 database"


def test_declining_changes_nothing(machine):
    old, pre = _old_nexus(machine), _prerelease(machine)
    with pytest.raises(migrate.MigrationError, match="nothing was changed"):
        migrate.migrate(lambda p: False)
    assert (old / "nexus.db").exists() and (pre / "nexus.db").exists()
    assert not (old / MARKER).exists() and not (pre / MARKER).exists()
    assert sorted(p.name for p in machine.iterdir()) == [".nexus", ".nexus10"]


def test_a_migrated_home_needs_nothing_and_never_asks(machine):
    _prerelease(machine)
    migrate.migrate(lambda p: True)
    plan = migrate.migrate(lambda p: pytest.fail("asked twice"))
    assert not plan.needed
    assert sorted(p.name for p in machine.iterdir()) == [".nexus"]


def test_the_aside_name_never_overwrites_an_earlier_one(machine):
    _old_nexus(machine)
    first = migrate.plan(now=1_000_000)
    first.aside_to.mkdir()
    second = migrate.plan(now=1_000_000)
    assert second.aside_to != first.aside_to and second.aside_to.name.endswith("-2")


@pytest.mark.parametrize("where", [".nexus", ".nexus10"])
def test_a_running_daemon_blocks_migration(machine, where):
    _old_nexus(machine), _prerelease(machine)
    (machine / where / "daemon.json").write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(migrate.MigrationError, match="daemon"):
        migrate.migrate(lambda p: pytest.fail("asked while a daemon runs"))
    assert (machine / ".nexus10" / "nexus.db").exists()


def test_a_stale_daemon_file_does_not_block(machine):
    _prerelease(machine)
    stale = subprocess.Popen([sys.executable, "-c", "pass"])
    stale.wait()
    (machine / ".nexus10" / "daemon.json").write_text(json.dumps({"pid": stale.pid}))
    migrate.migrate(lambda p: True)
    assert (machine / ".nexus" / MARKER).exists()


def test_a_symlinked_prerelease_home_is_refused(machine):
    target = machine / "elsewhere"
    target.mkdir()
    (machine / ".nexus10").symlink_to(target)
    with pytest.raises(migrate.MigrationError, match="symlink"):
        migrate.migrate(lambda p: True)
    assert not (machine / ".nexus").exists()


def test_a_failed_move_puts_old_nexus_back(machine, monkeypatch):
    old, pre = _old_nexus(machine), _prerelease(machine)
    real = os.rename

    def rename(src, dst):
        if str(src) == str(pre):
            raise OSError("disk says no")
        real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(migrate.MigrationError, match="disk says no"):
        migrate.migrate(lambda p: True)
    assert (old / "nexus.db").read_text() == "old nexus database"
    assert (pre / "nexus.db").exists()


def test_the_command_asks_and_proceeds_with_yes(machine, capsys, monkeypatch):
    _old_nexus(machine), _prerelease(machine)
    assert main(["migrate"]) == 1  # no terminal, no --yes
    assert "--yes" in capsys.readouterr().err
    assert (machine / ".nexus10").exists()

    assert main(["migrate", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Old Nexus's data is kept at" in out
    assert main(["migrate"]) == 0
    assert "already set up" in capsys.readouterr().out


def test_other_commands_point_at_migrate(machine, capsys):
    _old_nexus(machine)
    assert main(["daemon", "status"]) == 1
    assert "nexus migrate" in capsys.readouterr().err
