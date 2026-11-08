# Fork subagent: prompt-cache check (2026-11-08)

Question: does a fork's first request hit the cache the parent's request just wrote
(PLAN.md section 10, "Cache")? **Yes, on every provider tested.**

## Method

`run.py` runs one real turn per provider in-process (`make_runtime`), with a scratch
`$NEXUS10_HOME` and database. Only the credential store reads the real
`~/.nexus/auth.json` (with its file lock). The parent (`orchestrate`, effort `low`) is told to call
`session` once with `agent: "fork"`, `wait: true` and a one-word task, then reply `done`.
`read.py` prints the `usage` events of both sessions. One run per provider, small
context (about 3–5K tokens), so the numbers show that the prefix is shared, not
steady-state rates.

The fork inherits the parent's settled history (the task message; the parent's
unfinished `session` call is dropped), so the most the fork can reuse is about the
parent's **first** request: system prompt, tools and the user message.

## Results

| Provider / model | Parent request 1 input | Fork request 1 input | Fork cached | Cached / reusable prefix |
| --- | ---: | ---: | ---: | ---: |
| `codex/gpt-6-luna` | 3,196 | 3,288 | 3,072 | 96% |
| `copilot/gpt-6-luna` | 3,194 | 3,286 | 3,191 | ~100% |
| `opencode/deepseek-v4.1-flash` | 4,327 | 4,417 | 4,224 | 98% |
| `grok/grok-4.7` (`edit` disabled, see below) | 5,039 | 5,127 | 4,992 | 99% |

The gap below 100% is most likely each provider caching in fixed-size blocks (3,072 and 4,224 are
multiples of 128); not checked further. Without the
shared prefix, the first request of a new session reads 0 cached tokens on the
same providers (parent request 1 in every row except Grok, which cached 1,152 tokens of
a shared system prefix from an earlier request).

The Claude provider (Agent SDK) is left out of this check at the user's request.

## Findings outside the fork

- **Grok rejects the `edit` tool schema:** `HTTP 400 invalid_client_tool_schema: edit:
  tool parameter root must be an object type (root schema is an anyOf/oneOf…)`. Every
  Grok session with `edit` enabled fails on its first request, fork or not. The Grok row
  above was run with `edit` disabled (both sessions; the fork copies the switch). Tracked in
  PLAN.md section 10.
- `codex/gpt-5.3-codex-spark` and `codex/gpt-5.4-mini` return `model_not_found` with a
  ChatGPT-account Codex sign-in, though `nexus models` lists them for Codex.

## Re-running

From `nexus-core/`: `uv run python ../reports/fork-cache/run.py <provider/model> low > out.json`,
then `uv run python ../reports/fork-cache/read.py out.json`. Set `DISABLE=edit` for Grok until
the schema is fixed. Each run makes three small paid requests.
