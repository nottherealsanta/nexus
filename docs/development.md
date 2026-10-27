# Development

## Repo layout

```
PLAN.md           the plan, principles, ledger of changes from old Nexus
AGENTS.md         instructions for agents; points to docs/
docs/             this documentation
nexus-core/       Python (uv): everything that is not a UI
nexus-tui/        placeholder for the Rust Ratatui client
nexus-nsh/        placeholder for the NSH client
```

`nexus-tui/` and `nexus-nsh/` hold no code yet. Clients will speak only the
HTTP API ([api.md](api.md)).

Inside `nexus-core/` (a uv project: `pyproject.toml`, `uv.lock`, `.python-version`):

```
nexus/
  llmhub/            types, sse, responses, chat, providers, catalog, hub
  llmhub/auth/       store, device, codex, copilot, grok, opencode, errors
  tools/             base, read, bash, edit, write, ask, code
  observations.py    per-session read observations and the read-before-edit guard
  workspace.py       control tools: expose, session-directory moves, worktree specs
  worktrees.py       Git worktree list/create/delete through argument arrays
  questions.py       the ask contract: schema, bounds, validation, result text
  snapshot.py        serializable session configuration and instruction-file records
  agents.py          agent files: parse, load (builtin, global, project)
  builtin_agents/    orchestrate, build, quick, scout, plan (.md) + NOTICE
  loop.py            the agent loop
  codec.py           JSON encoding of messages and events
  paths.py           home(): $NEXUS10_HOME or ~/.nexus10
  export.py          read-only session/tree exports: Markdown and JSON
  store.py           SQLite: projects, sessions, messages, observations (~/.nexus10/nexus.db)
  bus.py             in-process event bus (seq-numbered, bounded per subscriber)
  kernels.py         daemon-owned persistent Monty kernels
  bash_jobs.py       daemon-owned background bash jobs
  runtime.py         projects, sessions, turns, approvals, questions, workspace controls, interrupts
  server.py          the HTTP API (Starlette) — docs/api.md
  daemon.py          serve, start, stop, discovery via daemon.json — docs/daemon.md
  inspector/         index.html: the session inspector page — docs/inspector.md
  client.py          HTTP client for the API, used by the CLI
  cli.py             local: login | logout | status | models | serve | daemon
                     daemon clients: project | session | workspace | run | chat
tests/               offline tests (test_server.py drives the API with a scripted provider)
```

State lives in `~/.nexus10` (`$NEXUS10_HOME`). Never touch `~/.nexus` (old Nexus).

## Commands

Run uv from `nexus-core/`. Use uv only: `uv add <pkg>`, never `pip install`.
Python is pinned in `.python-version` (>= 3.13).

```sh
cd nexus-core
uv sync                        # venv, deps and the dev group (pytest, pytest-asyncio, ruff)
uv run pytest -q               # the offline suite
uv run pytest -q tests/test_tools.py -k name   # one area
uv run ruff check nexus tests  # lint; line length 140
uv run nexus status            # which providers are connected
uv run nexus login codex       # or: login codex --device
uv run nexus models --refresh
uv run nexus chat --wire       # see every request and SSE event
uv run nexus run "prompt" --model grok/grok-4.7 --yes --workspace .
```

CLI flags for `chat` and `run`: `--model provider/model`, `--effort`,
`--session ID`, `-c/--continue`, `--workspace` (default `.`), `--wire`,
`--wire-file F.jsonl`, `--yes`, `--disable bash,write` (empty string clears switches). Model, effort and `--yes` (permission `allow`)
are saved on the session. After changing core code, run `nexus daemon stop` so the
next command starts a daemon with the new code. With
`--wire` the CLI prints the redacted request and every SSE event; `--wire-file`
appends the same wire events as JSONL. `/context` in `chat` prints the next
request body; `/exit` and `/quit` leave.

## Tests

`tests/` needs no network or credentials. `pytest-asyncio` runs in `auto` mode
(`asyncio_mode = "auto"`), so async tests need no marker.

| File | Covers |
| --- | --- |
| `test_responses.py` | Responses dialect: request body, replay rules, event parsing |
| `test_chat.py` | Chat dialect: request body, reasoning fields, event parsing |
| `test_catalog.py` | models.dev filtering and `ModelInfo` |
| `test_tools.py` | read, bash, edit (replacement and patch), write |
| `test_ask.py` | ask contract and validation, pending/reply API, cancellation, snapshots, nested code questions |
| `test_loop.py` | The loop with a scripted provider |

Loop tests use a small scripted provider that replays one reply per request.
`test_catalog.py` also covers `split_ref`. The table is from the test names and
file headers; the tests were not read in full.

## Style

* Module docstrings state the contract and cite the PLAN.md step.
* Types are frozen dataclasses with `slots`.
* Everything is bounded (sizes, counts, timeouts); clipping is announced.
* Errors never contain credentials.
* Layering: `llmhub` does not import `tools`, `loop` or `cli`. Adapters take header
  sources and never read credentials. Task tools never change Nexus state.

## Docs travel with code

A behavior or design change updates the matching page in `docs/` in the same
change. A step that lands or a decision that changes also updates PLAN.md (and
a ledger row for deliberate changes from old Nexus). Write the contract and the
reason, briefly, in the present tense; say "not verified" where it is not. A
new module gets a mention in the doc for its area and in the layout above. Index
new pages in [README.md](README.md).
