# Code mode: Monty compatibility gate

Status: **CM1 resolved by an isolated helper design; code mode is implemented
(offline-tested) and off by default.** The "blocked" analysis below is kept as the
reason for the design.

## Resolution

Monty 1.1.0 `AsyncMonty` has no environment or cwd controls, so Nexus does not use a
daemon-owned pool. Each `code` call starts a Python helper
(`nexus/tools/codemode/monty_adapter.py`, the only importer of `pydantic_monty`) with
an explicit minimal env, a private temporary cwd and a new process group. The helper
owns a one-process Monty pool; `killpg` ends everything on finish, cancel or
deadline. Rationale: [decisions.md](decisions.md). Verified on macOS arm64 against the
installed extra: arithmetic, async host callbacks with keyword arguments, denied
`subprocess`/`os`/`open`, whole-call deadlines, `CollectString(max_bytes=...)`,
`type_check_stubs`, `external_lookup`, `min_processes=0`, the resource-limit keys,
the importable module list (see [tools.md](tools.md#code-mode-the-code-tool)), and
that `class` inheritance and `yield`/`match` are unsupported.

**Not verified:** Linux execution (only wheel listings were checked), full adapter
cold-start latency/RSS per call, and callback source positions. The Phase 0 spike
below measures only a minimal helper plus one live worker.

The Plan 03 compatibility spike was repeated with Python 3.13 on macOS
arm64 using `pydantic-monty==1.1.0` in a disposable environment, outside the
project dependency environment. The complete Plan 03 document was read before
the spike. No runtime or project dependencies were changed.

## Confirmed

- The pinned package installs successfully from PyPI.
- The PyPI `pydantic-monty-client` 1.1.0 release lists Linux wheels, including
  CPython 3.13 manylinux x86_64/aarch64 and musllinux x86_64/aarch64. This is
  packaging evidence, not a Linux execution test.
- `AsyncMonty` owns a subprocess pool. Its constructor accepts `binary_path`,
  `min_processes`, `max_processes`, `checkout_timeout`, `request_timeout`,
  `max_checkouts_per_worker`, and the two duration-limit grace settings.
- Its public constructor has **no worker environment or working-directory
  parameter**. Actual calls with `env={}` and `cwd='/tmp'` each raise
  `TypeError: AsyncMonty.__new__() got an unexpected keyword argument ...`.
- The installed `_monty.pyi` documents binary discovery through `MONTY_BIN`,
  the environment scripts directory, and `PATH`. It provides no public worker
  spawn hook for supplying an environment or working directory.

## Original blocking requirement (resolved above)

Plan 03 CM1 explicitly requires stopping and revisiting the design if the
worker environment cannot be scrubbed. The documented pool API cannot satisfy
that requirement directly. Changing the daemon's process-wide `os.environ` or
working directory around pool startup is not a safe workaround: asynchronous
worker spawning/replacement and unrelated daemon operations would race with
those changes, and replacement workers also need the same guarantees.

A separate helper process launched with an explicit credential-free environment
and private empty working directory could own the Monty pool, or a carefully
specified executable launcher could enforce those properties for every worker
spawn. Neither fallback is yet selected or verified. A helper requires an IPC,
cancellation, shutdown, and host-callback protocol; a launcher requires verified
cross-platform executable discovery and lifecycle behavior. Neither should be
silently treated as the planned in-daemon pool.

**Resolved:** the helper design above was selected. The original text asked to select and authorize the isolated helper/launcher design,
or supply a supported Monty API/version with per-worker environment and working
 directory controls. Do not relax credential scrubbing or mutate daemon-global
environment/cwd to proceed.

## Still unverified

Linux execution, full adapter cold-start latency/RSS, and callback source positions.
(The original spike also listed `min_processes=0`, supported imports, cancellation
and resource-limit recovery; these are now exercised by the adapter tests on
macOS.) Cross-patch snapshot compatibility remains unverified; see Phase 0 below.

## Kernel plan Phase 0: live-session spike (2026-10-24)

Scope: §2 and Phase 0 of `plans/planning/code-mode-kernel.md`, **no product
changes**. Reproducer: ignored `artifacts/monty_kernel_phase0.py`. All probes
were local, with no network calls, mounts, real credential values printed, or
package downloads. The helper runs in a temporary empty working directory with
`-E -s`; snapshots are written only to a temporary directory and removed.

### Commands and environment

```sh
python artifacts/monty_kernel_phase0.py
python -m pytest -q tests/test_codemode*.py
python -m pytest -q tests/test_docs.py
git diff --check
git check-ignore artifacts/monty_kernel_phase0.py
```

The final run used installed `pydantic-monty==1.1.0`, CPython 3.14.3, macOS
arm64; `sys.executable` was `/Users/santa/repos/nexus/.venv/bin/python`.
The spike exited 0; the existing code-mode suite reported **116 passed in
14.73s**. Documentation checks reported **36 passed in 0.17s**;
`git diff --check` was clean. `git check-ignore` confirmed the script is ignored. An initial
`.venv/bin/python` attempt failed because that executable was unavailable at
that point; subsequent `python` invocations resolved to the environment above.
An existing alternate `/tmp/nexus-monty-spike/bin/python` was also inspected via
`importlib.metadata.version('pydantic-monty')`: it had **no installed Monty
distribution**, so it could not supply a second patch version. No packages were
installed or changed for this spike.

### Findings

1. **Worker environment:** the default helper was launched with its inherited
   environment plus the synthetic `NEXUS_PHASE0_FAKE_SECRET=phase0-local-marker`.
   `/bin/ps eww -p PID -o command=` could see that marker in the helper (positive
   control), but not in its direct `monty` child. No environment keys were
   visible at all for that child. A second helper launched with only
   `LANG=C.UTF-8` and `LC_ALL=C.UTF-8` likewise started a working Monty worker;
   the helper contained those keys plus macOS-added `__CF_USER_TEXT_ENCODING`,
   while the worker again showed no environment keys, including no locale keys.
   Thus the observed worker environment is empty, **not a copy of the helper's
   environment**, on this build/platform. Scrubbing the helper is compatible
   with worker startup and keeps the synthetic secret out of both processes.
   This is an OS observation with a positive control, not a portable guarantee
   about future Monty builds; retain helper scrubbing as defense in depth.
2. **Failed cells are not transactional.** Initialize `before = 1`,
   `items = [1]`, `stable = 7`; then run
   `before = 2; created = 3; items.append(2); 1 / 0`. The latter raises
   `MontyRuntimeError: ZeroDivisionError: division by zero`. The next cell sees
   `(before, created, items, stable) == (2, 3, [1, 2], 7)`: rebinding, a newly
   created global, and mutation all persist. The session remains usable.
3. **Namespace inspection:** top-level `locals()` returned
   `{'before': 2, 'items': [1, 2], 'stable': 7, 'created': 3}` across cells.
   `globals()`, `dir()`, `dir(before)`, and `vars()` each raised a `NameError`
   wrapped in `MontyRuntimeError`. A `whos()` implementation can use a top-level
   `locals()` probe; it must not assume CPython's `globals()` or `dir()` exists.
   This does not establish that `locals()` inside a helper function exposes the
   session namespace, nor that copying arbitrary values out is bounded/safe.
4. **Checkout options:** `pool.checkout(limits=..., type_check=True,
   type_check_stubs=...)` is supported. A limits dictionary containing
   `max_feed_duration_secs=0.1`, `max_turn_duration_secs=0.1`,
   `max_memory=67108864`, `max_recursion_depth=100`, `max_suspensions=10`,
   `max_total_sleep_secs=0`, and `gc_interval=1000` was accepted.
   `sum(range(10))` returned 45; `while True: pass` raised a feed-time
   `TimeoutError` at about 100.7 ms; the next cell returned 42. A separate
   checkout with `max_memory=1048576` rejected `[0] * 1000000` with
   `MemoryError` (16042722 bytes > 1048576); its next cell also returned 42.
   These are VM limits, not a claim about total worker RSS or exhaustive
   enforcement of every accepted key. With stub
   `def twice(x: int) -> int: ...` and a host callback, `twice(4)` returned 8;
   `twice("bad")` raised `MontyTypingError` citing the stub's expected `int`.
5. **One live worker cost:** `AsyncMonty(min_processes=1, max_processes=1)`
   created one direct `monty` child. In the final run, `ps -axo
   pid=,ppid=,rss=,comm=` reported **8832 KiB** RSS for the inherited-mode
   worker and **8864 KiB** for the scrubbed-mode worker after `1 + 1`.
   Each Python helper was **32832 KiB** RSS: roughly **41 MiB combined RSS**
   for this minimal helper plus one worker, before user data. RSS sums can
   double-count shared pages; this is not peak RSS or a sizing guarantee for
   the product adapter. Timed pool creation, checkout and first expression
   took **30.7 ms** and **5.2 ms**, respectively, in already-running helpers;
   Python launch/import and full Nexus adapter overhead are excluded.
6. **Snapshot compatibility:** `dump()` returned **1911 bytes**. It preserved
   the failed-cell state above, `saved = {"numbers": [1, 2, 3]}`, and
   `def add_saved(x): return x + stable`. Loading the same trusted bytes into
   (a) a new checkout in the same pool, (b) a new pool after the original pool
   closed, and (c) a separate Python helper with its own pool all produced
   `(saved, add_saved(5), before, created, items) ==
   ({'numbers': [1, 2, 3]}, 12, 2, 3, [1, 2])`.
   **Across patch versions: not verified.** Only 1.1.0 was available; no
   cross-version safety or forward/backward compatibility is inferred.
   Snapshots remain trusted, unauthenticated, version-sensitive bytes.

### Phase gate and plan recommendations

Neither explicit stop condition fired: a scrubbed helper successfully launches
its worker, and snapshots survive a fresh pool and a fresh helper process.
Do not implement subsequent phases as part of this spike. Before implementation,
make the plan explicit that failed cells retain partial writes, use a top-level
`locals()` approach rather than `globals()` for namespace inspection, and treat
snapshot format compatibility as exact-version-only until a separate offline
cross-version probe proves otherwise. Worker RSS above is a baseline, not a
validated multi-kernel capacity budget. Linux behavior and cross-patch dump
compatibility remain open; no plan or product files were changed here.

## Implemented isolation adapter verification

The installed 1.1.0 `AsyncMonty` API has no environment or cwd constructor
controls. Nexus therefore starts a Python helper using an explicit minimal env,
private temporary cwd and new process group; the helper owns the Rust pool.
Verified against the actual installed optional extra: arithmetic, asynchronous
external callbacks with keyword arguments, denied subprocess/os/open operations,
and whole-call deadlines. `CollectString(max_bytes=...)`, `type_check_stubs`,
`external_lookup`, `min_processes=0`, and the proposed resource-limit keys work.
