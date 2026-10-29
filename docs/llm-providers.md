# LLM providers

`nexus/llmhub/` turns four live providers and an optional dev mock into one conversation shape and one stream
shape ([stream-events.md](stream-events.md)). There is one module per wire dialect
and a table that says which provider speaks which, with what quirks.

| Module | Role |
| --- | --- |
| `llmhub/__init__.py` | The `Provider` protocol: `name`, `body(Request)`, `stream(Request)` |
| `llmhub/types.py` | Conversation parts, `Request`, stream events |
| `llmhub/providers.py` | `PROVIDERS` table and `make(provider_id, auth, model=None)` |
| `mock/` | Dev-only scripted provider and scenarios (`NEXUS10_DEV=1`; [scenarios](#offline-mock-provider)) |
| `llmhub/responses.py` | OpenAI Responses dialect (codex, grok) and `ResponsesQuirks` |
| `llmhub/chat.py` | OpenAI Chat Completions dialect (copilot, opencode) and `ChatQuirks` |
| `llmhub/sse.py` | Server-sent events parser |
| `llmhub/hub.py` | Joins sign-ins to the table; `provider/model` refs ([models.md](models.md)) |

Adapters never read stored credentials. They take a header source
(`async headers() -> Mapping[str, str]`) from `llmhub/auth` ([auth.md](auth.md)).

## The provider table

`PROVIDERS` in `providers.py` (the `mock` row is added only when `NEXUS10_DEV` is truthy):

| id | Label | Dialect | Endpoint (`base_url`) | Request URL | models.dev id | Default model |
| --- | --- | --- | --- | --- | --- | --- |
| `mock` | Offline mock | scripted | — | `mock://<scenario>` | built in | `hello` (dev only) |
| `codex` | ChatGPT (Codex) | responses | `https://chatgpt.com/backend-api/codex` | `{base}/responses` | `openai` | `gpt-6.1-terra` |
| `grok` | xAI (SuperGrok) | responses | `https://cli-chat-proxy.grok.com/v1` | `{base}/responses` | `xai` | `grok-4.7` |
| `copilot` | GitHub Copilot | chat | `https://api.githubcopilot.com` | `{base}/chat/completions` | `github-copilot` | `claude-sonnet-5` |
| `opencode` | OpenCode Go | chat | `https://opencode.ai/zen/go/v1` | `{base}/chat/completions` | `opencode-go` | `kimi-k3` |

`ProviderInfo` also has `verified` (a live multi-step tool turn has run through
the adapter). It is `True` for all four. Sign-in itself is not verified for
copilot, grok and opencode (PLAN.md, step 3).

### Quirks per provider

| id | Quirks |
| --- | --- |
| `codex` | `encrypted_reasoning=True`, `replay_raw=True`, `prompt_cache_key=True`, `session_header="session_id"` |
| `grok` | `session_header="x-grok-conv-id"` (everything else off) |
| `copilot` | `initiator_header=True` |
| `opencode` | `session_header="x-opencode-session"` |
| `mock` | scripted events; no auth or network (dev only) |

### What each quirk field does

`ResponsesQuirks` (`responses.py`):

| Field | Effect |
| --- | --- |
| `encrypted_reasoning` | Adds `"include": ["reasoning.encrypted_content"]` to the body, and allows reasoning items to be replayed (see below) |
| `replay_raw` | Own-origin text and function-call items are sent back verbatim (`raw`) instead of rebuilt |
| `prompt_cache_key` | Sends `prompt_cache_key` = conversation id, cut to 64 characters, when the request has one |
| `session_header` | Name of a header that carries the conversation id (cut to 64 characters) when the request has one |

`ChatQuirks` (`chat.py`):

| Field | Effect |
| --- | --- |
| `session_header` | Same as above |
| `initiator_header` | Sets `x-initiator`: `user` when the last message is from the user, otherwise `agent` (an empty history counts as `user`) |

## Request bodies

Both dialects always stream. Examples below come from `provider.body(request)` for
a conversation with one user message, one assistant turn (reasoning plus a `read`
tool call) and the tool result, `reasoning_effort="medium"`, conversation id
`conv-123`. Tool schemas are shortened.

### Responses (codex)

```json
{
  "model": "gpt-6.1-terra",
  "instructions": "Be brief.",
  "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What is in a.txt?"}]},
    {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Need to read"}], "encrypted_content": "gAAA..."},
    {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": "{\"path\":\"a.txt\"}"},
    {"type": "function_call_output", "call_id": "call_1", "output": "     1\thello"}
  ],
  "tools": [{"type": "function", "name": "read", "description": "Read a file", "parameters": {"...": "..."}, "strict": false}],
  "tool_choice": "auto",
  "parallel_tool_calls": false,
  "reasoning": {"summary": "auto", "effort": "medium"},
  "store": false,
  "stream": true,
  "include": ["reasoning.encrypted_content"],
  "prompt_cache_key": "conv-123"
}
```

Fixed fields: `tool_choice: "auto"`, `parallel_tool_calls: false`, `store: false`,
`stream: true`, `strict: false` on every tool, `reasoning.summary: "auto"`.
`reasoning.effort` is present only when the request has an effort. `store` is
off, so the whole history is resent each request.

`instructions` (Responses) and the first `system` message (Chat) carry the
session's frozen starting context: the agent prompt with only its available
tools, the environment block, and the labelled `# Instructions file` section
read from the session directory. Later turns and daemon restarts replay it
verbatim; file edits and date changes do not rewrite it
([agents.md](agents.md#session-snapshots)).

### Responses (grok)

The same body without `include` and `prompt_cache_key`. Replay is rebuilt, and
the reasoning item is gone:

```json
{
  "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What is in a.txt?"}]},
    {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{\"path\":\"a.txt\"}"},
    {"type": "function_call_output", "call_id": "call_1", "output": "     1\thello"}
  ]
}
```

(`model`, `instructions`, `tools`, `tool_choice`, `parallel_tool_calls`,
`reasoning`, `store` and `stream` are as above.) A rebuilt assistant text is
`{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ...}]}`.

### Chat Completions (copilot, opencode)

```json
{
  "model": "claude-sonnet-5",
  "messages": [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "What is in a.txt?"},
    {"role": "assistant", "content": null,
     "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\"path\":\"a.txt\"}"}}],
     "reasoning_content": "Need to read"},
    {"role": "tool", "tool_call_id": "call_1", "content": "     1\thello"}
  ],
  "stream": true,
  "stream_options": {"include_usage": true},
  "tools": [{"type": "function", "function": {"name": "read", "description": "Read a file", "parameters": {"...": "..."}}}],
  "tool_choice": "auto",
  "parallel_tool_calls": false,
  "reasoning_effort": "medium"
}
```

`tools`, `tool_choice` and `parallel_tool_calls` are present only when the
request has tools. `reasoning_effort` only when it has an effort. The system
message is omitted when `instructions` is empty. An assistant message with no
text has `content: null`; a tool call with empty arguments is sent as `"{}"`.
Each `ToolResult` becomes its own `tool` message.

## User images

`Image(data_url, name)` is a user part holding a base64 data URL
([stream-events.md](stream-events.md#conversation)).
The daemon validates attachments before it creates the turn
([api.md](api.md#image-prompts)) and precedes each new image with a text
part `[Image A]`, `[Image B]`, and so on in attachment order. Both dialects
send each label immediately before its image, so a reference in the user's text
maps to the right image, and the labels survive replay.

- **Responses:** an image becomes `{"type": "input_image", "image_url": <data
  URL>}`, appended to the content of the user `message` item just built from the
  same message's preceding text (otherwise a new user `message` item).
- **Chat:** a user message with any image sends `content` as an array of
  `{"type": "text", "text": …}` and `{"type": "image_url", "image_url": {"url":
  <data URL>}}` items in part order. A text-only user message keeps its plain
  string `content`.

Stored images replay in both dialects whichever provider the session switches
to. Whether the model understands them depends on the selected model; Nexus
does not check.

## Replay rules

Every part a provider produced keeps its exact wire item (`raw`) and the provider
that made it (`origin`). On the next request a part is "own" when its `origin`
equals the provider being called and `raw` is set.

### Responses dialect (`input_items`)

| Part | Own, `replay_raw` on | Otherwise |
| --- | --- | --- |
| `Text` | `raw` verbatim | Rebuilt: `output_text` message (assistant) or `input_text` message (user) |
| `Reasoning` | `raw` verbatim only if `encrypted_reasoning` is on **and** `raw.encrypted_content` is truthy | Dropped |
| `ToolCall` | `raw` verbatim | Rebuilt `function_call` with `call_id`, `name`, `arguments` |
| `ToolResult` | n/a | `function_call_output` with `call_id`, `output` |
| `Image` | n/a | `input_image` added to the user message item ([User images](#user-images)) |

So codex replays its own output items exactly, including encrypted reasoning.
grok rebuilds text and calls and never sends reasoning back (store is off and
there is no encrypted content). Reasoning from any other provider is dropped.
`ToolResult.is_error` is not sent; the error is only in the output text.

### Chat dialect (`chat_messages`)

* Text parts of a message are joined into one string. Foreign text is kept (it
  is plain text). A user message with images is the exception: a content array
  ([User images](#user-images)).
* Tool calls are rebuilt from `id`, `name`, `arguments`; `raw` is not used.
* Reasoning is replayed only when the part's `origin` is this provider and
  `raw["field"]` is one of `reasoning_content`, `reasoning`, `reasoning_text`.
  It goes on the assistant message under that field name, holding the part's
  `summary`. Kimi- and DeepSeek-style thinking models need it next to tool calls.
  Foreign reasoning is dropped.
* The parser records the field name the host used (the first one seen in the
  stream) in `Reasoning.raw = {"field": ...}`. If the host sent no reasoning, no
  Reasoning part exists.

## Streaming

Both providers yield, in order: `WireRequest`, `WireResponse`, then a
`WireEvent` for every SSE event followed by the normalized events it produces.
Headers go out as: `content-type`, `accept: text/event-stream`, the session
header (if set and the request has a conversation id), `x-initiator` (copilot),
then the auth headers (which override earlier keys).

### Annotated stream: Responses (codex, one reasoning item, one tool call)

| SSE event | Normalized events |
| --- | --- |
| (before the request) | `WireRequest`; then `WireResponse(200, ...)` |
| `response.created` | `WireEvent` only |
| `response.output_item.added` (reasoning) | `WireEvent`; resets the summary counter |
| `response.reasoning_summary_part.added` | `WireEvent`; from the second summary part on, also `ReasoningDelta("\n\n")` |
| `response.reasoning_summary_text.delta` | `WireEvent`, `ReasoningDelta(delta)` (`response.reasoning_text.delta` is handled the same way) |
| `response.output_item.done` (reasoning) | `WireEvent`, `PartDone(Reasoning(summary, origin, raw=item))`; summary parts are joined by a blank line |
| `response.output_item.added` (function_call) | `WireEvent`, `ToolCallStarted(id=call_id, name)`; remembers item id to call id |
| `response.function_call_arguments.delta` | `WireEvent`, `ToolArgsDelta(id=call_id, delta)` |
| `response.output_item.done` (function_call) | `WireEvent`, `PartDone(ToolCall(call_id, name, arguments, origin, raw=item))` |
| `response.output_text.delta` | `WireEvent`, `TextDelta(delta)` |
| `response.output_item.done` (message) | `WireEvent`, `PartDone(Text(joined output_text, origin, raw=item))` |
| `response.completed` | `WireEvent`, `Usage` (when present), `Done("tool_calls" or "end_turn", response_id)` |

`response.incomplete` yields `Usage` and `Done("max_tokens", ...)` when the reason
is `max_output_tokens`, otherwise `Done("other", ..., detail=reason)`. `Done` is
`tool_calls` when any function call was started in this stream.
Usage maps `input_tokens`, `input_tokens_details.cached_tokens`,
`output_tokens`, `output_tokens_details.reasoning_tokens`.

### Annotated stream: Chat (copilot, opencode)

| SSE data | Normalized events |
| --- | --- |
| (before the request) | `WireRequest`; `WireResponse` |
| chunk with `delta.reasoning_content` (or `reasoning`, `reasoning_text`) | `WireEvent`, `ReasoningDelta`; the first field name seen is remembered |
| chunk with `delta.content` | `WireEvent`, `TextDelta` |
| chunk with `delta.tool_calls[i]` (first for index `i`) | `WireEvent`, `ToolCallStarted(id, name)` (id defaults to `call_{i}`), then `ToolArgsDelta` if arguments are present |
| later `tool_calls[i]` chunks | `WireEvent`, `ToolArgsDelta(id, fragment)`; a name that arrives late fills the empty one |
| chunk with `finish_reason` | `WireEvent`; remembered |
| chunk with `usage` (often after `finish_reason`) | `WireEvent`; remembered |
| `[DONE]` | `WireEvent(name, "[DONE]")`; the read loop ends |
| after the stream | `PartDone(Reasoning)`, `PartDone(Text)`, one `PartDone(ToolCall)` per index in order, `Usage`, `Done` |

Only choice index 0 is read. Parts are emitted at the end because usage arrives
late. `Done.stop` is `tool_calls` when any call exists; otherwise
`stop` maps to `end_turn`, `length` to `max_tokens`, and anything else to `other`
with the finish reason as `detail`. Usage maps `prompt_tokens`,
`prompt_tokens_details.cached_tokens`, `completion_tokens`,
`completion_tokens_details.reasoning_tokens`.

## Redaction

`redact_headers` (in `responses.py`, used by both dialects) replaces the value
with `***` for these header names, compared case-insensitively:
`authorization`, `proxy-authorization`, `cookie`, `set-cookie`,
`chatgpt-account-id`, `x-openai-internal-codex-residency`, `x-api-key`. It is applied to the request headers in `WireRequest` and to the
response headers in `WireResponse`. The real headers are used for the HTTP call.

Not redacted: the session header, `x-initiator`, and grok's `x-xai-token-auth`,
`x-grok-client-version`, `x-grok-client-identifier` (constant client identity,
not secrets).

## Partial responses and replay

A provider stream that ends without `Done` is incomplete, and the adapter also
raises for some cut-offs ([Errors](#errors)). Whatever arrived is still emitted:
`WireEvent` for every SSE frame, `TextDelta`/`ReasoningDelta`,
`ToolCallStarted`/`ToolArgsDelta`, and `PartDone` for items the provider
finished. The daemon stores those events as diagnostic history.

Replay uses conversation parts, not deltas. An interrupted response therefore
never becomes an assistant message: there is no honest way to turn partial
deltas or partial arguments into a complete item, and reasoning encryption (for
codex) only exists in a finished reasoning item's `raw`. A response that reached
`Done` has complete `PartDone` items, and startup recovery restores exactly
those, with their `origin` and `raw`, without another provider call. A tool call
in restored parts is closed as "did not run" unless there is durable evidence it
started. Partial output stays inspectable in the event log and is clearly
labelled in the recovery notice ([daemon.md](daemon.md#crash-recovery)).

## Errors

All failures are `ProviderError(message, status=None, body=None, phase=…,
retry_after=None)`. Messages carry no credentials. `phase` says where a failure
happened: `transport` (connect/read), `response` (an HTTP status), or `stream`
(an error event, or a stream that ended before completion). The agent loop
retries only a `transport` or `response` failure with status `None`, `429`,
`502`, `503` or `504`, and only before any response stream event was received.

| Case | Result |
| --- | --- |
| HTTP status >= 400 | `"{name}: HTTP {status}"`, with `status`, `body` (response text, first 4000 characters), `phase: "response"` and a bounded `retry_after` when the header is valid |
| `httpx.HTTPError` while connecting or streaming | `"{name}: network error: {ExceptionName}"`, `phase: "transport"` |
| Responses `error` or `response.failed` event | `"{name}: stream error {code}: {message}"`, message cut to 4000 characters, `phase: "stream"` |
| Chat chunk with an `error` object | Same format |
| Responses stream ends without `Done` | `"{name}: stream ended before response.completed"`, `phase: "stream"` |
| Chat stream ends with neither `[DONE]` nor a `finish_reason` | `"{name}: stream ended before a finish_reason or [DONE]; the reply may be cut off"`, `phase: "stream"` |

Auth failures (`AuthError`, `CredentialError`) are raised while building
headers, before `WireRequest` is yielded ([auth.md](auth.md)).

Limits: request timeout 300 s (15 s connect). One SSE event may be at most
8,000,000 characters (`sse.py`), otherwise `ValueError`. Non-JSON event data is
passed through as a string in `WireEvent.data`.


## Offline mock provider

Set `NEXUS10_DEV=1` to register `mock` and its scenario names as models, for
example `mock/hello` or `mock/tool_marathon`. The mock provider makes no network
requests. It drives the same `Request` and `Event` protocol as live providers:
`WireRequest`, `WireResponse`, a `WireEvent` before each normalized delta or
completed part, usage, and `Done`. This keeps the normal loop, persistence, turn
summaries and clients in the path while scripts make outcomes repeatable.

Scenarios are in `nexus/mock/scenarios/`; the catalogue contains `hello`,
`bash_wait`, `cancel`, `parallel_tools`, `question`, `streaming_rich`, `stress`,
`tool_marathon`, `provider_failure`, `parallel_subagents` and `context_pressure`.
`parallel_subagents` requires the `orchestrate` agent, which has the `session`
control tool; child sessions receive their own scripted reply. Tools run through
the normal workspace tool layer. `tool_marathon` creates and
edits `mock-output.md` in the selected workspace, so run it in a disposable
workspace. `provider_failure` exercises the normal retry path; `cancel` parks
until cancelled. Some old scenarios that depended on manager-only or live
services were intentionally not carried over.

## Not verified

* Whether every Codex-listed model id is served by the Codex backend.
* Whether some Copilot GPT models require Responses instead of Chat.
* Which models of each provider understand images; Nexus sends them to any model.
