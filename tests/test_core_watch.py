from nexus.core.watch import Change, DirectoryWatcher


def test_first_poll_primes_and_reports_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    watcher = DirectoryWatcher([tmp_path], pattern="*.txt")
    assert watcher.poll() == []


def test_create_change_delete_detection(tmp_path):
    watcher = DirectoryWatcher([tmp_path], pattern="*.txt")
    watcher.prime()

    a = tmp_path / "a.txt"
    a.write_text("one")
    assert watcher.poll() == [Change("created", a.resolve())]

    a.write_text("one much longer")
    assert watcher.poll() == [Change("changed", a.resolve())]

    b = tmp_path / "b.txt"
    b.write_text("b")
    c = tmp_path / "c.txt"
    c.write_text("c")
    assert watcher.poll() == [
        Change("created", b.resolve()),
        Change("created", c.resolve()),
    ]

    a.unlink()
    assert watcher.poll() == [Change("deleted", a.resolve())]


def test_non_matching_files_are_ignored(tmp_path):
    watcher = DirectoryWatcher([tmp_path], pattern="*.py")
    watcher.prime()
    (tmp_path / "notes.txt").write_text("hello")
    assert watcher.poll() == []


def test_recursive_scan(tmp_path):
    nested = tmp_path / "pkg"
    nested.mkdir()
    watcher = DirectoryWatcher([tmp_path], pattern="*.py", recursive=True)
    watcher.prime()
    tool = nested / "tool.py"
    tool.write_text("x = 1")
    assert watcher.poll() == [Change("created", tool.resolve())]


def test_snapshot_is_a_copy(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    watcher = DirectoryWatcher([tmp_path], pattern="*.txt")
    watcher.prime()
    snapshot = watcher.snapshot()
    snapshot.clear()
    assert watcher.snapshot() != {}
