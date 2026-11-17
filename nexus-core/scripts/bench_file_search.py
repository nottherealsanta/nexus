"""Benchmark `nexus.files.search_files` (the `@` file search) on synthetic and real repos.

    uv run python scripts/bench_file_search.py --dirs               # 10k + 100k synthetic, plus this repo
    uv run python scripts/bench_file_search.py --sizes 100000 --dirs --profile
    uv run python scripts/bench_file_search.py --sizes --repo ~/some/repo

Synthetic repos are built once under $NEXUS_BENCH_DIR (default: the system temp dir) and reused. Budget (plans/50,
Phase 3): warm p95 < 20 ms on 100k files; first query on a cold root < 1 s. `--dirs` matches the `@` picker, which
asks for directories too. "warm" is a sweep of keystroke-like queries after the first one has run.
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import os
import pstats
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nexus import files

QUERIES = ["s", "sr", "src", "src/", "src/ma", "src/mai", "main", "comp", "index.ts", "zzzz-no-match", "test_", "util"]
DIRS = ["src", "lib", "app", "tests", "docs", "packages", "components", "services", "models", "utils", "core", "api",
        "web", "cli", "plugins", "internal", "pkg", "cmd", "assets", "styles"]
WORDS = ["main", "index", "util", "helper", "component", "service", "model", "config", "types", "client", "server",
         "handler", "router", "store", "view", "page", "test", "spec", "mock", "format"]
EXTS = [".py", ".ts", ".tsx", ".rs", ".md", ".json", ".css", ".go"]


def build(base: Path, count: int) -> Path:
    """`count` tracked files at depth 2-7 plus count/5 ignored `node_modules` files, git-initialised."""
    root = base / f"repo-{count}"
    if (root / ".built").exists():
        return root
    rng = random.Random(count)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("node_modules/\ndist/\n*.log\n")
    made = 0
    dirs: list[Path] = []
    while made < count:
        if len(dirs) < count // 12 + 1:
            parts = [rng.choice(DIRS) + (str(rng.randint(0, 40)) if i else "") for i in range(rng.randint(2, 7))]
            directory = root.joinpath(*parts)
            directory.mkdir(parents=True, exist_ok=True)
            dirs.append(directory)
        directory = rng.choice(dirs)
        for _ in range(rng.randint(5, 25)):
            path = directory / f"{rng.choice(WORDS)}_{rng.choice(WORDS)}{rng.randint(0, 999)}{rng.choice(EXTS)}"
            if not path.exists():
                path.write_text("x")
                made += 1
    ignored = root / "node_modules" / "pkg"
    ignored.mkdir(parents=True)
    for i in range(count // 5):
        (ignored / f"dep{i}.js").write_text("x")
    (root / "dist").mkdir()
    (root / "dist" / "a.log").write_text("x")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    (root / ".built").write_text("")
    return root


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def drop_caches() -> None:
    for name in ("_git_cache", "_indexes"):
        cache = getattr(files, name, None)
        if isinstance(cache, dict):
            cache.clear()


async def measure(root: Path, rounds: int, include_dirs: bool) -> dict:
    drop_caches()
    started = time.perf_counter()
    await files.search_files(root, "s", 30, include_directories=include_dirs)
    cold = (time.perf_counter() - started) * 1000
    warm: list[float] = []
    for _ in range(rounds):
        for query in QUERIES:
            started = time.perf_counter()
            await files.search_files(root, query, 30, include_directories=include_dirs)
            warm.append((time.perf_counter() - started) * 1000)
    return {"cold": cold, "p50": statistics.median(warm), "p95": pct(warm, 0.95), "max": max(warm)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="*", default=[10_000, 100_000])
    parser.add_argument("--repo", type=Path, action="append", default=[])
    parser.add_argument("--no-self", action="store_true", help="skip this repo")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--profile", action="store_true", help="cProfile one warm sweep on the last repo")
    parser.add_argument("--dirs", action="store_true", help="include_directories=True (what the `@` picker uses)")
    args = parser.parse_args()
    base = Path(os.environ.get("NEXUS_BENCH_DIR") or tempfile.gettempdir()) / "nexus-bench-files"
    base.mkdir(parents=True, exist_ok=True)
    roots = [(f"synthetic {n:,}", build(base, n)) for n in args.sizes]
    if not args.no_self:
        roots.append(("this repo", Path(__file__).resolve().parents[2]))
    roots += [(r.name, r) for r in args.repo]
    print(f"{'repo':<18}{'cold ms':>10}{'warm p50':>10}{'warm p95':>10}{'warm max':>10}"
          f"   ({len(QUERIES)} queries x {args.rounds} rounds)")
    for label, root in roots:
        r = await measure(root, args.rounds, args.dirs)
        print(f"{label:<18}{r['cold']:>10.1f}{r['p50']:>10.1f}{r['p95']:>10.1f}{r['max']:>10.1f}")
    if args.profile:
        root = roots[-1][1]
        await files.search_files(root, "s", 30, include_directories=args.dirs)
        profiler = cProfile.Profile()
        profiler.enable()
        for query in QUERIES:
            await files.search_files(root, query, 30, include_directories=args.dirs)
        profiler.disable()
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(14)


if __name__ == "__main__":
    asyncio.run(main())
