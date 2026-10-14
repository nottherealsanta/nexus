# Provider and model stream onboarding

Use this checklist when adding a provider, enabling a new model, or changing its
wire API. [adding-a-provider.md](adding-a-provider.md) is the end-to-end guide
(route, files, tests, docs); this page is its stream inventory. Read
[models.md](models.md) and [loop.md](loop.md) first. Provider adapters normalize output into
`nexus/model/stream.py`; the loop records durable events and both clients consume
the reduced view. Keep provider-specific parsing in the adapter. A model name is
not a reliable substitute for inspecting the endpoint's actual stream.

## Inspect the request and the complete stream

1. Confirm the provider's model catalogue, capabilities, authentication route,
   endpoint, and supported reasoning controls. Exercise the configured runtime
   route, including endpoint fallback, rather than only a hand-built HTTP request.
2. Use an existing connected account through the auth manager. Keep credentials,
   headers, encrypted reasoning, user prompts, and private output out of logs and
   fixtures. Record only safe endpoint names, request controls, event type names,
   counts, ordering, and synthetic or reviewed summary excerpts. Bound live probes
   with a timeout and use a small deterministic prompt.
3. Inspect every event type and relevant field across text, reasoning, tool calls,
   usage, completion, refusal, and errors. Check default reasoning settings and an
   explicit effort/budget; some endpoints require summaries to be requested.
   Check multiple reasoning runs, mixed thought/text chunks, and responses without
   any thoughts. Do not assume the advertised model catalogue proves inference.
4. Write an inventory mapping each observed wire event/field to the shared contract,
   or explaining why it is transport-only, unsupported, or opaque replay data.
   Account for newly observed events before declaring onboarding complete. Record
   provider/model, endpoint, date, settings, tests, and remaining limitations.

## Normalize semantics once

| Provider output | Shared representation | Required behavior |
| --- | --- | --- |
| Message identity | `MessageStart` | Routing metadata; no assistant content block. |
| Reply text | `TextDelta` | Preserve order and exact text. |
| Public thought/summary text | `ThinkingDelta` | Preserve all text in the timeline; context shows a bounded live phrase. |
| Thought boundary or replay signature | `ThinkingEnd` | Finalize immediately; preserve opaque signatures for supported replay, never display them. |
| Tool name, streamed arguments, final input | `ToolCallStart`, `ToolCallDelta`, `ToolCallEnd` | Preserve IDs, interleaving, argument assembly, and final parsed input. |
| Token accounting | `Usage` | Map input/output/cache/reasoning fields correctly; document cumulative vs incremental counts. |
| Completion/refusal/truncation | `MessageStop` | Map the stop reason; close outstanding content and calls. |
| Provider failure | Typed provider error | Preserve retry/degradation semantics; redact before crossing the host boundary. |
| Unnormalized diagnostic payload | `Raw` | Opt-in adapter passthrough only; currently ignored by the loop and not durable or user-visible. |

This table is extensible, not a promise that every arbitrary wire field is already
stored. For new user-visible semantics (for example citations, artifacts, explicit
activity phases, or provider-supplied duration), extend the shared stream/message
contract, durable event schema, reducer, and both clients together. Add replay and
adapter tests. Do not solve it with model-name checks, client-only state, or blanket
persistence of raw payloads. Transport heartbeats and opaque/private reasoning do
not become displayed summaries. If the endpoint exposes no public thoughts, record
that limitation rather than fabricating a phrase or a provider duration.

## Thinking and context projection

Responses requests for thinking-capable models use `reasoning.summary = "auto"`.
Gemini uses `includeThoughts` at the default budget; an explicit zero budget disables
it. Other adapters map the thoughts they receive into the same events. Mixed chunks
emit thinking before reply text. The loop records `thinking.end` for explicit
boundaries and for unsigned thought streams transitioning to text, tools, or stop.
Separate runs/signatures remain separate; the legacy final aggregate must not
repeat already finalized text during replay.

`ui_support/context.py:thinking_status` and the web projection use the trailing,
unfinalized thought block of the active turn. They show its last complete bold
heading, or normalized summary text, bounded to 120 characters. The full public
summary remains in the timeline. The context meter and details activity clear at
the thought boundary, before the response finishes. Completed turns cannot leak an
old activity phrase into the next turn. Reconnect must reconstruct the same state
from the durable log. No thinking duration is synthesized.

## Verification before shipping

- Add sanitized wire fixtures and adapter request/parser tests for every newly
  observed semantic event, including fragmented and mixed chunks, signatures,
  unsigned thoughts, tool interleaving, usage, and exceptional termination.
- Run provider conformance and `tests/test_thinking_context.py`. Its shared
  provider-to-loop-to-reducer checks include unsigned Ollama and OpenCode ACP
  streams as well as signed adapters. Test distinct signatures and signature-only
  replay carriers without duplicate content.
- Run `tests/test_core_loop.py` for durable boundaries and
  `tests/test_ratatui_projection.py` for the real native context/details projection.
  Run `tests/playwright_web_check.py` for live updates, reconnect, clearing before
  completion, safe text rendering, and narrow-screen layout.
- Keep live checks gated and outside the ordinary offline suite. Use the configured
  provider and assert actual inference, event ordering, and termination. Verify
  browser/TUI imports use the changed checkout when running standalone scripts.
- Run the full offline suite and lint before release. Follow [release.md](release.md)
  for the version bump, merge, and publication verification.

## Baseline observations: 2026-09-30

| Route | Observed behavior | Verification |
| --- | --- | --- |
| Codex GPT-6 Luna and GPT-5.6 Luna | Public reasoning headings with summary requested; no duration observed. | Live connected-account probes. |
| Codex GPT-6 Sol | Public reasoning headings, thought boundary, reply, usage, stop. | Live connected-account probe. |
| Copilot `gpt6-luna` | Chat endpoint rejects the model; configured fallback uses Responses. Public summaries, distinct boundaries, reply, usage, stop. | Live catalogue and inference; gated regression below. |
| Anthropic, Gemini, compatible chat, Ollama, OpenCode ACP | Shared thought normalization and context lifecycle. | Offline adapter/conformance tests; no other connected accounts were available. |
| Claude Agent SDK bridge | Buffers final text; currently exposes no live thoughts. | Explicit implementation limitation. |
| xAI Grok over SuperGrok (2026-10-14) | Live: device sign-in, grok-4.7 text, usage (with cached input) and a normal stop over `/responses`. Reasoning summaries were not asserted live; the event shapes follow OpenCode's xAI tests. | `tests/test_xai_live.py` (2 passed) and a `nexus chat` turn; offline adapter, conformance, device-OAuth and routing tests. |

### External CLI workers (Antigravity)

Antigravity (`agy`, [agents.md](agents.md#external-antigravity-workers)) is not a
streaming provider, so this inventory does not apply: it emits no `TextDelta`,
`Usage` or `ToolCall*` events into the shared contract. Its own NDJSON events
(`init`, `step_update`, `result`) become `external.*` events instead, and tool calls
are never `tool.*` events. Onboarding it means checking the CLI, not a wire stream:

| Check | Evidence |
| --- | --- |
| `agy --version`, `--help`, `agy models` | Observed locally on agy 1.3.4; model IDs are discovered at runtime, never stored as a catalogue |
| Headless event shapes (`init`, `step_update`, `result`; stdin user event; `--print-timeout` as a duration) | Documented at `antigravity.google/docs/cli/headless`, fetched by the parent session |
| Quota usage (`agy -p /usage`) | Built-in TSV verified locally on agy 1.3.4. Nexus parses strict four-column rows, inverts remaining percentages to used percentages, and reports unexpected rows as notes only |
| A real model turn, cached login, permission and trust wording | **Not run**: no credentials, offline fake-agy tests only; quota command verified without a model turn. Needs the user's explicit approval |

Repeat the Copilot regression with an existing connected account:

```sh
NEXUS_COPILOT_THINKING_LIVE=1 .venv/bin/python -m pytest -q -m live tests/test_copilot_thinking_live.py
```

The test checks Responses fallback, public thinking, its boundary, reply, and normal
termination. Device sign-in itself was not exercised. These observations are a
baseline; repeat the inventory when onboarding another model or when a provider
changes its stream format.

### Antigravity visibility and safe discovery

An installed `agy` is discovered without a handwritten provider section using a
bounded `agy models` probe only. Configured `agy` aliases and custom executables
are respected. The host model list exposes these rows labelled **Subagents only**
(including native clients); selecting one as the main model is refused with an
explanation, while explicit subagent tier/role/Task references remain supported.
Discovery does not acknowledge inherited settings: worker execution still requires
`worker_inherits_agy_settings = true` in an explicit provider section.

Providers Settings always includes Antigravity, with manual CLI sign-in: run `agy`
in your terminal, then **Refresh models** (re-runs `agy models`; the page shows the count and last-refreshed time). CLI presence is not verified
authentication. Nexus does not run a login subcommand, manage CLI credentials, or
perform a model turn to discover availability. A missing CLI is reported honestly.
