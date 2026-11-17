"""The per-root `@` search index: same ranking as `_rank`, refreshed on invalidation and after the TTL."""
import asyncio
import random

import pytest

from nexus import files


@pytest.fixture(autouse=True)
def fresh_indexes():
    files._indexes.clear()
    yield
    files._indexes.clear()


def _tree(root, rng, count=300):
    words = ["main", "Main", "util", "src", "comp", "index", "a", "test_x"]
    for _ in range(count):
        parts = [rng.choice(words) + str(rng.randint(0, 3)) for _ in range(rng.randint(1, 4))]
        path = root.joinpath(*parts[:-1], parts[-1] + rng.choice([".py", ".ts"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() and not path.is_dir():
            path.write_text("x")


async def test_index_results_equal_the_rank_function_over_a_full_listing(tmp_path):
    rng = random.Random(7)
    _tree(tmp_path, rng)
    index = await files._index_for(tmp_path.resolve())
    for include_dirs in (False, True):
        listing = [p for p, _, _ in (index.everything if include_dirs else index.only_files)]
        for query in ("", "m", "ma", "main", "MAIN1", "src/ma", "comp2/", "index.ts", ".py", "zzz", "a/"):
            needle = query.casefold()
            expected = sorted(
                (p for p in listing if needle in p.rstrip("/").casefold()), key=lambda p: files._rank(p, needle)
            )
            for limit in (1, 7, 30, 100):
                got = await files.search_files(tmp_path, query, limit, include_directories=include_dirs)
                assert got == expected[:limit], (query, limit, include_dirs)


async def test_new_files_appear_after_invalidation_and_after_the_ttl(tmp_path, monkeypatch):
    (tmp_path / "one.txt").write_text("x")
    assert await files.search_files(tmp_path, "txt") == ["one.txt"]
    (tmp_path / "two.txt").write_text("x")
    assert await files.search_files(tmp_path, "txt") == ["one.txt"]  # inside the TTL: still the old index
    files.invalidate_index(tmp_path)
    assert await files.search_files(tmp_path, "txt") == ["one.txt", "two.txt"]
    (tmp_path / "three.txt").write_text("x")
    monkeypatch.setattr(files, "_GIT_LIST_TTL", 0.0)
    assert await files.search_files(tmp_path, "txt") == ["one.txt", "two.txt"]  # stale served, refresh started
    await asyncio.gather(*files._building.values())
    assert await files.search_files(tmp_path, "txt") == ["one.txt", "three.txt", "two.txt"]


async def test_concurrent_first_queries_share_one_build(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    calls = []
    original = files._walk_visible

    def counting(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(files, "_walk_visible", counting)
    results = await asyncio.gather(*(files.search_files(tmp_path, "a") for _ in range(8)))
    assert results == [["a.txt"]] * 8
    assert len(calls) == 1


async def test_mutations_invalidate_the_index(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert await files.search_files(tmp_path, "") == ["a.txt"]
    files.mutate_path(tmp_path, "create_file", "b.txt")
    assert await files.search_files(tmp_path, "") == ["a.txt", "b.txt"]


async def test_index_count_is_bounded(tmp_path):
    for number in range(files._MAX_INDEXES + 3):
        root = tmp_path / f"r{number}"
        root.mkdir()
        (root / "f.txt").write_text("x")
        assert await files.search_files(root, "f") == ["f.txt"]
        await asyncio.sleep(0)
    assert len(files._indexes) <= files._MAX_INDEXES
