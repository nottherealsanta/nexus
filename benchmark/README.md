# Agent benchmark

`bench.py` runs two small, real-model scenarios against the configured Nexus
provider. Run every command below from the repository root.

## Prerequisites

- Python 3.11 or newer and the Nexus project dependencies installed (for
  example, `pip install -e .` in the project environment).
- A repository or `~/.nexus/config.toml` Nexus config with a default model and a
  configured provider that can serve it. Supply provider credentials through
  environment variables or Nexus's credential store, not literal keys in TOML.
- Benchmark setup requires the selected provider config to use the sectioned
  `config_version = 2` format; setup gives an actionable error for legacy flat
  provider configs rather than generating an ambiguous workspace config.
- For a hosted provider, a working network connection and an account that can
  make model requests. A configured local provider such as Ollama can also be
  used.

Setup copies the selected Nexus config into each generated workspace's
`nexus.toml`, preserving provider/model settings while replacing local
permissions with a deny-by-default benchmark allowlist. Before running, the
effective layered policy is checked: permissive modes, inherited extra grants,
unbounded workspace write roots, and unattended prompts cause a fail-closed
refusal. Literal credential fields and credential-bearing provider environment
values are refused rather than copied; use
`${env:VARIABLE}` references or Nexus's credential store.

## Commands

List scenarios and the artifact root:

```sh
python3 benchmark/bench.py list
```

Create or recreate both workspaces, or just one:

```sh
python3 benchmark/bench.py setup
python3 benchmark/bench.py setup file-edit
python3 benchmark/bench.py setup shell-command
```

Run the file-edit scenario:

```sh
python3 benchmark/bench.py run file-edit
```

Run the shell-command scenario only when you explicitly opt in:

```sh
python3 benchmark/bench.py run shell-command --allow-shell
```

The optional `--timeout SECONDS` defaults to 300 seconds and is capped at 600.
Each run starts a fresh session, reuses its workspace, and removes the previous
output and run logs before invoking the agent. To restore a scenario's original seeded workspace
after edits, run `setup` for that scenario again.

Check the current output without invoking a model, for one scenario or both:

```sh
python3 benchmark/bench.py check
python3 benchmark/bench.py check file-edit
python3 benchmark/bench.py check shell-command
```

Reset one or both benchmark-owned scenario directories:

```sh
python3 benchmark/bench.py reset file-edit
python3 benchmark/bench.py reset shell-command
python3 benchmark/bench.py reset
```

Reset only removes the fixed, marked scenario directories under
`artifacts/benchmark`; it has no arbitrary-path option. Deletion is anchored to
open directory descriptors, refuses symlink/special-file trees, and checks path
identity while traversing. The marker is an accident guard, not proof against a
local actor who can forge it. `setup` recreates the selected scenario and
therefore also replaces its workspace and run artifacts.

## Scenarios and grading

- **`file-edit`** gives the agent `input.txt` containing `color=blue` and asks
  it to write `result.txt` with `color=green`. The grade passes only when the
  output file matches the expected bytes exactly.
- **`shell-command`** asks the agent to invoke exactly
  `echo shell-ok > result.txt; echo BENCHMARK_BASH_RAN`. The grade requires the
  exact expected `result.txt` (`shell-ok`) and JSONL evidence of exactly one
  requested Bash call, the exact command input, one start and successful
  non-error completion, no other requested/started tools, and the success marker
  with `exit_code: 0`.
Grading uses the raw in-memory event objects before log sanitization; `check`
reads the latest sanitized JSONL and is therefore a best-effort repeat grade.

The command reports the **Nexus process exit/timeout** separately from the
**output grade**. A passing grade alone does not make a run successful: the
benchmark command exits zero only when the process exits zero without timing
out and grading passes. Grading checks files and, for the shell scenario, tool
events; it does not rely on the model's final response text. `check` applies the
same output/evidence grade to the current workspace, but does not report or
recheck a prior process exit status.

## Artifacts and repeatability

Generated, ignored files are kept below `artifacts/benchmark/`:

```text
artifacts/benchmark/
  .benchmark-owned
  file-edit/
    .benchmark-owned
    workspace/                 # seeded input, copied nexus.toml, agent, output
    run/                        # latest run artifacts
  shell-command/
    .benchmark-owned
    workspace/                 # seeded instructions, copied nexus.toml, agent, output
    run/                        # latest run artifacts
```

Each scenario's `run/` contains `stdout.log`, `stderr.log`, `events.jsonl`, and
`result.json`. The logs and result describe only the latest run because a new
run replaces them. Logs are redacted best-effort: structured JSONL string values
are scrubbed without changing fields such as `exit_code`, but heuristic redaction
cannot guarantee detection of every secret. Logs remain local run artifacts and
should still be handled appropriately. Inspect
them from the repository root, for example:

```sh
less artifacts/benchmark/file-edit/run/result.json
less artifacts/benchmark/file-edit/run/stderr.log
less artifacts/benchmark/file-edit/run/events.jsonl
```

To compare repeated attempts, save or copy the run directory elsewhere before
starting the next run. Runs use a fresh session and clear the generated output
and logs, but they reuse the workspace; run `setup <scenario>` to restore its
seeded files and copied config. Results may vary between runs because the model
is live and can be nondeterministic. Live runs can consume provider tokens or
incur charges.

## Shell scenario security

`--allow-shell` is an explicit opt-in to the fixed Bash action. The benchmark
requires a deny-by-default effective policy and rejects inherited broad grants;
however, permission matching is not an OS sandbox. The approved command runs
with the user's OS privileges and may access host resources according to the
operating system. Do not run this scenario with an untrusted model or provider.

The file-edit scenario also invokes a live model and should be run only with a
provider/model you trust. Inspect generated workspace configs and run logs when
needed.
