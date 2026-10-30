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

| id | Label | Dialect | Endpoint (`base_url`) | Request URL | models.dev id |
| --- | --- | --- | --- | --- | --- |
| `mock` | Offline mock | scripted | — | `mock://<scenario>` | built in |
| `codex` | ChatGPT (Codex) | responses | `https://chatgpt.com/backend-api/codex` | `{base}/responses` | `openai` |
| `grok` | xAI (SuperGrok) | responses | `https://cli-chat-proxy.grok.com/v1` | `{base}/responses` | `xai` |
| `copilot` | GitHub Copilot | chat | `https://api.githubcopilot.com` | `{base}/chat/completions` | `github-copilot` |
| `opencode` | OpenCode Go | chat | `https://opencode.ai/zen/go/v1` | `{base}/chat/completions` | `opencode-go` |
| `openai` | OpenAI (API key) | responses | `https://api.openai.com/v1` | `{base}/responses` | `openai` |
| `groq` | Groq | chat | `https://api.groq.com/openai/v1` | `{base}/chat/completions` | `groq` |
| `nvidia` | NVIDIA | chat | `https://integrate.api.nvidia.com/v1` | `{base}/chat/completions` | `nvidia` |
| `ollama` | Ollama (local) | chat | `http://localhost:11434` (from `$OLLAMA_HOST`) | `{base}/v1/chat/completions` | none; models from `GET /api/tags` |

A provider has no default model ([models.md](models.md#no-default-model)); the mock's scripted
scenario defaults to `hello` inside `MockProvider`. `ProviderInfo` also has `verified` (a live multi-step tool turn has run through
the adapter). It is `True` for all four. Sign-in itself is not verified for
copilot, grok and opencode (PLAN.md, step 3).

### Quirks per provider

| id | Quirks |
| --- | --- |
| `codex` |
| `grok` |
| `copilot` |
| `opencode` |
| `mock` |

### What each quirk field does

`ResponsesQuirks` (`responses.py`):

| Field | Effect |
| --- | --- |
| `encrypted_reasoning` | Adds `"include": ["reasoning.encrypted_content"]` to the body, and allows reasoning items to be replayed (see below) |
| `replay_raw` | Own-origin text and function-call items are sent back verbatim (`raw`) instead of rebuilt |
| `prompt_cache_key` | Sends `prompt_cache_key` = conversation id, cut to 64 characters, when the request has one |
| `session_header` | Name of a header that carries the conversation id (cut to 64 characters) when the request has one |
| `initiator_header` | Sets `x-initiator` (as for Chat, below) |
| `vision_header` | Copilot: sends `copilot-vision-request: true` when any message carries an image, a user's or a tool's (`types.has_images`) |

`ChatQuirks` (`chat.py`):

| Field | Effect |
| --- | --- |
| `session_header` | Same as above |
| `initiator_header` | Sets `x-initiator`: `user` when the last message is from the user, otherwise `agent` (an empty history counts as `user`) |
| `vision_header` | Same as for Responses |

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

## Tool-result media

A tool can return images, not just text (today: an MCP server's `image` content; the shape is general). This section
is the checklist for a new dialect or provider. The design follows the MCP tools spec, where a result is an
**ordered list of typed content items** that the client hands to the model, and what opencode does
(`packages/ai/src/protocols/open-responses.ts`, `anthropic-messages.ts`, `openai-chat.ts`, which keep the list and
lower it per protocol).

**In Nexus.** `ToolResult(call_id, output, is_error, media)` where `media` is a tuple of
`Media(artifact, mime, name, data_url)`: the text comes first, then the images in order. A stored message holds
only `artifact` (an id in the artifact store, [api.md](api.md#artifacts-k8)), `mime` and `name`; the JSON codec drops
`data_url`, so stored messages, events and exports stay small and never hold image bytes. Just before a request is
built, `Agent.request()` runs `Runtime.render_media` on each message, which fills `data_url` from the artifact
(`data:<mime>;base64,…`). **A dialect only ever reads `data_url`** (use `types.tool_images(result)`, which
returns the images that have bytes) and never touches the artifact store. A `Media` without `data_url` is not sent.
`render_media` leaves it empty, and appends `[N image(s) not sent: <reason>]` to the result's text, when the model
takes no image input (`ModelInfo.image_input`, from models.dev `modalities.input`; a model the catalog does not
know counts as taking images) or the artifact's bytes are gone. Only PNG, JPEG, GIF and WebP are sent
(`artifacts.MODEL_IMAGE_TYPES`); audio stays an artifact with a text line. A tool returns media by returning
`ToolOutput(text, media)`, which is a `str`, so code cells, hooks and clipping treat it as plain text.

**What a dialect must decide.** Where do a tool result's images go on this wire? Check, in this order:

1. Can the tool-result item itself hold images? Then put them there, after the text, in order, and keep a text-only
   result as a plain string so existing fixtures do not change.
2. If not, the images follow the whole group of tool results as **one user message**, each preceded by a text part
   that says which call returned it. The tool messages must stay contiguous right after the assistant message that
   made the calls, so the extra message goes after the last of them.
3. If the wire takes no images at all, send the text only (the result already says an image was stored).

Then set `vision_header`-style quirks if the provider wants a flag when images are present, and add a recorded wire
fixture test (see `tests/test_tool_media.py`) plus a real call, as for any provider.

| Dialect | Where the images go | Verified live |
| --- | --- | --- |
| Responses | `function_call_output.output` becomes `[{"type": "input_text", "text": …}, {"type": "input_image", "image_url": <data URL>}, …]` | codex `gpt-6-luna`, copilot `gpt-6-luna`, grok `grok-4.7` (2026-10-29) |
| Chat | the `role: "tool"` message keeps the text; one `role: "user"` message follows the group with `[Image returned by tool call <id>: <name>]` then an `image_url` part per image | copilot `claude-sonnet-5` (2026-10-29) |
| Messages (Anthropic, J4) | planned: `tool_result.content` is an array of `text` and `image` blocks (`{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`); that dialect supports it natively | not built |

Things that are easy to get wrong:

* **A provider may reject small or odd images.** grok answered `HTTP 400 invalid_image` for a 16×16 PNG (minimum 512
  pixels); the same call with a 64×64 image worked.
* **Copilot refuses images unless `copilot-vision-request: true` is set.** The header is on for any request with an
  image anywhere, because a tool image makes the request a vision request.
* **Replay across providers.** Because only the reference is stored, a session that moves from one provider to
  another re-renders its tool images for the new dialect. Nothing about the old wire format is kept for them.
* **Wire events show the bytes.** The request body in a `wire_request` event contains the base64, because wire
  events are never clipped. Redaction does not apply to image data.
* **Prompt cache.** Changing whether images are sent (model switch to a text-only model) changes the prefix.

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
control tool; sub agents receive their own scripted reply. Tools run through
the normal workspace tool layer. `tool_marathon` creates and
edits `mock-output.md` in the selected workspace, so run it in a disposable
workspace. `provider_failure` exercises the normal retry path; `cancel` parks
until cancelled. Some old scenarios that depended on manager-only or live
services were intentionally not carried over.

## Not verified

* Whether every Codex-listed model id is served by the Codex backend.
* Copilot GPT models: the catalog (`provider.npm = @ai-sdk/openai`) routes them to `{base}/responses`, with `x-initiator`, and no encrypted-reasoning replay yet (`providers.copilot_uses_responses`). Verified live with `gpt-6-luna` (2026-10-29); other families stay on Chat.
* Which models understand *user* images; Nexus sends them to any model. Tool-result images are skipped for models the catalog marks text-only (see "Tool-result media").

## API-key and local providers (J1–J3)

`openai`, `groq`, `nvidia` and `opencode` sign in with a pasted key (`nexus login <id>`,
or `POST /api/provider/{id}/login` with `{"method": "api_key", "api_key": …}`). The key goes in
`auth.json`; `$OPENAI_API_KEY`, `$GROQ_API_KEY`, `$NVIDIA_API_KEY` or `$OPENCODE_API_KEY` is used
when set and nothing is stored (`llmhub/auth/api_key.py`). Providers that speak the OpenAI chat
format share `ChatProvider`, so adding one is a table row and a sign-in. `openai` uses the same
Responses quirks as codex. A model id may contain `/` (`groq/openai/gpt-oss-120b`); only the first
`/` separates the provider.

`ollama` needs no key. `nexus login ollama` records the server (`$OLLAMA_HOST`, else
`http://localhost:11434`). Its models are whatever the server has pulled (`/api/tags`, embedding
models left out); it reports no context window or prices, so those are unknown. `groq` and `nvidia` ran a live read + bash turn (`verified`); `openai` and `ollama` have not. NVIDIA's
catalog lists end-of-life models that answer HTTP 410, and some (deepseek-v4.1-flash) queue for
over a minute; the default is `openai/gpt-oss-20b`. Groq's free tier answers 429, which the retry handles.
