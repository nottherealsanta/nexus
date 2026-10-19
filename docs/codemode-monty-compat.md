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

**Not verified:** Linux execution (only wheel listings were checked), cold-start
latency and RSS per call, and callback source positions.

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

Linux execution, cold-start latency and RSS, and callback source positions. (The
spike originally also listed `min_processes=0`, supported imports, cancellation and
resource-limit recovery; these are now exercised by the adapter tests on macOS.)

## Implemented isolation adapter verification

The installed 1.1.0 `AsyncMonty` API has no environment or cwd constructor
controls. Nexus therefore starts a Python helper using an explicit minimal env,
private temporary cwd and new process group; the helper owns the Rust pool.
Verified against the actual installed optional extra: arithmetic, asynchronous
external callbacks with keyword arguments, denied subprocess/os/open operations,
and whole-call deadlines. `CollectString(max_bytes=...)`, `type_check_stubs`,
`external_lookup`, `min_processes=0`, and the proposed resource-limit keys work.
