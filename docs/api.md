# HTTP API

Nexus Core runs as a daemon (see [daemon.md](daemon.md)). Every surface talks
to it over this API: the `nexus` CLI (`nexus-core/nexus/client.py`), and later
the TUI (`nexus-tui/`) and NSH (`nexus-nsh/`). The shape follows OpenCode's
`/api/*` (opencode.ai/v2/docs/api), cut down to what exists. Code:
`nexus-core/nexus/server.py`.

API 1.5 adds request-local streaming checkpoints. `text_delta`, `reasoning_delta`,
`tool_call_started` and `tool_args_delta` now have `id: null` and no SSE id/cursor.
Their data (and `part_done`) carries `request_id`, request-local `seq` and
`part_index`. Register the stream before reading `/turn`; seed the running
request's `partial`, merge only newer queued events, and replace the indexed part
on `part_done`. Id-less events never advance `Last-Event-ID`. Updated clients are required for streaming. Older clients may drop id-less
deltas or repeatedly resync until the response is saved; canonical assistant
messages remain available.
Requests in `/turn` also carry nullable `partial`, `http_status` and `ttfb`
(seconds); interrupted partials are diagnostic-only, never model context.

`GET /api/session/{id}/diagnostics` returns `{keep, compaction, waits}` (durable
question/permission asked/replied records). `POST` accepts `{"keep": true|false}`
to protect diagnostic events and wire bodies. A compaction record has
`session_id`, `first_id`, `last_id`, cumulative `removed` and `time`.
`GET .../event` includes `compaction`; JSON session exports include
`events_compacted`. The inspector explains lost diagnostics. Compaction advances
the global resync floor conservatively: any cursor below it receives 410,
including cursors whose rows remain in another interleaved session.

## Basics

- **Base URL.** `http://127.0.0.1:4747` for local clients (`$NEXUS_PORT`). Core
  binds `0.0.0.0` (all IPv4 interfaces) by default. `$NEXUS_HOST` overrides the
  default; `nexus serve --host` takes precedence over both.
- **Auth.** **The default network bind has no authentication: anyone who can
  reach Core can use its API. Use only trusted networks or a firewall.** For
  local-only, token-protected access, set `NEXUS_HOST=127.0.0.1` or pass
  `--host 127.0.0.1`. Loopback binds require `Authorization: Bearer <token>`, read from
  `~/.nexus/daemon.json` (mode 0600). `GET /api/event` also accepts
  `?token=` for browser `EventSource`. A missing or wrong token gets 401.
  A non-loopback bind serves without a token, so a phone can connect to
  `http://<host>:<port>` by address alone; bind only on a trusted network.
- **Bodies.** JSON objects, at most 1 MiB. Prompt requests allow 12 MiB for inline image data,
  and `PUT /api/project/{id}/file` allows 8 MiB so a 1 MiB UTF-8 file fits after JSON escaping
  (the file itself is still limited to 1 MiB). `POST /api/voice/transcribe` is a raw WAV,
  not JSON, up to 120 seconds ([voice.md](voice.md#speech-to-text)).
- **Compression.** A response of 500 bytes or more is gzipped when the client sends
  `Accept-Encoding: gzip`. The event stream (`text/event-stream`) and audio responses are never compressed. Browsers and
  iOS's `NSURLSession` ask for and decode gzip on their own, so a phone on LTE
  transfers a few times fewer bytes.
- **Errors.** `{"error": "..."}` with 400 (bad input), 401, 404 (unknown id),
  409 (a turn is already running, a stale revision, or another conflict below),
  410 (history no longer retained; see [Event stream](#event-stream)),
  413 (voice audio over 120 s), 422 (a voice request that cannot run, with `code`)
  or 502 (a provider, auth or one-shot failure).

## Terminal resource API

The terminal-specific HTTP and streaming contract is in [terminals.md](terminals.md).
Terminal routes require the daemon bearer token when Core has one. In the tokenless
non-loopback bind they accept only requests whose actual TCP peer is loopback, whose
`Host` (and `Origin`, when present) names a loopback host on the published port, and
that carry no `Forwarded`, `X-Forwarded-*`, `X-Real-IP` or `X-Client-IP` header.
Anything else is refused before body parsing or idempotency replay, so a remote client
can neither reach the shell nor replay a local terminal mutation by guessing its
idempotency key. The configured URL, `Host` and forwarding headers are never treated
as proof of a local peer. A local reverse proxy that strips those headers is outside
this isolation: authenticate the daemon when terminals must pass through one. The
terminal stream is separate from session events and model context. Replay after
discarded output is explicitly lossy; see the contract.
`PATCH /api/terminal/{id}` with `{"name": "…"}` renames a resource and returns
its updated metadata. Names default to `Terminal` and have the validation and
persistence rules documented in the terminal contract. Metadata may include
`turn_id`, `tool_call_id`, `control_acquisitions` and `control_releases` for
bounded diagnostic provenance. Tool links identify actual originating calls;
manual terminals do not infer them from the latest session turn. Restart clears
controller identity on every persisted resource, and outgoing queue overflow
immediately revokes the slow attachment and its writer authority.

## Endpoints

`GET /api/session/{id}/inspect` is the read-only native inspector projection
(**Inspector API v2**, `"version": 2`; full contract and example in
[inspector.md](inspector.md#inspector-api-v2)). Query: `view` (`Timing`
default, `Conversation`, `Steps`, `Requests`, `Raw`, `Questions`), `before`
(opaque cursor from `next_before`; absent = newest page), `limit` (default 50,
max 200), `session` (inspect another member of the same tree), and the filters
`q`, `kind`, `status`, `type` (comma list of event types). The old `page`
parameter is gone: `page=0` is ignored, any other value is 400. Returns
`{version, session_id, session, view, limit, before, next_before, more, generated,
head, session_head, log_head, generation, floor, filters, cache, cache_range,
tree, notes, omitted, text, records, lanes}` with `Cache-Control: no-store`.
Bad view/limit/cursor/session/filter is 400; an unknown session is 404.

- `GET /api/session/{id}/inspect/head[?session=]` returns
  `{session_id, session, head, session_head, log_head, generation, floor}` cheaply
  (indexed `max(id)` reads) so a client can skip a refetch when nothing changed.
- `GET /api/session/{id}/inspect/record?kind=&id=` returns one full record
  (`{kind, id, session_id, detail_bytes, record}`) for any `detail` the page
  clipped. `kind` is `message|event|turn|request|tool|attempt`; a tool id is
  `turn_id:call_id`. 404 when the record is not in this session's tree.

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/bash` | | Background bash jobs (metadata: status, command, exit code, counts, optional `turn_id` and `tool_call_id` of the recorded call that started it; jobs are pipe processes, never terminal resources) |
| GET | `/api/bash/{id}` | | One job with its retained output (404 if missing) |
| DELETE | `/api/bash/{id}` | | 204; kills it (a finished job is unchanged) |
| GET | `/api/kernel` | `?summary=1` | Live kernels, metadata, retained cells and variable previews; `summary` omits the retained cell bodies |
| GET | `/api/kernel/{id}` | | One kernel (404 if missing) |
| DELETE | `/api/kernel/{id}` | | 204; 409 while busy |
| GET | `/api/config` | | `{revision, values, defaults, schema, errors, zmx_unavailable}` for the initial settings; `zmx_unavailable` is why ZMX cannot run on this machine (non-POSIX), else null |
| PATCH | `/api/config` | `{expected_revision, set, unset}` | updated config; stale revision is 409 |
| GET | `/api/info` | | `{name, version, api_version: {major, minor}, code, pid, home, url, log: {generation, head, head_cursor, floor, floor_cursor}, retention, retention_status, retry, idempotency}` |
| GET | `/api/snapshot` | `?session=`, `?message_limit=` | projects, sessions, running turns, pending waits, optional bounded session view and an event watermark |
| GET | `/api/search` | `?q=`, `?project=`, `?archived=exclude\|only\|include`, `?limit=` (1..100, default 30) | `{query, hits: [{session_id, kind: "title", "user" or "final_answer", message_id, snippet, rank, session}]}`: one hit per session, best first ([Search](#search-k5)) |
| GET, POST | `/api/usage` | | GET reads the saved snapshot; POST refreshes it. `{fetched, providers: [{id, label, source, plan, windows, notes, error}]}` ([Usage](#usage-k11)) |
| GET | `/api/artifact/{id}` | | the stored bytes, served as an attachment with the stored media type ([Artifacts](#artifacts-k8)) |
| GET | `/api/provider` | | `[{id, label, dialect, base_url, catalog, connected, verified, methods}]`; `methods` are the sign-ins a client can start (`browser`, `device`, `cli`, `local`, `api_key`); an API-key provider adds `key_hint`, `key_env` and `key_source` (`stored`, `env` or null); `claude` adds `note` ("Needs Claude Code 2.1.248 or newer", or that Claude Code is not installed), shown in the provider detail of both clients. With ZMX on, `claude-code`/`agy` rows follow with `zmx: true`, `dialect: "external-cli"` and no `methods` ([external-workers.md](external-workers.md)) |
| GET | `/api/provider/{id}/login` | | the in-progress sign-in, or `{status: "idle"}` ([Configuration](#configuration-provider-login-and-context-budget)) |
| POST | `/api/provider/{id}/login` | `{method?, api_key?}` | 202 while a browser, device, CLI or local flow runs; 200 `{status: "connected"}` for an accepted key. 409 if that provider is already signing in. 400 for a method the provider does not list. A `browser` flow listens on the daemon's `127.0.0.1:1455` and publishes its URL; the client opens it |
| DELETE | `/api/provider/{id}/login` | | 204; cancels a pending flow and signs out |
| GET | `/api/model` | `?provider=` | `{source, models: [ModelInfo + ref]}` for connected providers; ZMX models add `zmx: true` and are offered only in subagent pools |
| GET | `/api/model/last` | `?project=`, `?agent=` | `{model, source}`: the model a new session for this agent starts with: `agent` (last selected for it in the project), `last` (last selected anywhere) or `none` with `""` ([daemon.md](daemon.md#model-selection)) |
| GET | `/api/agent` | `?project=` or `?directory=` | `{agents: [Agent], problems: [{path, error}]}` |
| (session JSON) | | | also `skills: [{name, description, scope, state, locked}]` and `mcp: [{server, tools, state, locked, error, scope?: "global" or "project"}]`: what the starting-context header switches. `state` is `on`, `off` or `unavailable`; a disabled project entry is not part of the session ([tools.md](tools.md#skills-and-the-skill-tool), [mcp.md](mcp.md#project-servers)) |
| GET | `/api/skill` | `?project=` or `?directory=` | `{skills: [{name, description, scope, path, sha256, allowed_tools, warnings}], problems: [{path, error, kind}]}` ([tools.md](tools.md#skills-and-the-skill-tool)) |
| POST | `/api/skill/install` | `{fetch_id, skills?, scope, project?, overwrite?}` or `{source, scope, …}` | `{installed, skipped, problems, available, scope}`: `/learn`, copies skills from a Git repo or folder ([tools.md](tools.md#learn-installing-skills)) |
| POST | `/api/skill/preview` | `{source, scope, project?}` | `{fetch_id, source, scope, requested, problems, found: [{name, description, installed, warnings}]}`; the fetch is kept 10 minutes ([tools.md](tools.md#learn-installing-skills)) |
| GET | `/api/skill/{name}` | same | one skill plus `body`, `content` (complete UTF-8 `SKILL.md`, including frontmatter), `extra`, `files` (tool-readable resource paths), and `children` (sorted immediate supporting file/folder names, excluding `SKILL.md`; directories end in `/`, symlinks are not followed) |
| GET | `/api/mcp` | | `{servers: [...]}`: MCP server states, no secrets, never connects ([mcp.md](mcp.md#api-and-events)) |
| GET | `/api/mcp/{name}` | | one server plus `tool_names`, `log` |
| POST | `/api/mcp/{name}/restart` | | close and reconnect; returns the row |
| GET | `/api/mcp/{name}/resources`, `/resource`, `/prompts`, `/prompt` | `?uri=` / `?name=&arguments=` | read-only MCP resources and prompts, fenced untrusted text; connects the server; also under `/api/project/{id}/mcp/{name}/` ([mcp.md](mcp.md#api-and-events)) |
| GET | `/api/extension` | `?project=` or `?directory=` | `{tools: [{name, description, scope, path, sha256, timeout}], problems: [{path, error}]}`: user tools ([tools.md](tools.md#user-tools)) |
| GET | `/api/voice` | | `{stt, tts}` status; never loads or downloads a model ([voice.md](voice.md#status)) |
| POST | `/api/voice/transcribe` | raw mono 16 kHz PCM16 WAV; `?partial=1` | `{text}`; 409 `voice_busy` for an overlapping preview; transcript is not sent to a model ([voice.md](voice.md#speech-to-text)) |
| POST | `/api/voice/stt/prepare` | empty | Load the cached STT engine before recording; returns `{state: "loaded"}` or a voice error |
| POST | `/api/voice/stt/download` | empty | 202 `{state: "downloading", bytes_done, bytes_total, error}`; explicit consent to download pinned weights ([voice.md](voice.md#speech-to-text)) |
| POST | `/api/voice/tts/download` | empty | 202 `{state: "downloading"}`; consented download of `[speech] tts_model`; progress is `voice.progress` with `target: "tts"` and the state shows in `GET /api/voice` |
| DELETE | `/api/voice/{stt or tts}` | | removes the configured model repo from the HF cache; 409 while downloading or playing; `{repo, freed, ...status}` |
| GET | `/api/search/service` | | `{state, url, runtime, detail}` for the local SearXNG behind `web_search`; `state` is `running`, `stopped`, `absent` or `unavailable` |
| POST | `/api/search/service/start` | empty | starts it (Apple `container`, else Docker; creates `nexus-search/.env` once) and waits up to 30 s; concurrent starts are serialized, and an existing Apple container is rechecked after starting its system; the status, or 422 `search_start_failed` |
| POST | `/api/voice/speak` | `{session_id}` | streams the latest completed answer as `audio/wav` ([voice.md](voice.md#speech)) |
| POST | `/api/voice/speak/stop` | empty | `{stopped}` |
| GET | `/api/agent/{name}` | `?project=` or `?directory=` | `Agent` with `prompt` |
| GET | `/api/project` | | `[Project]`, most recently used first |
| POST | `/api/project` | `{path, name?}` | the project at `path`, created if new |
| GET | `/api/project/{id}` | | `Project` |
| PATCH | `/api/project/{id}` | `{name?, status?, expected_revision?}` or `If-Match`; at least one of `name`, `status` | `Project`; a stale revision is 409; `status` is `current`, `passive` or `inactive` (see [Project rail](#project-rail-order-status-and-removal)) |
| POST | `/api/project/{id}/move` | exactly one of `{before: id\|null}` or `{after: id\|null}` | `{projects}`: every visible project in the new order. `before: null` is the end, `after: null` the start |
| POST | `/api/project/{id}/remove` | | `{id, removed: true, sessions_kept}`; unregisters the project and keeps its sessions stored, never touching files; 409 while a session is running or has pending work |
| DELETE | `/api/project/{id}` | | 204; **permanently deletes** the project and its sessions (the rail uses `remove` instead) |
| GET | `/api/project/{id}/worktrees` | | the project checkout's worktrees (`repository`, `main`, `worktrees[]`, `project_directory`); 400 outside Git |
| GET | `/api/project/{id}/git` | `?worktree=` | registered checkout status and changed paths |
| GET | `/api/project/{id}/git/diff` | `?path=`, `?worktree=` | staged and unstaged diffs for one path, capped at 1 MiB each |
| GET | `/api/project/{id}/git/live` | `?worktree=` | working and idle sessions associated with the registered checkout |
| POST | `/api/project/{id}/git/action` | keyed; `{action, worktree?, paths?, value?, confirmed_live?}` | stage, unstage, commit, fetch, pull, push, switch or branch action |
| POST | `/api/project/{id}/files` | `?session=` | create, rename or delete a workspace entry |
| GET | `/api/project/{id}/files` | `?path=`, `?session=` | one directory level ([Project files and Git](#project-files-and-git)) |
| GET | `/api/project/{id}/file` | `?path=`, `?session=` | UTF-8 text, a SHA-256 `revision` and a language hint; binary or over 1 MiB is rejected |
| PUT | `/api/project/{id}/file` | `?path=`, `?session=`; `{content, expected_revision}` | writes only if the revision still matches; `"new"` creates; 409 `stale_file` ([File writes](#file-writes-k10)) |
| GET | `/api/project/{id}/files/search` | `?q=`, `?limit=`, `?directories=`, `?session=` | `{query, paths}`: visible paths containing `q` |
| GET | `/api/project/{id}/prompts` | `?q=`, `?limit=`, `?before=` | prompt history for Up-arrow recall, newest first |
| GET | `/api/project/{id}/mcp` | | this project's `.mcp.json` and `.agents/mcp.json`: `{project_id, file, exists, revision, error, warnings, servers, files, duplicates}`; never starts a server ([mcp.md](mcp.md#project-servers)) |
| GET | `/api/project/{id}/mcp/{name}` | | one project MCP row plus `tool_names` and `log`; never starts a server |
| POST | `/api/project/{id}/mcp/{name}/restart` | | close and reconnect a project server; returns the row |
| GET | `/api/session` | `?project=`, `?directory=` or `?parent=`, plus `?archived=exclude\|only\|include` | `[Session]` |
| POST | `/api/session` | `{project` or `directory` or `parent, agent?, title?, model?, permission?, effort?, disabled_tools?}` | `Session`; with `parent`, a child in the parent's directory (`launched_by: "user"`) |
| GET | `/api/session/{id}` | | `Session` |
| PATCH | `/api/session/{id}` | `{agent?, title?, model?, permission?, effort?, disabled_tools?, expected_revision?}` (`""` clears model or effort; `If-Match` also accepted) | `Session`; a stale revision is 409 |
| DELETE | `/api/session/{id}` | | 204; interrupts a running turn first |
| POST | `/api/session/{id}/archive` | | `Session` for the whole root tree; idempotent |
| POST | `/api/session/{id}/unarchive` | | `Session` for the whole root tree; idempotent |
| POST | `/api/session/{id}/model` | `{model}`; `""` returns to the agent's own model (400 when it names none); a non-empty ref is remembered | `Session` |
| POST | `/api/session/{id}/agent` | `{agent}` | `Session` |
| GET | `/api/session/{id}/switch-cost` | `?model=`, `?agent=` | `{cache_lost, reasons, cached_tokens, input_tokens}`: ask before switching ([Cache warning](#cache-warning-k9)) |
| GET | `/api/session/{id}/usage` | | the session's spend ledger `{since, models}` (also in `/turn`) |
| GET | `/api/session/{id}/artifact` | | `[{id, mime, name, bytes, source, created}]` |
| GET | `/api/session/{id}/grant` | | `{grants: [...]}`: what "allow for this session" now covers |
| DELETE | `/api/session/{id}/grant` | | 204; forget the grants |
| POST | `/api/session/{id}/workspace-tools` | | `Session`; activates the two on-demand workspace tools (idempotent) |
| POST | `/api/session/{id}/directory` | `{directory}` | `Session` + `change`; 200 applied/unchanged, 202 pending while a turn runs |
| GET | `/api/session/{id}/worktrees` | | `{repository, main, session_directory, worktrees: [...]}` |
| POST | `/api/session/{id}/worktrees` | `{action: "list" or "create" or "delete", path?, branch?, base?}` | the list, the created/reused worktree, or the deleted worktree |
| GET | `/api/session/{id}/git` | | branch, ahead/behind and changed paths ([Project files and Git](#project-files-and-git)) |
| GET | `/api/session/{id}/git/diff` | `?path=` | separate staged and unstaged diffs; untracked contents are not included |
| POST | `/api/session/{id}/shell` | `{command}` | 200 `{status, exit_code}`; runs `bash -c` in the session directory (composer `!` mode), adds command and output to context as a user message, never starts a turn |
| POST | `/api/session/{id}/prompt` | `{text?, images?, pastes?, mode?: "send"\|"steer"\|"queue"\|"interrupt"}` | 202. `send` (default) returns `{status: "busy", turn_id, origin, idempotency_key?}` and is 409 while a turn runs. `steer` and `queue` return `{input_id, mode, state: "pending"}`; while a turn runs they wait, and while idle the daemon starts that input immediately `interrupt` cancels the running turn exactly as `POST …/interrupt` does (denying a waiting approval, cancelling a question, stopping subagents), then starts a turn with this message and returns the `send` body plus `mode: "interrupt", interrupted: true`. Pending steers and queued messages stay and run after it (nothing is returned to the composer). Idle, it is `send`. It is checked first (empty text is 400, archived 409, ZMX 409), so a refused message never costs the running turn; it is idempotent under `Idempotency-Key` like `send` ([Pending inputs](#pending-inputs-steer-and-queue), [Image prompts](#image-prompts), [Collapsed text pastes](#collapsed-text-pastes)) |
| GET | `/api/session/{id}/input` | | `{inputs, held}` |
| POST | `/api/session/{id}/input/next` | | release a held queue and send its next item |
| PATCH | `/api/session/{id}/input/{iid}` | `{text?, mode?}` | edit text, or promote `queue` to `steer` |
| DELETE | `/api/session/{id}/input/{iid}` | | remove it and return its parts |
| POST | `/api/session/{id}/input/{iid}/move` | `{before}` | reorder pending input |
| POST | `/api/session/{id}/continue` | `{}` | 202 with turn provenance; starts a turn from the stored context and adds no user message. 409 busy/archived/unreconciled, 400 empty, 404 unknown |
| POST | `/api/session/{id}/interrupt` | | 204 |
| POST | `/api/session/{id}/seen` | | 204; a client is showing the session. Publishes a live-only `session.seen` so every client clears its unread mark |
| GET | `/api/session/{id}/message` | | `[Message]` in order |
| GET | `/api/session/{id}/event` | `?after=<event id>&limit=` (≤ 5000) `&type=a,b` | `{events, counts, more, floor, head, generation, history_truncated}`: the retained stored event log |
| GET | `/api/session/{id}/turn` | `?after=<turn id>` or `?before=<turn id>`, `&limit=` (default 50, ≤ 200) | `{turns, more_before, more_after, floor, usage}`: turn summaries, oldest first, and the spend ledger; see [Turn summaries](#turn-summaries) |
| GET | `/api/session/{id}/children` | | `[Session]`, direct children |
| GET | `/api/session/{id}/tree` | | the whole tree containing it, from its root: `Session` + nested `children` |
| POST | `/api/session/{id}/btw` | `{question, request_id?}` | 200 `{session, request_id, inherited, inherited_context}` immediately starts a hidden read-only fork, even while the parent is busy. `inherited_context` is the frozen inherited request prefix as JSON text. Follow the child's ordinary SSE events; nothing enters the parent. 400 empty/invalid question, 409 archived |
| GET | `/api/session/{id}/btw` | child session id | `{session, head, inherited, calls, running, error, messages, partial, events}`: reconnect checkpoint for a side run. Messages exclude the inherited prefix; events are compact tool/nested-call progress. 400 for a non-btw session |
| POST | `/api/session/{id}/handoff` | `{}` | 201 `{from, to, reason, session, summary}`. Writes a summary with one tool-free request, then starts a new root session of the same agent, model, directory and switches whose first message (`delivery: "handoff"`) is that summary; `continued_from` links back. Like any new root session it reloads the global and project MCP configuration and connects enabled project servers; explicit MCP switches carry over, but servers that were only automatically switched off after a failed connect are retried. The old session is not changed. 409 busy/archived, 400 child or empty session |
| POST | `/api/draft/context` | session-creation fields | the first request a new session would send, with nothing stored ([Configuration](#configuration-provider-login-and-context-budget)) |
| GET | `/api/session/{id}/context` | | the provider request body plus `used`, `window`, `exact` context budget |
| GET | `/api/session/{id}/snapshot` | | the full frozen session snapshot (agent, prompt, tools, instruction file, child catalog) |
| GET | `/api/session/{id}/export` | `?format=markdown\|json&scope=session\|tree` | a read-only Markdown transcript or JSON document; see [Session export](#session-export) |
| GET | `/api/session/{id}/permission` | | pending approvals |
| POST | `/api/session/{id}/permission/{pid}` | `{reply: "allow" or "deny", scope?, note?}` | 204 ([Approvals](#approvals-k7)) |
| GET | `/api/question` | | every pending `ask` question request |
| GET | `/api/session/{id}/question` | | pending questions for that session (empty list when none) |
| POST | `/api/session/{id}/question/{qid}` | `{answers: [{question_id, selected?, text?}, …]}` | 204; 400 invalid answer, 404 unknown question or session, 409 already resolved |
| POST | `/api/client/view` | `{stream_id, client_id, project?, session?, panel?}` | the stored ephemeral view; 400 invalid id, 404 unknown project or session, 409 `stream_closed` or `client_mismatch` ([Client views](#client-views-ephemeral)) |
| GET | `/api/manager` | | `{enabled, mode, model, consent, live_consent, call: {call_id, holder, phase, error, card, live} or null}`: restores the Manager call after a reconnect; `call.live` is the GPT-Live diagnostics or `null` ([The Manager](#the-manager)) |
| GET/POST | `/api/manager/session` | none | GET current Manager session metadata or null; POST ensures/reuses it without audio or model work. An explicit `/api/snapshot?session=ID` retains this selected session metadata across reconnects; default snapshots still hide it. Explicit read-only debug navigation; normal session prompt returns 409 `manager_session_read_only` |
| POST | `/api/manager/live/stop` | `{stream_id}` | 204; holder only, closes audio while preserving the control call and backend session |
| POST | `/api/manager/start` | `{stream_id, client_id, audio?: boolean}` | start the call on this client, or move it here: `{call_id, holder, mode, model}`; 409 `manager_disabled` or `stream_closed` |
| POST | `/api/manager/stop` | `{stream_id}` | 204; ends the call, never stops a session |
| POST | `/api/manager/live/start` | `{stream_id}` (holder; `[manager] mode = "live"`) | Core opens the GPT-Live call: `{call_id, audio}`; 409 `live_mode_off`, `consent_required`, `live_busy`, `live_cancelled`, `live_unsupported`, `live_*` ([manager.md](manager.md#gpt-live-voice-m5-experimental-not-verified-against-the-real-service)) |
| WS | `/api/manager/live/audio?stream_id=&token=` | binary PCM16 mono 24 kHz both ways; text `mute`/`unmute` | holder only; close codes 4401, 4403, 4404, 4409 |
| POST | `/api/manager/say` | `{stream_id, text, source: "typed" or "speech"}` | 202 `{turn_id}`; the answer arrives as `manager.*` events |
| POST | `/api/manager/cancel` | `{stream_id}` | `{cancelled, muted, dropped, reason?}`: barge-in |
| POST | `/api/manager/activity` | `{stream_id, listening, speaking}` | 204 |
| POST | `/api/manager/card/dismiss` | `{stream_id, card_id}` | 204; a stale card id changes nothing |
| POST | `/api/manager/navigate/result` | `{stream_id, request_id, ok, reason?}` | 204; an unknown or late request id is ignored |
| GET | `/api/manager/episode` | | the Manager's own past conversations, newest first, `[{id, created, updated, continued_from, model}]`; open one with the session routes or the inspector |
| GET | `/api/manager/context` | | `{title, document, episode, episodes, clipped}`: the Manager's current hidden context as bounded, read-only text (episodes, recap, state snapshots and deltas, recent log) for the header's debug modal ([manager.md](manager.md#the-context-modal-debugging)) |
| GET | `/api/manager/audit` | `?limit=&tool=` | what the Manager did on the user's words, newest first: `[{id, time, episode_id, turn_id, tool, target, heard, ok, result}]` |
| GET | `/api/event` | `?session=` (that session and its descendants), `?cursor=<generation>:<id>` (or `Last-Event-ID`) | server-sent events (below) |
| GET | `/api/info` (handshake) | | `api_version` is `{major, minor}`. A client sends nothing past `/api/info` when the major differs; a newer minor only adds fields, so older clients keep working and say what they lack |
| GET | `/inspector` | | the session inspector page ([inspector.md](inspector.md)); static, no token needed to load it |

Configuration patch paths and polling behavior are described in [config.md](config.md).

### Objects

```jsonc
// Project
{"id": "prj_…", "path": "/abs/path", "name": "repo", "created": 1793…, "updated": 1793…,
 "revision": 1}

// Session
{"id": "ses_…", "project_id": "prj_…", "directory": "/abs/path", "title": "…", "agent": "build",
 "parent_id": null, "parent_call_id": null, "launched_by": "user", "children": ["ses_…"],  // who launched whom; a child row also has "tokens" (input + output spent)
 "fork": null,  // a fork child: {"parent_id": "ses_…", "inherited": 12}; its first `inherited` messages are copied from the parent, in order
 "worker": null,  // a ZMX child (external-workers.md): {provider, label, model, status, permission_mode, context_complete: false, disclosure, not_applied, …}; its `tools`, `skills` and `mcp` are []
 "model": "codex/gpt-6.1-terra", "model_source": "session",   // session | agent | parent | none (default: only before models were remembered)
 "effort": "high", "effort_source": "agent",                    // session | agent | provider
 "tools": ["read", "edit", "write", "ask", "code", "expose_workspace_tools"], "disabled_tools": ["bash"],
 "workspace": {"entry": true, "active": false},   // on-demand control state
 "starting_directory": "/abs/path",               // frozen into the system prompt
 "directory_changed": false, "directory_change": null,  // or {from, to, status: "pending", source}
 "permission": "ask", "status": "idle", "revision": 1,  // revision increments on mutation
 "created": 1793…, "updated": 1793…,
 "archived": false,                                // archive is separate from busy/idle status
 "snapshot": {"version": 2, "origin": "created", "created": 1793…, "directory": "/abs/path",
              "agent": {"name": "build", "source": "project", "path": "…/build.md", "fingerprint": "sha256:…"},
              "instructions": {"path": "…/AGENTS.md", "status": "loaded", "bytes": 123, "sha256": "sha256:…"},
              "current_instructions": {"path": "…/AGENTS.md", "status": "loaded", "bytes": 123, "sha256": "sha256:…"},
              "workspace": {"entry": true, "active": false}, "directory_changes": 0,
              "granted_tools": ["read", "bash", "edit", "write", "ask", "code"],
              "child_agents": [{"name": "scout", "description": "…", "mode": "subagent", "source": "builtin",
                                "path": "…/scout.md", "fingerprint": "sha256:…"}]}}

// Worktree entry (in the worktree list)
{"path": "/abs/wt", "head": "…", "branch": "feature", "bare": false, "detached": false,
 "locked": null, "prunable": null, "main": false, "exists": true, "dirty": false, "changes": 0}

// Agent (see agents.md)
{"name": "plan", "description": "…", "source": "builtin", "path": "…/plan.md",
 "mode": "all", "color": null, "model": null, "effort": null, "tools": ["read", "bash"], "steps": null,
 "permission": {}}   // a subagent-capable agent also has "subagent": {pool, fallbacks, …}

// Message (parts encoded as in stream-events.md, including raw wire items)
{"id": "msg_…", "session_id": "ses_…", "seq": 2, "created": 1793…, "role": "assistant",
 "parts": [{"type": "tool_call", "id": "call_…", "name": "bash", "arguments": "{…}", "origin": "codex", "raw": {…}}]}
// A message also has `"origin": "manager"` (the message level, not a part's provider) when the Manager sent it into
// the session on the user's words; clients label it `via Manager`. A message typed by a person has no `origin`.
// Optional `"delivery"` is `steer`, `queued` or `handoff`.
// Optional `"notices"` carries structured facts about Core notices in this message; the message text is always
// exactly what the model received. A finished `wait: false` child adds
// {"kind": "subagent", "state": "finished"|"failed"|"interrupted", "session_id": "ses_…", "agent": "build",
//  "title": "…", "model": "provider/model", "effort": "high"|null, "duration": 310.2}. Older notices have no
// metadata; clients fall back to parsing `Sub agent ses_… (agent …) finished.`.

// Pending approval (also the data of permission.asked)
{"id": "per_…", "session_id": "ses_…", "tool": "bash", "call_id": "call_…", "args": {"command": "ls"},
 "rule": "bash(git *)"}   // `rule` only when a permission rule made it ask (docs/tools.md#permission-rules)

// Pending question (also the data of question.asked)
{"id": "qst_…", "session_id": "ses_…", "call_id": "call_…", "created": 1793…,
 "questions": [{"id": "db", "question": "Which database?",
                "options": [{"id": "pg", "label": "PostgreSQL", "preview": "…"}],
                "multiple": false, "allow_free_text": true}]}

// Answer item (POST …/question/{qid})
{"question_id": "db", "selected": ["pg"], "text": null}
```

Questions are separate from approvals: an answer is data for the model, never a
permission grant. Every question in a request must be answered exactly once, and
every question accepts free text (alone or with selections). Unknown question or
option IDs, duplicate or missing answers and invalid selection counts get 400 and
leave the request pending. A second reply to a resolved request (including after
cancellation) gets 409, with no second event or tool result; a bounded set of
recent resolutions is remembered, so a much older id gets 404 instead. Questions
live in memory and have no daemon-side timeout; they end with an answer or with
cancellation. Shapes and limits: [tools.md](tools.md#ask).

`permission` is `ask` (bash waits for a reply; read, `bash_output`, `bash_kill`,
`ask`, `edit`, `write`, the outer `code` call and workspace controls never ask)
or `allow` (the default for new sessions; nothing asks). Session-directory edits are approval-free by fixed
policy; see [tools.md](tools.md#file-mutations). `status` is
`busy` while a turn runs.

### Turn summaries

`GET /api/session/{id}/turn` (since `api_version` 1.2) returns what a client
shows about each turn without reading the event log: status, steps, stop,
error, recovery, every provider request with its `provider/model` ref, effort,
token usage, reasoning time and attempts (retries), and each tool call's timing
and error flag. Tool arguments and output stay out; they are in the messages.

- No cursor: the newest `limit` turns. `after=<turn id>`: the oldest `limit`
  turns started after that turn (reconnect). `before=<turn id>`: the newest
  `limit` turns before it (load earlier). Turns are always oldest first.
- `more_before` / `more_after`: whether older / newer turns exist beyond the page.
- `floor.seq`: messages with a lower `seq` have no summary (their turns were
  pruned, or they predate turn records). `null` when every message is covered.
- `usage`: the session's spend ledger per model, every request and attempt
  counted. It outlives pruned turns. `since` is when its first row counted
  (`null` when nothing was counted); for an older session totals start there.
- `usage` on a request or attempt is `null` when unknown (no report, or a crash
  before the provider reported it), never zeros. A request's usage is the sum of
  its attempts'.
- `stop` is the loop's `TurnEnd.stop` (`ended`), else the last request's stop.
  `ended` is `{stop, steps}` once the loop ended the turn, else `null`.
- `message_id` ties a request to its assistant message; `start_seq` is the last
  message seq before the turn.
- Requests that predate this version were backfilled once from the events that
  were still retained: `model` is then the bare provider model id (no
  `provider/` prefix), and usage is filled only where exactly one `usage` event
  fell in the request's range.
- 404 for an unknown session; 400 for an `after`/`before` turn that is not in
  this session, both cursors at once, or a `limit` below 1.

```json
{
  "turns": [{
    "id": "trn_…", "status": "completed", "started": 1793241740.77, "finished": 1793241775.34,
    "start_seq": 150, "steps": 2, "stop": "end_turn", "ended": {"stop": "end_turn", "steps": 2},
    "error": null, "recovery": "none",
    "requests": [{
      "id": "req_…", "step": 1, "status": "completed", "model": "openai/gpt-6.1-terra", "effort": "low",
      "started": 1793241740.78, "finished": 1793241747.71, "stop": "tool_calls", "error": null,
      "message_id": "msg_…", "usage": {"input": 48880, "cached": 48384, "output": 89, "reasoning": 0},
      "reasoning_seconds": 2.1,
      "attempts": [{"n": 1, "status": "completed", "delay": null, "started": 1793241740.78,
                    "finished": 1793241747.71, "error": null,
                    "usage": {"input": 48880, "cached": 48384, "output": 89, "reasoning": 0}}]
    }],
    "tools": [{"call_id": "call_…", "name": "bash", "state": "inserted",
               "started": 1793241747.72, "finished": 1793241748.01, "is_error": false}]
  }],
  "more_before": false, "more_after": false, "floor": null,
  "usage": {"since": 1793241740.79,
            "models": {"openai/gpt-6.1-terra": {"requests": 1, "input": 48880, "cached": 48384,
                                                "output": 89, "reasoning": 0}}}
}
```

After every turn closes (completed, failed or interrupted) the daemon publishes
a durable `turn.summary` event whose `data` is that turn's entry, before the
`session.status` idle event. Startup recovery publishes one for each turn it
closes. A client replaces its copy of that turn with it.

### Archive and session lists

Archive state is per root tree: `POST …/archive` and `POST …/unarchive` apply
to the named root and every descendant in one transaction and are idempotent.
A child-only request gets 400 naming its root; the operation must start from
the root. The response is the root `Session` with `archived` set. Repeating an
operation that changes nothing returns 200 with no transition event. Archive returns 409 if a background job in the tree is still running.

Clients expose this as `/archive`. They close the tabs named in the single
`session.archived` event's `sessions` list, using normal tab-close neighbour
selection, regardless of which client or Core initiated it. An open archived
session on mobile returns to Sessions. Unarchive does not reopen closed tabs.

`GET /api/session` filters by `archived`:

| Value | Returns |
| --- | --- |
| `exclude` (default, or omitted) | unarchived sessions only; archived trees are hidden |
| `only` | archived sessions only |
| `include` | every archived and unarchived session |

The filter applies to `?project=`, `?directory=` and `?parent=` lists; for
`?parent=` it filters the direct children. Any other value is 400. Explicit
`GET …/session/{id}`, `…/tree`, `…/children`, `…/message`, `…/event` and
`…/snapshot` ignore archive state, so an archived session stays readable and
inspectable.

Archiving is refused with 409 while the root or any descendant is running a
turn or waiting for an approval or question; nothing is interrupted. Archived
sessions refuse a prompt (409, naming the unarchive command) and refuse a new
child in the tree (409) until unarchived. Unarchive restores availability but
does not start a turn. Archive state never deletes messages, events, snapshots,
directories, worktrees or branches, and it does not stop a
background bash job. A background job's finish notice is stored as context but
does not wake an archived session; it is present when the user later unarchives
and resumes.

The daemon also archives idle trees itself (no API call): a root session whose
whole tree has had no activity for `[sessions] auto_archive_idle_seconds`
(default one hour; `0` disables) receives the same transition and one
`session.archived` event. Trees that are running, waiting for an approval or
question, holding pending input or running a background job are left alone
([config.md](config.md#sessions-settings), [daemon.md](daemon.md#session-archiving)).

### Workspace controls

`POST /api/session/{id}/workspace-tools` activates `change_session_directory` and
`worktree` (idempotent; also for sessions whose stored snapshot predates them)
and returns the session. The activation is stored with the snapshot. A new
session starts with only `expose_workspace_tools`, so `tools` reports it; older
sessions do not gain it until this call.

`POST /api/session/{id}/directory {directory}` validates an existing directory
(absolute, or relative to the current session directory) and moves the session.
Idle: **200** with `change.status: "applied"` (or
`"unchanged"`), the same forward announcement a tool call produces, and a
`session.directory_changed` event. Busy: **202** with
`change.status: "pending"`; the move is applied at the next tool/model dispatch
or when the turn ends. Session JSON shows the target in `directory_change` until
then. A second pending request gets 409. Errors: 400 for a missing/empty path or
a path that is not an existing directory, 404 for an unknown session.

`GET /api/session/{id}/worktrees` and
`POST /api/session/{id}/worktrees {action}` list, create/reuse or delete Git
worktrees for the session directory's repository. `path` is absolute or relative
to the main checkout. Create returns `path`, `branch`, `created`/`reused` and
does not change `directory`. Delete refuses the main checkout, dirty, locked or
missing worktrees, and paths used by a session, a project or a pending move; it
never deletes a branch. 400 carries Git's or the validation message; 404 is an
unknown session.
Limits and intent: [tools.md](tools.md#workspace-control-tools-on-demand).

### Manual continuation

`POST /api/session/{id}/continue` starts one normal turn from the session's
stored context. It is an explicit client action: reconnecting, reading a
session, restarting the daemon and startup recovery never continue a session by
themselves.

The request contains no user text. The daemon sends the frozen system prompt and
the saved messages exactly as they are, in order; it appends no synthetic user
message such as "continue" or an empty message, and it rewrites no history. It
uses the session's current effective model, effort, tools, permission and
directory, records the new provider request, response and any subsequent tool
calls like an ordinary prompt, and supports interruption, approvals and
questions.

```jsonc
// 202 from POST /api/session/ses_…/continue
{
  "status": "busy",
  "turn_id": "trn_…",
  "origin": "continuation",
  "continuation": {
    "predecessor": {
      "turn_id": "trn_…", "request_id": "req_…", "status": "interrupted",
      "request_status": "interrupted", "step": 2, "stop": null,
      "link": "request", "legacy": false, "note": null
    },
    "reconciliation": null          // or {calls, tool_message, notice}
  },
  "reconciled": null
}
```

| Situation | Status |
| --- | --- |
| A turn is already running (`session.status` `busy`) | 409; nothing is interrupted |
| The root tree is archived | 409 with the unarchive command |
| The session has no stored messages | 400, saying to send a prompt first and naming the prompt endpoint/CLI |
| The session has an unfinished durable turn | 409; restart the daemon so startup recovery reconciles it |
| Unknown session | 404 |
| Missing or wrong token | 401 |

`predecessor` names the preceding turn and its last provider request when they
exist. `link` is `request`, `turn` (a turn with no request), or `none`; a legacy
conversation without execution records says so in `note` rather than inventing a
link. A crash during a continuation keeps this object in `turn.data`, and
startup recovery reports it in `turn.recovered`.

Before a provider request is made, every saved tool call must have a result. If
a crash left calls dangling, the continuation closes them from durable evidence
and appends one labelled `[Nexus recovery: continuation reconciled interrupted
tool calls]` user notice. A completed result is preserved; a started call says
its outcome is unknown and is never re-run; a saved call that never started says
so; a legacy call with no execution record is labelled unknown. The tool message
and notice are committed in one transaction, so repeating the continuation
sends the reconciled context unchanged and appends nothing again. Partial
provider output remains diagnostic history in the event log and is never
reconstructed into arguments or reasoning.

Continuation announces itself in the durable `session.status` `busy` event:
`{status: "busy", turn_id, origin: "prompt" | "continuation", continuation?,
reconciled?}`. The matching idle event carries the same `turn_id` and `origin`,
and startup recovery's `turn.recovered` carries `origin` and `continuation` too.
This is execution/API metadata; it is not an extra model-visible user message.
After an ambiguous network failure, retry only with the same retained
`Idempotency-Key` (see below). Without a key, inspect the session or event log
before taking another action: the turn may already have started.

### Mutations, retries, continuation and reconnect

These four actions are separate and never substitute for one another:

| Action | Owner | What is retried/replayed | Durable evidence | It never… |
| --- | --- | --- | --- | --- |
| Client retry of the same mutation | client + `mutation` journal | One keyed HTTP request, replayed with the original status/body | `mutation` row: key, fingerprint, operation, target, accepted, turn/resource, status/body, expiry | Creates a second turn or repeats an event |
| Provider request retry inside a turn | agent loop + `turn_attempt` | Only a pre-stream transport/429/502/503/504 failure, bounded by policy | `turn_request`, `turn_attempt`, `retry_scheduled/started/exhausted` events | Runs a tool again, appends the user message, or continues after the first stream event |
| Event-stream reconnect | client cursor + `Stream` | Replay of durable events after the last applied cursor | Event row id, log generation, floor/head, per-subscriber overflow | Resubmits a prompt or answers a wait |
| Manual continuation | `POST …/continue` | Nothing automatically; one explicit new turn | `turn.data.origin/continuation`, `session.status` busy/idle provenance | Retries a failed request, answers a question/approval, or resumes a recovered turn |

Timeouts and budgets belong to these owners: the client owns its own transport
timeout (and keyed transport retries), the retry policy owns pre-stream provider
attempts, the event stream owns queue/backpressure, and manual continuation has
no automatic retry. A question or approval is resolved only by its explicit
reply (or an explicit cancellation); a reconnect or continuation never answers
one.

#### Idempotency-Key

Every mutating request may carry `Idempotency-Key`. The key is 1–128 characters
from `A-Z a-z 0-9 . _ : -`. It is scoped to this daemon's database and log
generation. The fingerprint is `sha256:` plus the hex digest of one string:
a JSON array `[method, path, sorted query pairs, normalized If-Match revision]`,
a newline, then the canonical JSON body (`sort_keys`, no extra space). An
`expected_revision` field is covered because it is part of that body. The header
is what the array records; a body-only revision is not also normalized into the
array. Credentials are not included.

| Situation | Result |
| --- | --- |
| New key | Prompt/continue atomically journal acceptance with the turn; other mutations journal intent before running and the returned result afterwards |
| Same key, same fingerprint, after acceptance | The original status and body are replayed with `Idempotent-Replayed: true`; no work runs again |
| Same key, different payload/target | **409** `{"code": "idempotency_key_reused", …}`; nothing changes |
| Concurrent identical requests | They share one acceptance and one result; only one turn/resource is created |
| Key for a target deleted after acceptance | The stored result is replayed before the target is looked up; the target is not recreated |
| Rejected prompt/continue (4xx) | Not journaled; the key may be retried after the conflict clears (for example, prompt while busy) |
| Other keyed mutation returns 4xx | The returned failure is journaled; use a new action/key after correcting the request |
| Crash before an ordinary mutation's response was journaled | **409** `mutation_outcome_unknown`; inspect the target, never blindly repeat the effect |
| Unknown/absent key | Current single-attempt behavior; a lost response is ambiguous |

The deduplication lifetime is **seven days after terminal resolution**
(`NEXUS10_MUTATION_RETENTION_DAYS`). Prompt and continue journal their
acceptance **in the same SQLite transaction as the turn**, so a crash cannot
leave a turn without a replay record or a record without a turn. Unresolved
entries never expire. When a keyed turn finishes (normally or through startup
recovery), its entry becomes resolved and starts the lifetime. After expiry the
key must not be retried automatically. Retained response bodies are bounded at
64 KiB; a larger accepted response replays as a bounded accepted record that
names the operation, target, resource and turn, and says to inspect the
resource. Other mutation helpers commit their changes independently of the response
journal. Their durable intent prevents automatic repetition in that crash window;
startup resolves it to a stable, expiring 409 `mutation_outcome_unknown`. This is
not an exactly-once guarantee for external side effects.

Prompt/continue acceptance responses include `idempotency_key`. A replayed
prompt/continue returns the original `turn_id` even if that turn has since
finished or been interrupted; it never schedules work.

#### Revisions

`Project` and `Session` carry a monotonic `revision`. Reads report it.
`PATCH /api/project/{id}` and `PATCH /api/session/{id}` (and
`POST …/model`, `POST …/agent`) accept `If-Match: <revision>` or
`expected_revision` in the JSON body. A stale value is **409**
`{"code": "revision_conflict", "revision": <current>, "current_revision": <current>, "resource": …}`
before any change. A keyed update is replayed from its journal before the
revision is rechecked, so a retry returns the original accepted result; fetch the resource separately
for its current state. It does not apply the old update again.
Archive/unarchive, directory moves, title/history, agent/tool switches and
model/effort changes increment the session revision. The workspace snapshot and
message history remain the authoritative context.

#### Mutation route matrix

`Keyed` means the route accepts `Idempotency-Key` and replays an accepted
response. `Revision` means it accepts an expected revision. `Crash` is the
durable reconciliation for a lost HTTP response.

| Route | Keyed | Revision | Crash / replay behavior |
| --- | --- | --- | --- |
| `POST /api/project` | yes | — | A replayed create returns the same project id; deletion later does not recreate it |
| `PATCH /api/project/{id}` | yes | `If-Match` / `expected_revision` | Replay returns the original project; stale revision 409 |
| `DELETE /api/project/{id}` | yes | — | Replay is 204 even though the project and sessions are gone |
| `POST /api/project/{id}/move` | yes | — | Applied against Core's current order (not a client's list); replay returns the original list; an unknown id is 400 |
| `POST /api/project/{id}/remove` | yes | — | Replay returns the original answer; a project already removed is 404 |
| `POST /api/project/{id}/git/action` | required | — | External effect: a pending outcome after a crash is `mutation_outcome_unknown`, never blindly replayed |
| `POST /api/project/{id}/worktree/integrate` | required | — | External effect on the base checkout; a pending outcome after a crash is `mutation_outcome_unknown`, never blindly replayed |
| `POST /api/session` | yes | — | Replay returns the same session; a child is not created twice |
| `PATCH /api/session/{id}` | yes | `If-Match` / `expected_revision` | Replay before recheck; stale revision 409 |
| `DELETE /api/session/{id}` | yes | — | Replay is 204; tombstones already recorded |
| `POST …/archive`, `…/unarchive` | yes | — | Replay is the original transition; repeating an unchanged state publishes nothing |
| `POST …/model`, `…/agent` | yes | yes | Replay is the original session; the new snapshot/notice is not rebuilt |
| `POST …/workspace-tools` | yes | — | Activation is idempotent; replay returns the activated session |
| `POST …/directory` | yes | — | Replay returns the original applied/pending result and does not move twice |
| `POST /api/project/{id}/files` | yes (journal only) | — | Filesystem mutations are external effects: an interrupted intent returns `mutation_outcome_unknown`; inspect the workspace before retrying |
| `POST …/worktrees` | yes (journal only) | — | SQLite intent/evidence only. Git create/delete is an external effect: a crash leaves a pending intent that becomes **409** `mutation_outcome_unknown` (and expires after the retention lifetime). Inspect the repository; never blindly repeat a failed create/delete |
| `POST …/prompt` | yes | — | Acceptance + turn journal are atomic. A crash before the task starts is recovered as an interrupted turn, and replay returns the original `turn_id` |
| `POST …/continue` | yes | — | Same as prompt; replay returns the original continuation provenance |
| `POST …/interrupt` | yes | — | Replay is 204; it never resumes or answers a wait |
| `POST …/permission/{pid}` | yes | — | A resolved approval replays its 204; distinct contradictory replies still conflict |
| `POST …/question/{qid}` | yes | — | A resolved question replays its 204; a different answer without the same key is 409/404 |
| `DELETE /api/kernel/{id}`, `/api/bash/{id}` | yes | — | A replayed delete is 204; runtime resources are not required to still exist |

Except for atomic prompt/continue acceptance, all keyed routes persist intent
before execution and their response afterwards. The ordinary replay outcomes in
the table require a recorded response; a crash in between produces
`mutation_outcome_unknown`, including for creates, deletes and wait replies.
Git operations are also external effects and cannot be made transactional with
SQLite. An unknown outcome is never rerun blindly. Mutations without a key keep the old single-attempt semantics; clients
must not assume an unkeyed POST is safe to retry.

**Ordering.** Durable acceptance order is the order of the `mutation` rows and
events; a keyed replay never reorders work. Synchronous resource updates
(project/session settings, model/agent switches, tool switches, archive) apply
immediately and serialize in the daemon's event loop; a stale expected revision
is rejected before anything changes. A directory move requested while a call is
running is queued and applied at the next model/tool dispatch boundary (or turn
end), never inside the running call. An interrupt cancels the active turn and
its descendants; it never answers a pending question or approval. A `send`
prompt while busy is **409**. `steer` is inserted after outstanding tool results
and before the next model call; `queue` starts a later turn. See
[Pending inputs](#pending-inputs-steer-and-queue).

#### Bounded provider retries

A provider request is retried within its agent step only when the failure
happens **before any response stream event** and is a transport error, `429`,
`502`, `503` or `504`. Authentication, validation, cancellation, persistence
and unknown errors are terminal. An HTTP `Retry-After` is honored when it fits
the remaining budget; otherwise the retry stops and reports it.

Defaults (`NEXUS_RETRY_MAX_ATTEMPTS`, `NEXUS_RETRY_BASE_DELAY`,
`NEXUS_RETRY_MAX_WAIT`, `NEXUS_RETRY_MAX_ELAPSED`): 3 attempts total,
exponential backoff with jitter from 1 s, at most 30 s per wait and 60 s of
retry time per step. Each attempt is recorded in `turn_attempt` and visible as
`retry_scheduled` / `retry_started` / `retry_exhausted` events with the attempt
number, delay, status, provider message and `Retry-After`. Once a stream event
arrives, failure is terminal; its partial output stays diagnostic. No retry
ever re-runs a tool, an outer `code` cell, a background command, approval or
question, and a daemon restart never resumes a pending retry. A settings change
waits for the next logical request; `self.request()` is unchanged across a
retry, so a retried attempt cannot silently change contents.

#### Consistent snapshot

`GET /api/snapshot?session=<id>&message_limit=<1..1000>` returns state at one
watermark:

```jsonc
{
  "watermark": {"generation": "log_…", "head": 4200, "floor": 0, "head_cursor": "log_…:4200"},
  "projects": [Project], "sessions": [raw session rows],  // each row also carries `worker` (null, or a ZMX child's facts)
  "running": [turn], "approvals": [pending approval], "questions": [pending question],
  "selected": {"session_id": "ses_…", "messages": [Message], "messages_limit": 200,
               "messages_more": false, "oldest_seq": 1}
}
```

The watermark is read in the same SQLite read transaction as the lists and the
bounded selected-session view. Apply events strictly after `head`; do not infer
loss from gaps in a filtered stream. If a cursor returns `history_unavailable`,
`cursor_generation`, `cursor_ahead` or overflow, fetch this snapshot, discard
stale pending waits, and resubscribe after `watermark.head_cursor`. Pending
questions/approvals are current in-memory waits only; the durable cancellation
events from a restart dismiss old waits. If the selected session was deleted
while a client was away, `GET /api/snapshot?session=` is 404; the Python client
retries the snapshot globally and emits `client.resync` with
`data.snapshot.deleted_session`, so the session view can close. A snapshot read
never mutates state, starts a turn or answers a wait.

### Session export

`GET /api/session/{id}/export` returns a read-only export of one session or its
descendants. It is an ordinary authenticated API call: the CLI uses it, and no
client reads SQLite.

| Query | Values | Meaning |
| --- | --- | --- |
| `format` | `markdown`, `json` | Required. |
| `scope` | `session` (default), `tree` | `session` exports the selected session only. `tree` exports the selected session and all of its descendants (never its ancestors or siblings), so exporting a child includes that child's own subtree. |

Responses:

| Format | `content-type` | Filename |
| --- | --- | --- |
| `markdown` | `text/markdown; charset=utf-8` | `nexus-<session id>-<scope>-<UTC timestamp>.md` |
| `json` | `application/json; charset=utf-8` | `nexus-<session id>-<scope>-<UTC timestamp>.json` |

The filename is sent as `content-disposition: attachment; filename="…"`.
`x-nexus-export-format-version` is `1`; `x-nexus-export-boundary` reports
`event:<event id>;seq:<bus seq>` for the read boundary,
`x-nexus-log-generation` names the log those ids belong to, and
`x-nexus-log-floor` is the retention floor at the boundary. A missing or unknown
`format` and an unknown `scope` are 400, an unknown session is 404, and a
missing or wrong token is 401.

Archived sessions export normally and are not unarchived; an archive state is
the same before and after the export. There is no export without a session.

#### Read-only contract and boundary

One export opens its own SQLite connection in read-only mode and reads
everything inside one transaction. Session rows, snapshots, messages and events
therefore come from one coherent database view even while turns keep writing;
the watermark (`boundary.log_generation`, `boundary.event_id`, `boundary.seq`,
per-session counts and the newest `session.updated`) names that view. `event_id`
is the durable boundary (a resume cursor is `<log_generation>:<event_id>`);
`seq` is the publishing daemon's counter and is informational only. The read
transaction is held while
the response streams. Writes after the boundary are not in the export, which is
why a running session's export is labelled durable partial state.

An export never calls a provider, starts or interrupts a turn, activates a tool,
writes a notice or event, reconstructs a missing snapshot, or reads a workspace
file or a credential. A running session exports what is durably stored at the
boundary; it is not interrupted or waited for. The JSON event log excludes live-only deltas. The inspector exposes checkpointed
request partials separately as diagnostic-only output.

The JSON event log is bounded so a huge session cannot hold the read snapshot
open without limit: once an export has emitted 256 MiB of event data, or has held
its snapshot for 300 s, the rest of the event log is omitted. Each session object
carries `events_clipped`, null when nothing was omitted, else `{reason,
events_emitted, events_stored, first_omitted_event_id}`. Messages and snapshots
are never clipped, and the Markdown export contains no event log.

Events are read in pages of 1,000 by event row id until a short page; this is
not the `/event` endpoint's 5,000-event page cap, and there is no `limit` query
parameter. Both formats stream. The export includes every event **retained at
the boundary**; when retention has moved the floor, `boundary.history_truncated`
is true and `boundary.floor` names the expired boundary. `boundary.expired_payloads`
counts wire envelopes whose body expired by payload retention (their ids/types
remain with an explicit expired marker); `boundary.wire_payload_bytes` is the
retained full wire payload size. The Markdown header names both cases. Messages
and snapshots are not affected by either lifetime, and the export never claims
an expired wire payload was present.

Only top-level transport `headers` on `wire_request` and `wire_response` events
are passed through provider header redaction (`authorization`,
`proxy-authorization`, `cookie`, `set-cookie`, `chatgpt-account-id`,
`x-openai-internal-codex-residency`, `x-api-key` become `***`). This applies to
transport metadata only; message text and tool arguments/results are preserved
as stored, with no text-pattern redaction.

#### Markdown

A readable transcript. It starts with the export metadata and a tree outline,
then one section per session, in order: the session's fields (agent, model,
effort, directory, parent/child relationships, archive state, snapshot origin
and instruction-file status), a snapshot note, the stored snapshot's full
system prompt and tool definitions, and the stored messages in order. Snapshot
instructions are labelled as the current stored snapshot, not a reconstruction
of every historical request. Each message shows its role, id and stored sequence; parts are labelled
`Text`, `Reasoning`, `Tool call` and `Tool result`. Reasoning shows the readable
summary; encrypted reasoning is labelled as present but unreadable and says it
is replayed unchanged. Tool arguments and results are fenced; arguments are
pretty-printed only when they are valid JSON (the JSON export keeps the stored
text exactly). A user message that starts with `[Nexus notice:`,
`[Nexus recovery:` or `[Notification:` is labelled a context notice. Child
conversations stay in their own session sections, never inside the parent's
messages.

No additional clipping is applied: existing clip notices are reproduced as
stored. Fences are one backtick longer than any backtick run in the content, so
content containing code fences stays valid. The Markdown export does not include
the wire event log; use JSON for that. An empty session says no messages were
stored. A session without a stored snapshot says so and does not reconstruct
one.

#### JSON (format version 1)

```jsonc
{
  "format": "nexus.session-export",
  "format_version": 1,
  "generated": 1793…, "generated_at": "2026-10-27T12:34:56+00:00",
  "scope": "tree", "root": "ses_…", "session_count": 2,
  "state": {"running": false, "running_sessions": [], "durable_partial": false,
            "note": "…"},                       // running/partial explanation
  "boundary": {"log_generation": "log_…", "event_id": 4200, "seq": 9000, "floor": 0,
               "history_truncated": false, "session_updated": 1793…,
               "counts": {"ses_…": {"messages": 4, "events": 88}}, "note": "…"},
  "sessions": [{
    "session": {"id": "ses_…", "project_id": "prj_…", "directory": "/abs/path",
                "title": "…", "model": null, "permission": "ask", "effort": null,
                "agent": "build", "parent_id": null, "parent_call_id": null,
                "launched_by": "user", "fork": null, "disabled_tools": [], "archived": false,
                "created": 1793…, "updated": 1793…},   // model/effort null = not chosen; fork as in Session
    "tree": {"depth": 0, "in_export": true, "parent_in_export": false,
             "children": ["ses_…"], "children_in_export": ["ses_…"]},
    "snapshot": { …full stored session_snapshot, or null… },
    "snapshot_origin": "created", "snapshot_created": 1793…,
    "snapshot_notice": "Recorded when the session was created…",
    "state": {"running": false, "durable_partial": false},
    "counts": {"messages": 4, "events": 88},
    "messages": [{"id": "msg_…", "session_id": "ses_…", "seq": 1, "created": 1793…,
                  "role": "user", "parts": [{"type": "text", "text": "…",
                                             "origin": null, "raw": null}]}],
    "events": [{"id": 42, "seq": 42, "time": 1793…, "type": "text_delta",
                "session_id": "ses_…", "data": {…}}]
  }]
}
```

Records keep the API shapes: `messages` is the `/message` list (ids, per-session
`seq`, parts with `origin` and `raw`), and `events` is the stored log (durable
event row `id`, publishing daemon's `seq`, time, type, session id, data) in order
and retained at the boundary. `tree.children` lists every direct child;
`children_in_export` lists the ones included by the scope. `snapshot` is the full
stored snapshot or `null`.

The snapshot provenance is labelled, never inferred. `origin: "created"` was
recorded when the session was created: its frozen agent prompt is the starting
one, later explicit changes are recorded forward in the messages, and an agent
switch replaces the snapshot. `origin: "agent-switch"` is the configuration
rebuilt when the agent was switched, and its notice says it is not the session's
original historical prompt. `origin: "reconstructed"` carries the reconstruction
note. A session with no stored snapshot gets `snapshot: null` and a notice that
the original prompt is unknown and was not reconstructed for the export.

CLI:

```sh
nexus session export ID --format markdown --output transcript.md
nexus session export ID --format json --scope tree --output session.json
nexus session export ID --format markdown                 # stdout
nexus session export ID --format json --scope tree -o -  # stdout ('-' too)
```

Omitting `--output` (or `-`) writes to stdout. An existing file is refused
unless `--force` is passed. The CLI downloads through the API to a temporary
`.part` file in the destination directory and publishes it only after the whole
response succeeded (exclusive hard link, or atomic replacement with `--force`); a failed download removes the temporary file and leaves an
earlier destination untouched. See [daemon.md](daemon.md#session-export).

`GET /api/session/{id}/event` reads one session's stored log by durable row id
(`?after=<event id>`); global events are not part of a session's log.

`GET /api/session/{id}/export` adds `x-nexus-log-generation` next to the boundary
headers, and the JSON `boundary` carries `log_generation` beside `event_id` and
`seq` (see [Session export](#session-export)).

## Event stream

`GET /api/event` is `text/event-stream`. The first frame is `server.connected`;
it has no `id:` line:

```
event: server.connected
data: {"version": "0.10.0.dev0", "generation": "log_1a2b3c4d5e6f7a8b", "head": 4200,
       "head_cursor": "log_1a2b3c4d5e6f7a8b:4200", "resumed_from": null,
       "session": null, "floor": 0, "stream_id": "stream_…"}
```

Every later frame is one durable log row (the one exception, `bus.overflow`, is
stream control and described below), and the SSE `id:` line is its cursor:

```
id: log_1a2b3c4d5e6f7a8b:4201
event: text_delta
data: {"id": 4201, "cursor": "log_1a2b3c4d5e6f7a8b:4201", "seq": 77, "time": 1793…,
       "type": "text_delta", "session_id": "ses_…", "data": {"type": "text_delta", "text": "Hel"}}
```

| Field | Meaning |
| --- | --- |
| `id` | The durable row id in this database's event log. Stable across daemon restarts. |
| `cursor` | `<log generation>:<event id>`. Ordering is ascending event id. |
| `generation` | Identifies this database's log. In `/api/info` as `log.generation`. A new or replaced database has a new generation; a copied database keeps the same one. |
| `seq` | The publishing daemon's own counter. It restarts at 1 on every daemon start. **Not** a cursor; after a replayed range it can jump backwards. |
| `head`, `head_cursor`, `floor` | The durable head and retention floor at the moment of the request (`/api/info` reports the same). |
| `stream_id` | This one event-stream subscriber's identity for ephemeral state. It is not durable and changes on every connection; see [Client views](#client-views-ephemeral). |

`generation`, `head`, `head_cursor`, `floor` and `stream_id` are stream/info
metadata, not fields of every event; each durable event carries `id`, `cursor`, `seq`,
`time`, `type`, `session_id`, `data` and `payload_state`. For a stored/replayed
`wire_request`, `wire_response` or `wire_event`, `payload_state` is `full` or
`expired`. An expired payload keeps its id/type/cursor and has an explicit
marker in `data`: `{"$nexus_payload": "expired", "state": "expired",
"event_type": …, "original_bytes": …, "expired_at": …, "reason": …, "note": …,
"resync": "/api/snapshot"}`. It is never an empty event and does not move the
floor. Live SSE delivery always carries the full payload when it is first
published; payload expiry affects subsequent replay/storage reads.

Events are committed before subscribers receive them; a storage failure raises
instead of publishing an event that could not be replayed. A session event whose
session row is already gone is not published (the deletion tombstone covers it).

STT download progress is a global `voice.progress` event. Its `data` contains
`model: "stt"`, `state` (`downloading`, `ready` or `error`), `bytes_done`,
`bytes_total` and `error`; progress measures growth in the pinned HF cache blobs.

### Resume, replay and cursors

Pass the cursor of the last event applied as `?cursor=<generation>:<id>`, or as
the `Last-Event-ID` header (a browser `EventSource` sends it automatically after
the `id:` lines above). If both are present the **query parameter wins**. The
stream resumes strictly after that id.

The cursor is validated before the stream opens:

| Situation | Status | `code` |
| --- | --- | --- |
| Malformed cursor text | 400 | `invalid_cursor` |
| Cursor from another log generation | 409 | `cursor_generation` |
| Cursor ahead of the log head | 409 | `cursor_ahead` |
| Cursor older than the retained floor | 410 | `history_unavailable` |
| `?session=` names an unknown session | 404 | — |

Those responses are ordinary JSON (not a stream) and carry an actionable resync:

```json
{"error": "…", "code": "cursor_generation", "resync": true,
 "generation": "log_…", "head": 4200, "head_cursor": "log_…:4200",
 "floor": 0, "floor_cursor": "log_…:0"}
```

Reload state from `GET /api/snapshot` (or `GET /api/project` and
`GET /api/session`), then resume from `head_cursor`; omitting the cursor means
"start live at the boundary". A cursor error is never silently turned into
"start now".

Prefix retention removes only a contiguous prefix of the log and moves `floor`
to the last removed event id. A cursor **at** the floor can resume (the next
event is `floor + 1`); a cursor below it gets 410 `history_unavailable` with the
current head and floor. The durable head high-water mark survives even when
every retained row was removed, so ids are never reused. Independently, heavy
wire payload bodies may expire without moving the floor; the event stays at its
id with `payload_state: "expired"` and the explicit marker above. A session's
messages, snapshots, turn/request/tool records and `session_usage` stay
independent of both diagnostic lifetimes.

The replay-to-live boundary is atomic. The server registers the live
subscription, reads the durable head as the same step, and then pages the stored
log up to that head (500 rows per page). Events published during catch-up are
already queued for the live phase; events at or below the head are skipped on the
live side. So a publication during replay is delivered exactly once, at the
handoff, in order. No `limit` is applied to catch-up: the whole history from the
cursor is streamed.

`bus.overflow` is stream control, not a log event: it has no `id:` line and is
not stored.

```
event: bus.overflow
data: {"type": "bus.overflow", "time": …, "session_id": null, "id": null,
       "data": {"dropped_after": 4210, "generation": "log_…", "head": 4300}}
```

A subscriber more than 10 000 events behind is cut off after this frame.
`dropped_after` is the last live event enqueued for that subscriber (not
necessarily the last one the client applied); reconnect from your own last
applied cursor, or resync.

### Filtering and gaps

With `?session=`, only events of that session, its descendants, and global events
(`session_id: null`) are sent. Ids are global, so a filtered stream naturally
skips unrelated numbers: **a gap alone is not evidence of loss**. Only
`bus.overflow`, a cursor/resync response, or a subscriber ending without either
means continuity was broken.

Membership is maintained from the log itself. A `session.created` replayed or
received during the stream adds a descendant whose `parent_id` is already in the
tree; a `session.deleted` tombstone removes that session and its cached
descendants. Filtering therefore never depends on a relationship that a deletion
removed. Deleting the selected session leaves the stream open for global events;
a client should reload that tree when it sees the tombstone.

Global events are stored with `session_id: null`: `project.created`,
`project.updated`, `project.deleted`, and the minimal `session.deleted`
tombstone (`{"id": "ses_…"}`) so a reconnect cannot miss them for lack of a
session row. Deleting a session still deletes its messages, events and
snapshots; the tombstone contains only the id needed for synchronization.
Deleting a project publishes one tombstone per deleted session, then
`project.deleted`. Removing one (`remove`) publishes the same tombstones, since the sessions leave every list,
then `project.deleted` with `removed: true`.

### Project rail: order, status and removal

`Project` carries two rail fields, both stored in Core so every client and the overview agree:

- `position` (integer): the user's saved display order among visible projects, `0` first. New projects append;
  databases from before this field start in creation order. `POST /api/project/{id}/move` renumbers all visible
  projects atomically and publishes `project.reordered`; `/api/snapshot` and `GET /api/project` list projects
  in this order. Clients keep initials assigned in creation order, independent of display order.
- `status`: `current` (normal, in the rail), `passive` (in the rail, dimmed) or `inactive` (not in the rail, listed
  by the API and restored by setting `current`). The names were confirmed by the user (2026-11-06); they
  live in one constant per codebase. Changing status is `PATCH` and bumps `revision`.

Older Cores omit both fields; clients then fall back to creation order and `current`.

| Type | Data |
| --- | --- |
| `config.changed` | `{revision}`: global, stored; clients reload `/api/config` |
| `agents.changed` | `{}`: an agent file was added, removed or edited; global, live only; clients reload `/api/agent` |
| `extensions.changed` | `{}`: a user tool file changed; global, live only; reload `/api/extension` |
| `skills.changed` | `{}`: a skill file changed; global, live only; reload `/api/skill` |
| `mcp.changed` | `{}`: the MCP servers or the two `[mcp]` settings in `config.toml` changed; running sessions keep theirs; global, live only |
| `mcp.connected` / `mcp.disconnected` / `mcp.failed` / `mcp.tools_changed` | `{server, …}`: an MCP server's connection changed; global, live only ([mcp.md](mcp.md)) |
| `extension.failed` | `{path, error}`: a user tool file is invalid; global, live only |
| `project.created` / `project.updated` | `Project`; global, stored |
| `project.reordered` | `{order: [id, …]}`: all visible projects in their new saved order; global, stored |
| `project.deleted` | `{id}`, plus `removed: true` when the project was unregistered with `remove` (its sessions are kept); global, stored |
| `session.created` / `session.updated` | `Session`, stored in that session's log |
| `session.snapshot` | a snapshot summary, emitted once when an old session is lazily reconstructed |
| `session.deleted` | `{id}`: a global tombstone stored without the session's history |
| `session.archived` / `session.unarchived` | `{root, sessions: [ids], archived, revisions: {id: revision}}`: one transition for a whole tree, stored in the root's event log; it bumps each session's revision, and `revisions` carries the new values |
| `session.seen` | `{session, root}`, live only (never stored): a client showed the session, so every client drops its unread mark for it and its root |
| `session.status` | `{status: "busy" or "idle", turn_id?, origin?: "prompt" or "continuation", continuation?, reconciled?, turn_finished?}` before and after each turn and when child activity changes the parent's status. A parent remains busy while a child runs; `turn_finished: true` marks its own turn ending while its status remains busy. A recovered turn's idle also carries `reason` and `turn_id` |
| `message.added` | `Message`, once it is saved |
| `permission.asked` | pending approval |
| `permission.replied` | `{id, session_id?, allow, outcome: "allowed" or "denied" or "cancelled", reason?, scope?: "session", grant?, note?}`; a cancellation has `allow: false` ([Approvals](#approvals-k7)) |
| `permission.grants_cleared` | `{session_id}` |
| `artifact.added` | `{id, mime, name, bytes, source}` ([Artifacts](#artifacts-k8)) |
| `files.changed` | Global workspace invalidation: `{project_id, workspace, path?, paths?, revision?, source: "editor"/"user"/"disk", omitted?, polling?}`. Never carries a `session_id` |
| `files.watch` | Global `{project_id, workspace, source: "disk", status: "polling", poll_seconds: 3}` when native watching falls back |
| `git.changed` | Global `{project_id, workspace, source}` Git view invalidation; never session context |
| `question.asked` | pending question request (`id`, `session_id`, `call_id`, `questions`, `created`) |
| `question.replied` | `{id, session_id, call_id, outcome: "answered" or "cancelled", answers, reason?}` |
| `turn.recovered` | a turn a daemon restart interrupted: its outcome, restored messages, tool classifications, cancelled waits and lost runtime state (see [Crash recovery](#crash-recovery)) |
| `retry_scheduled` / `retry_started` / `retry_exhausted` | one provider retry transition: `{attempt, next_attempt?, attempt_id, delay?, reason, status?, retry_after?, max_attempts}` |
| `model_fallback` | a child switched after a pre-response failure: `{previous, model, reason}`; a context message also announces the switch |
| `log.retention` | a global retention pass: `{at, elapsed_ms, events: {removed, floor, head, protected, pinned, batches, retained_bytes, over_budget}, wire: {expired, expired_bytes, wire_bytes, over_budget, blocked}, migration, turn_links, scheduled, turns, mutations, storage, blockers, worked, settings}` |
| `session.notified` | `{from, text, notice}`: a `wait: false` child finished, and its answer goes to this (parent) session. `notice` is the structured subagent metadata clients also receive on the stored message |
| `session.workspace_tools` | `{active, entry, tools}`: the on-demand workspace controls were activated |
| `browser.failed` / `browser.tools_changed` | `{server, error}` / `{server, added, removed}`: a session's browser-use process failed or changed its tool list ([tools.md](tools.md#browser-tools)) |
| `session.worker` | a ZMX child's `worker` object, each time its CLI starts or ends a turn ([external-workers.md](external-workers.md)); `session.updated` follows with the same object on the session |
| `session.escalated` | `{needed, why, parent_id}`: a child reported it is blocked (its parent's report carries it) |
| `session.message_tool` | `{active, tools}`: `message_session` was exposed by the session's first `session` call (once) |
| `session.directory_changed` | `{from, to, branch, repository, worktree, source, project_id, frozen_directory, instructions}` |
| `worktree.listed` | `{repository, main, session_directory, worktrees: [...]}` |
| `worktree.created` / `worktree.deleted` | `{path, branch, head, repository, main, session_directory}` |
| `session.error` | `{kind: "provider" or "auth" or "request" or "interrupted", message, status?, body?}` |
| `bus.overflow` | `{dropped_after, generation, head}`: stream control, never stored; resume from your cursor or resync |
| `oneshot` | `{kind: "handoff" or "title", request, text, usage, model?}`: a tool-free request and reply, stored, never model context. Historical `btw` events remain readable |
| `btw.started` | On the parent: `{session, request_id, question, inherited}` correlates a modal with its hidden child, without adding a message |
| `btw.budget` | On the child: `{calls}` includes outer and nested code calls, up to 10. Further calls are refused |
| `session.handoff` / `session.handoff_failed` | `{from, to, reason}` on the old session, or `{session_id, error}` when an automatic handoff failed |
| loop and stream events | from [stream-events.md](stream-events.md): `wire_request`, `wire_response`, `wire_event`, `text_delta`, `reasoning_delta`, `tool_call_started`, `tool_args_delta`, `part_done`, `usage`, `done`, `step_start`, `tool_start`, `tool_end`, `turn_end`, `hook_fired` |

`wire_request` carries the exact request body and headers, with credentials
redacted. `wire_event` carries every provider SSE event as received. Nothing the
model sees is hidden from clients.

### Client views (ephemeral)

Each `GET /api/event` connection is one subscriber with its own `stream_id`
from `server.connected`. A client reports what it is showing with
`POST /api/client/view`:

```json
{"stream_id": "stream_…", "client_id": "nsh-ios-…", "project": "prj_…",
 "session": "ses_…", "panel": "files"}
```

`client_id` is a stable label only. The identity is `stream_id`: two windows or
two reconnects can share a client id and still keep separate views, and a report
for a closed stream never attaches to another one. The daemon keys the view by
the stream and drops it when that stream closes, whether the client disconnects,
the response is cancelled, or the daemon stops. A reconnect gets a new
`stream_id`; report again after its `server.connected` frame. The daemon sends
the stream id only on that stream's `server.connected` frame. Treat it as a
capability: do not log it, put it in a URL, or share it with another client.

Ids only: `project`, `session` and `panel` are short opaque identifiers (1–128
printable ASCII characters, no spaces), with `null` or `""` meaning none. A
`session` implies its project, so `project` may be omitted. An unknown project
or session is 404; a session that does not belong to the named project is 400.
A stream id that is not currently connected is 409 `stream_closed`; changing
`client_id` on an existing stream is 409 `client_mismatch`, so a client opens a
new stream instead. 200 returns the stored view (`client_id`, `project`,
`session`, `panel`, `updated`), including the project filled from a session;
the response does not echo the stream id.

There is deliberately no read route. Listing views would disclose every live
`stream_id` and `client_id` to any caller, including on a daemon bound without
a token, and a disclosed stream id is enough to overwrite that view. The
registry stays inside the daemon, where the Manager reads the call holder's
view in-process ([The Manager](#the-manager)); there is no global "current"
view, so the holder is whichever client started the call. Nothing is stored in SQLite, published as an event, or kept
across a restart.

A view is a UI hint, not authority: it is never an approval, a claim on a
session, or a navigation request. Screen contents, scroll position and drafts
are not accepted. The daemon still validates the referenced ids, and a Manager
must treat the rest as untrusted labels.

### The Manager

Full description: [manager.md](manager.md). This is the contract a client builds against. The Manager's events are
ordinary durable global events (`session_id: null`), so they replay from a cursor like any other; a hidden Manager
episode's own events (`message.added`, `session.status`, wire events) never reach any stream except one filtered to
that episode's id.

**Audio is optional.** `audio: false` on start only requires the Manager model consent. It does not open a microphone or GPT-Live; `/live/start` retains its separate Live consent check.

**The call.** `POST /api/manager/start` with the `stream_id` from `server.connected` makes that client the *holder*
and returns a random, non-secret `call_id`. Every `manager.*` event carries `call_id`; a client ignores events for a
call it does not hold, and `stream_id` never appears in an event or a response. Only the holder's `stream_id` is
accepted by `stop`, `say`, `cancel`, `activity`, `card/dismiss` and `navigate/result`; any other value is `403
{"error", "code": "not_holder"}` with no identifier echoed. Starting from another client moves the call (new
`call_id`); the previous holder sees a `manager.state` with a different `call_id` and shows "Moved to <holder>". The
call ends when the holder's event stream closes. `409 manager_disabled` when `[manager] enabled` is false.

`GET /api/manager/context` is the palette's read-only formatted debug view. It needs no call or holder and returns the same text
for both clients: `document` has the hidden episodes (current and earlier), the recap the current episode started
with, each turn's `<nexus_state>` block (snapshot or delta, with the user view when sent), and the recent log of
messages, reasoning summaries, tool calls and results. It is bounded and says what it cut: the last 40 log entries
and last 12 state blocks are shown, entries are cut at 1200 characters, and `clipped` carries
`{entries, log, states}`. The full raw log stays in the inspector. This is not a Manager timeline or chat view.

`GET /api/manager` also carries `call.live`, or `null` when GPT-Live is not running: `{style, peer,
client_attached, pending, counts}`. `style` is the wire route (`codex`), `peer` the WebRTC connection state,
`client_attached` whether an audio socket is open, `pending` the delegations waiting for a Manager answer, and
`counts` the call's counters (`audio.from_client`, `audio.to_client`, `event.<EventName>` and
`unknown.<wire type>`) so a silent call can be diagnosed without logs.

**Events** (`data` shown):

| Event | Data |
| --- | --- |
| `manager.session` | `{session}` current Manager session metadata after episode creation/rollover; no call id because debug navigation also works without a call. Clients reload canonical context only while explicitly viewing the debug session. |
| `manager.preview` | `{call_id, preview: {source, lines: string[0..3], clipped, text, details_clipped, tool: {name, arguments, clipped, status: "running" or "done" or "failed"} or null}}`. Bounded, redacted current model details: `text` retains up to 12,000 trailing characters (`details_clipped` announces omission), and tool `arguments` are formatted JSON up to 4,096 characters with a `clipped` flag. The legacy three-line excerpt remains. Text updates at most once per 150 ms, tool transitions and final excerpts immediately. Also restored as `call.preview` in status and `preview` in the start response. |
| `manager.state` | `{call_id, active, phase: "idle" or "thinking" or "replying", holder, mode, error: {code, message, fix} or null}`. `active: false` ends the call. `error.code` is `model_unavailable`, `model_failed` or `manager_error`; it stays until a turn succeeds |
| `manager.caption` | `{call_id, turn_id, role: "user" or "manager", text, final}` for text not otherwise spoken: the user's typed line (sent when `say` is accepted, `source: "typed"` only) and errors (`role: "manager"`) |
| `manager.speech` | `{call_id, turn_id, seq, text, urgent, proactive?}`: one sentence of the Manager's reply, `seq` counting from 0 within the turn, `urgent` true only for an unprompted line slipped into a reply, `proactive: true` only on an unprompted line (absent otherwise). A text-only client shows it at once; a speaking client fetches and plays it in order |
| `manager.card` | `{call_id, card: Card or null}`: the stage changed. `null` closes it (navigation confirmed, `clear_stage`, a dismiss) |
| `manager.navigate` | `{call_id, request_id, project?, session?, panel?, back?}`: move the holder's view and answer with `POST /api/manager/navigate/result`. Sent only for a turn with the user's words, and only to the call (the holder checks `call_id`). With `back: true` the other fields are the view Core resolved from its back stack; a client may use its own history instead |
| `manager.acted` | session-scoped (in the target session's stream): `{tool, origin: "manager", ok, heard, result}` for every act the Manager attempted on that session |

**Cards.** Every card has `{id, type, focused, title?}` plus:

```jsonc
{"type": "board", "project": "nexus", "columns": {"needs_you": [Row], "running": [Row], "finished": [Row]},
 "more": {"needs_you": 0, "running": 3, "finished": 0}}      // project is optional; 4 rows per column
{"type": "sessions", "rows": [Row], "more": 0}                // 8 rows
{"type": "approval", "session_id": "...", "handle": "auth-refactor", "approval_id": "...", "summary": "run: alembic upgrade head",
 "detail": "exact command or arguments", "clipped": false}
{"type": "question", "session_id": "...", "handle": "...", "question": "...", "options": ["..."]}
{"type": "diagram", "source": "flowchart TD ...", "clipped": false}
{"type": "excerpt", "handle": "...", "session_id": "...", "markdown": "...", "clipped": false}
{"type": "note", "markdown": "...", "clipped": false}
// Row: {n, session_id, handle, title, project, state, why?}; n numbers across the whole card (1, 2, 3 ...)
```

`state` is one of `needs_approval`, `needs_answer`, `blocked`, `failed`, `working`, `archived`, `idle` (the
Manager never produces `finished_unread`: Core has no unread source). Clients draw title and state from their own
store when they have the session. `focused` is true for every card a tool call produces; the unfocused proactive
cards of M4 will set it false. A card that picks a row opens that session through the client's normal navigation.

**Privacy notice.** `GET /api/manager` also returns `consent: {provider, version, required, accepted_at, notice}`
for the provider of `[manager] model`. The Manager reads any session, so session text reaches that provider; the user
acknowledges it once per provider and again when `version` changes. While `required` is true, `POST
/api/manager/start` and `/say` answer `409 {"code": "consent_required", "consent": {...}}`. A client shows `notice`
and acknowledges with `POST /api/manager/consent {accept: true, version}` (a wrong `version` is 400; `accept: false`
revokes); the response is the new `consent` object. The offline `mock` provider needs none (`required: false`,
empty `notice`).

**Speech.** `POST /api/voice/speak` has two forms. `{session_id}` is unchanged. `{stream_id, text}` speaks one Manager
sentence: the `stream_id` must be the call holder's (`403 not_holder` otherwise, also when `stream_id` is missing),
`text` is non-empty and at most 1200 characters after masking (`422 speech_text_too_long`), secrets are masked first
(`[redacted]`), and the response is a streamed WAV like the session form. If the previous sentence is still
playing the request waits (up to 10 s) rather than answering `409 speech_busy`, so a client may ask for the next
sentence early; sentences are spoken in the order requested. `POST /api/manager/cancel` also stops the sentence being
synthesised and adds `speech_stopped` to its response. The thinking cue needs nothing from Core: a client plays it
after about a second of `manager.state.phase == "thinking"`.

**Unprompted lines.** Attention ([manager.md](manager.md#attention-unprompted-lines)) emits `manager.speech` with
`proactive: true`, a `turn_id` of the form `mp_<hex>` (never one a client sent), `seq` from 0 within the line, and
`urgent: true` when it was slipped in between two sentences of a reply. A client shows it as the caption alone, with
no header indicator and no scrim, unless the user had called the Manager. The only card it may raise is an `approval` or `question`
card with `focused: false`; it never sends `manager.navigate`.

**Navigation.** After `manager.navigate` the client performs the move as if the user clicked, then sends
`navigate/result` with `ok` (and a short `reason` when it cannot: the file panel on iOS, a deleted session) and
reports its new view with `POST /api/client/view`. Core waits 5 s for the result; without it the Manager tells the
user it could not confirm the move.

**Origin.** A message the Manager sent into a session has `"origin": "manager"` ([manager.md](manager.md#origin--manager)).
`permission.replied` and `question.replied` made by the Manager carry `origin: "manager"` and `heard`.

**Hidden sessions.** `/api/session`, `/api/project`, `/api/snapshot` and `/api/search` never list a Manager
episode or its project. The snapshot's raw `sessions` rows now include `kind` (`"user"`), `topic` (a one-sentence
description once written, else `null`) and `topic_seq`.

## Crash recovery

If the daemon crashes or is restarted while turns are running, the next startup
reconciles them before it accepts a request ([daemon.md](daemon.md#crash-recovery)).
Clients do not call a recovery endpoint; they resync and see the outcome in the
durable log:

- `session.status` `{"status": "idle", "reason": "the daemon restarted while the turn was running", "turn_id": "trn_…"}`
  for each recovered session.
- Ordinary `message.added` events for a restored assistant message (a response
  that had completed), for the one tool message closing its calls, and for the
  new `[Nexus recovery: interrupted turn]` user notice. The notice is new
  context; earlier messages are unchanged.
- `question.replied` with `outcome: "cancelled"` and `permission.replied` with
  `outcome: "cancelled"` for every wait the restart lost. The old ids cannot
  resume the turn: a question reply gets 409, an approval 404.
- One `turn.recovered` per turn with `{turn_id, session_id, outcome,
  reason, legacy, origin, continuation, request, assistant_message, tool_message,
  tools, waits, losses, partial_output, notice}`. `outcome` is `interrupted`, or
  `completed` when the turn's `ended` record proved the loop finished before the crash
  (then `notice` is null). `legacy` is true for a pre-execution-record session.
  `origin` is `prompt` or `continuation` when recorded (null for an older turn),
  and `continuation` is the predecessor link a continuation crashed with.
  `partial_output` is `{"diagnostic_only": true, "counts": …, "parts": …,
  "tool_calls": …}` for an interrupted stream, or null. `tools[].outcome`
  is `restored`, `not_run`, `approval_pending`,
  `approval_denied`, `approval_cancelled`, `question_answered`,
  `question_cancelled`, `unknown`,
  `unknown_code` or `legacy_unknown`; `losses` reports `{kernels, bash_jobs}`.
- One `turn.summary` per closed turn, after `turn.recovered`, equal to its
  `GET /api/session/{id}/turn` entry.

Partial provider output (deltas and completed parts of an interrupted stream)
stays in the event log but is **not** inserted into the conversation, because
an interrupted response cannot be replayed without pretending it completed.
A restored response comes from parts that were durable before the crash. A
completed tool result is preserved and inserted; a started call with no result
is labelled unknown and is never re-run. A session saved by an older version
with a trailing tool call and no execution records gets a legacy
`[Nexus recovery: legacy interrupted turn]` notice and unknown results; it is
never claimed to have not run. Recovery itself is repeatable: a crash
during recovery does not duplicate a result, notice, cancellation or
`turn.recovered` event.

## A turn, end to end

1. Subscribe: `GET /api/event?session=ses_…` (with `?cursor=` / `Last-Event-ID`
   when reconnecting) and wait for `server.connected`.
2. `POST /api/session/ses_…/prompt {"text": "…"}` → 202. To resume the stored
   context without a new user message, `POST …/continue` instead and follow the
   same events; a continuation has no user `message.added` before `step_start`.
3. Events: `session.status busy` → `message.added` (user) → `step_start` →
   `wire_request` → `wire_event`… with `text_delta`/`tool_call_started`… →
   `part_done`… → `usage` → `done` → `message.added` (assistant) → `tool_start`
   → (`permission.asked` → client replies) → `tool_end` → `message.added`
   (tool) → `step_start` … → `turn_end` → `turn.summary` → `session.status idle`.
4. `POST …/interrupt` at any point cancels. A tool call left without a result
   gets an error result ("interrupted while running" or "before it ran"), so the
   next request stays valid. A pending question is closed with
   `question.replied`/`outcome: "cancelled"`; its answer can no longer arrive.

```sh
TOKEN=$(jq -r .token ~/.nexus/daemon.json)
curl -N "http://127.0.0.1:4747/api/event?token=$TOKEN"           # watch everything
curl -H "authorization: Bearer $TOKEN" -X POST -d '{"path":"."}' http://127.0.0.1:4747/api/project
```

## Not yet

Automatic continuation after a crash is not performed: recovery closes the
turn, and manual continuation is an explicit request. External Git effects are
not transactional; an ambiguous worktree operation must be inspected rather
than repeated. Worktree merge, integration and branch cleanup do not exist.
A session's directory can move anywhere the user can reach; it is not confined
to its project.

Provider sign-in, project files and diffs, and steer/queue are implemented
(the tables above). Retention, idempotent mutation keys, bounded provider
retries, the consistent snapshot and client reconnect/resync are implemented too
([Mutations, retries, continuation and reconnect](#mutations-retries-continuation-and-reconnect)).

## Switches, files and jobs

Kernel execution and metadata limits: [tools.md](tools.md#code). `code.call` and
`code.cell` are stored session events; `kernel.deleted` announces kernel removal.

Question requests are in memory like approvals, but their durable
`question.asked` records let startup recovery close a lost wait with an explicit
`question.replied`/`outcome: "cancelled"`. A turn does not survive a crash
itself; recovery marks it interrupted and never resumes it (Step 11). Their
schema, limits and cancellation behavior are in
[tools.md](tools.md#ask).

Background bash jobs: `bash.started` and `bash.finished` are stored session
events (the finish event carries a clipped output tail). A finish also adds a
notice to the starting session's context. Jobs are killed when their session or
project is deleted, and on daemon shutdown. Limits and lifecycle:
[tools.md](tools.md#background-bash-jobs).

`disabled_tools` on session create or patch is a list of task/control tool names.
Unknown names return 400 with the known names. An empty list re-enables all
agent-granted tools. `tools` reports what the next turn offers after agent
restrictions and switches. Children inherit switches when the field is omitted;
an explicit list on user-created children overrides that inherited setting.

A session freezes its agent file, rendered prompt, tool definitions and
`AGENTS.md` at creation ([agents.md](agents.md#session-snapshots)). Agent-file
and `AGENTS.md` edits therefore affect new sessions only.
`GET /api/session/{id}/context`, `GET /api/session/{id}/snapshot` and the actual
outgoing request all use the stored snapshot; they agree on `model`,
`instructions` and `tools` at any point where the message history is unchanged. A tool switch changes later request schemas
and appends a `[Nexus notice: tool availability changed]` user message; the
frozen system prompt is not rewritten. An agent switch rebuilds the snapshot
from the current files and appends `[Nexus notice: agent changed]`. An already-running call keeps its configuration; explicit changes apply to
subsequent model/tool calls, including within the same turn.

`disabled_tools` accepts task tools, `session`, `message_session`, the three
workspace control names and the two browser tool names. New sessions offer `expose_workspace_tools`; activating it adds
`change_session_directory` and `worktree` to later request schemas and to the
model-visible tool result, never to the frozen prompt. Activation persists with
the snapshot, and a pending directory move is visible in `directory_change`.

A session created before snapshots existed returns `snapshot.origin:
"reconstructed"` after its first use and carries a visible snapshot note. It is
not presented as the exact historical prompt; the reconstruction is stored once
and stays stable. It also starts with `workspace: {entry: false, active: false}`;
activate it explicitly if needed.

### Project files and Git

`GET /api/project/{id}/worktrees` lists the worktrees of the project's own
checkout (`repository`, `main`, `worktrees[]`, `project_directory`) when there
is no session to ask. It is the same shape as the session endpoint and returns
400 outside Git.

`GET /api/project/{id}/files?path=&session=` lists one directory level under
the project directory or the named session's directory. Entries report name,
kind, byte size, modification time, Git status and ignored status. Symlinks are
listed as links and are not followed when opening files or directories. `GET /api/project/{id}/file?path=&session=` returns UTF-8 text,
a SHA-256 content revision and a MIME-based language hint. Reads reject binary
or non-UTF-8 data and files over 1 MiB. Paths are relative to the selected root;
the session must belong to the project. Writes are `PUT /api/project/{id}/file` and publish `files.changed` ([File writes](#file-writes-k10)).

`GET /api/project/{id}/files?path=<directory>&count=true&session=` returns the number of descendant entries without following symlinks for a delete confirmation. Counting stops with an error after 50,000 entries or three seconds; it never presents a partial count as exact.

`POST /api/project/{id}/files?session=` accepts `action` (`create_file`, `create_dir`, `rename`, or `delete`), workspace-relative `path` and, for rename, `to`. Rename refuses an existing destination; deletion is permanent and recursive for directories. Symlink entries and symlink traversal are rejected. Successful operations publish a global `files.changed` event with `source: "user"`; no session timeline receives it. These operations do not belong to task tools.

`GET /api/project/{id}/files/search?q=&limit=&directories=&session=` returns
`{"query", "paths"}`: visible file paths under the selected root (relative,
POSIX, with a trailing `/` for directories when `directories=1` or `true`) whose
path contains `q`, case-insensitively. `q` defaults to `""`, which lists the first
visible paths. Results rank basename prefix matches, then basename matches, then
path matches; shallower paths come first, and directories rank ahead of files of
equal rank. `limit` defaults to 30 and is capped at 100. The walk never opens
files, skips hidden entries and all symlinks, and stops after 50,000 entries.
Inside a Git work tree only paths Git would not ignore are listed, and the list is
cached for 3 seconds. Invalid input returns 400: `q` must be at most 256
characters, contain no NUL or backslash, and not start with `/` or contain a `..`
segment; `limit` must be a non-negative integer.

`GET /api/project/{id}/prompts?q=&limit=&before=` is the prompt history for Up-arrow
recall: `{"prompts": [{"id", "text", "image_count", "session_id", "created"}]}`,
newest first, from every session of the project. Only what the user sent counts
(a prompt, a steer or a queued input once delivered); notices, child results and
sessions' own child prompts do not. A text sent several times appears once, at its
newest use; `image_count` mentions attached images, which are not stored here.
`q` is a case-insensitive substring, `limit` defaults to 50 (1-200) and `before`
is an `id` to page past. Rows live in `prompt_history`, which sessions from before
it existed get once from their stored user messages (notices excluded), and which
goes with the session when it is deleted.

`GET /api/session/{id}/git` returns branch, ahead/behind counts and changed
paths. Each change has `staged` (index column set), `unstaged` (worktree
column set, also true for untracked files), the porcelain v1 `status` code
(`"MM"`, `"A "`, `"??"`, with X as the index and Y as the worktree state) and
tracked line counts. Clients treat `"??"` as untracked.
`GET /api/session/{id}/git/diff?path=` returns separate staged and unstaged
diffs for a relative path. Untracked file contents are not included in diffs.


`GET /api/project/{id}/git` and `/git/live` accept an optional absolute
`worktree` path, defaulting to the checkout containing the project. The path must be a
registered non-bare worktree in the project's repository. Status returns the
porcelain branch header, NUL-safe changed paths and local/remote branch rows (`name`,
`local`, `current`, `upstream`). Live returns `working` and
`idle` session records; active turns, approvals, questions and running jobs
count as working, including children of sessions in the checkout.

`POST /api/project/{id}/git/action` requires `Idempotency-Key`. Its `action` is
`stage` or `unstage` with a nonempty `paths` array, or `commit`, `fetch`,
`pull`, `push`, `switch`, `branch_create`, `branch_delete`, `branch_delete_force`
or `discard`. Commit and branch actions take `value`; discard takes `paths` and
restores tracked worktree contents or deletes regular untracked files without
following symlinks. Force branch deletion additionally requires `force_confirmed: true`. All except stage and unstage require `confirmed_live`,
the IDs of working sessions shown in the confirmation. Core rechecks them
under its worktree mutation lock and returns 409 `worktree_live` with the fresh
lists if any new session is working. This is a pre-operation check, not an
exclusive lock on subsequent session activity or external Git processes. Pull is
fast-forward only and push never forces. When exactly one remote can be proposed
for a branch without an upstream, status includes `push_upstream: {remote, branch}`;
the confirmed push must pass that remote as `value`. Safe branch deletion uses
`-d`, force deletion requires separate confirmation, and Git hooks run on commit. An in-progress merge, rebase or other sequencer operation blocks
mutations. Local commands have a 120-second timeout; network commands get 300
seconds. Git stdout is capped at 16 MiB and stderr at 64 KiB; exceeding either
returns an error. Each change carries `added` and `removed` line counts (staged plus unstaged `git diff --numstat`; 0 for untracked files). Status exposes at most 10,000 changes and 10,000 branches, with
`changes_omitted` and `branches_omitted` counts. Mutations publish global
`git.changed`; switch, pull and discard also publish
`files.changed`. These events do not enter a session timeline. The desktop Git
surface and remaining actions are not yet implemented.

### Worktree review and integration

Control-plane only (no task or control tool reaches it; agent access needs its own approval design, see plan 9.15).
`GET /api/project/{id}/worktree/review?worktree=&base=` reads, never writes. `worktree` must be a registered,
non-main checkout on a branch; `base` defaults to the main checkout's branch and must be checked out in a
worktree. It returns `{worktree, branch, head, base, base_head, base_worktree, merge_base, ahead, behind,
fast_forward, commits (50 newest: sha, author, time, subject), commits_omitted, files (500: path, status,
added, deleted), files_omitted, conflicts (names; null when this Git is older than 2.38 and cannot predict),
blockers, live}`. `blockers` lists why integrating would be refused now: nothing to integrate, uncommitted
changes in either checkout, a Git operation in progress on the base, a predicted conflict. `live` is the
`/git/live` record of both checkouts.

`POST /api/project/{id}/worktree/integrate` requires `Idempotency-Key`:
`{worktree, expected_head, mode: "ff" | "merge", base?, message?, remove?, confirmed_live?}`. `expected_head` is
the full branch tip the user reviewed; a branch that moved since is refused. `ff` is `git merge --ff-only` (a
base that moved on is refused); `merge` is `git merge --no-ff` with `message` or Git's default. Everything is
rechecked under the worktree lock, a conflicting or failing merge is aborted and changes nothing, nothing is
forced, and hooks and Git's own identity rules apply. As for Git actions, a session working in either checkout
that `confirmed_live` does not list gives 409 `worktree_live` with `{live: {worktree, base}}`. With `remove`,
a session, project, pending directory move or background job using the worktree refuses the whole call before
anything is merged; after the merge Nexus removes the clean worktree and runs `git branch -d` in the base
checkout. The reply is `{merged, mode, branch, base, worktree, base_worktree, previous_base_head, head,
branch_head, removed: {worktree, branch, error}}`; if a removal step fails the merge still stands and `error`
says what was left. It publishes global `git.changed` and `files.changed` for the base checkout and
`worktree.integrated`. Archive and delete never touch a worktree.

### Image prompts

`POST /api/session/{id}/prompt` accepts `{text?: string, images?: [{name?: string, data_url: string}]}`.
At least text or one image is required. Up to eight PNG, JPEG, GIF or WebP
images are allowed, at most 8 MiB of decoded bytes combined. Data URLs must use
base64; remote URLs and SVG are rejected. Validation runs before creating the
turn. Images become ordered `image` parts alongside the user text, stored in
SQLite with their bytes for reload, continuation, export and provider replay.
They are not workspace files or ephemeral browser URLs.

### Collapsed text pastes

A client that shows a large paste as `[Pasted ~N tokens]` sends the marker in
`text` and the full content in `pastes`: `[{marker: string, text: string}]`. Core
replaces each marker with a `text` part carrying the full text and
`collapsed: true`, in marker order. Providers receive that text as ordinary
context; clients render the marker instead and show the part in a modal.
Markers must be unique and present in `text`. A prompt allows at most eight
pastes, 1,000,000 characters each and 4,000,000 in total. Ordinary text parts
keep their existing JSON shape; only a collapsed part includes the flag.

The TUI and NSH collapse a paste of at least 500 characters, independent of line
count, so shorter multiline pastes stay inline. Both composers show a preview
row per collapsed paste with **Show** opening the full text; after sending, the
user message shows only the marker chip, which opens the same text. A failed
send restores the collapsed draft.

## Configuration, provider login and context budget

See [Configuration](config.md) for the config revision/patch contract and the
provider login lifecycle. `POST /api/provider/{id}/login` returns 202 for a
browser, device or CLI flow, or 200 for an accepted OpenCode key. `GET` reports
progress; `DELETE` signs out (204). Global `provider.login` events report
progress: `{provider, status, message}`, plus `verification_uri` and
`user_code` while a flow waits (null once it ends), so clients show the link
and code on the provider's row. A browser flow has a `verification_uri` and no
`user_code`. A key is never in an event.

`GET /api/session/{id}/context` retains the provider request fields and adds
`used`, `window` and `exact`. The newest request's measured input is used when
its model matches the current model; otherwise `used` is a character-based
estimate and `exact` is false. A missing catalog window is null. Measured usage
refers to that request, rather than tokenizing newly appended context. Clients
label estimates with `~`. The estimate counts the context-bearing request: the
reasoning effort (`reasoning_effort`, `reasoning.effort` or Claude's `effort`)
is a request option, not context, so changing it does not move `used`.

`POST /api/draft/context` (session drafts, PLAN K4) takes the fields of a
session-creation request (`project` or `directory`, and optionally `agent`,
`model`, `effort`, `disabled_tools`), plans the session exactly as creation
does, and returns the request its first turn would send, with nothing stored:
the `/context` fields (`used`, `window`, `exact`, always false here), `model`,
`agent`, `parts` (`instructions`, `tools`, `instruction_file`, estimated tokens
of the system prompt, tool schemas and the AGENTS.md inside the prompt) and the
`snapshot` summary, `tool_info` (each tool's `description`), `details` (per-section text of the starting context: `system_prompt`, `instruction_file` `{path, status, bytes, content, error}`, `tools` `{name: {description, parameters}}`, `skills` and the fenced `mcp_listing`, exactly what the first request carries), and
the lists the session would start with: `tools` (names, in request order),
`skills` and `mcp` (the session JSON's rows, with `state` and `locked`; nothing the draft's own `disabled_tools` switched off is locked, since the session has not started, so only an unavailable server is), and `session` (`model`, `effort`: what creation would store, configured defaults included; null when unset). Clients show these, not their own defaults, so the screen does not change when the session is created. Enabled
MCP servers are connected first, as for creation. Clients draw the new-session
context from these lists alone: they keep no tool list of their own and show
"loading" until it answers.

## Pending inputs: steer and queue

`POST /api/session/{id}/prompt` accepts `mode: "send" | "steer" | "queue"`.
The default `send` retains its busy-session 409 behavior. `steer` and `queue`
return 202 `{input_id, mode, state: "pending"}` and stay outside model context
until delivery. While the session is idle the daemon starts that input as a turn
immediately; the response is still the pending input, not `{turn_id}`. During a running turn,
they wait. Steers are inserted before the next model request, after all results
from the current tool step. A steer arriving during a final answer causes
another model step. Queued inputs start subsequent turns in order.

`GET /api/session/{id}/input` returns `{inputs, held}`. `PATCH
/api/session/{id}/input/{iid}` edits `text` or promotes `mode` from queue to
steer; `DELETE` removes the input and returns its parts for draft restoration.
`POST .../input/{iid}/move` with `{before: iid}` reorders pending input.
`POST /api/session/{id}/input/next` releases a held queue and sends its next item.
Pending inputs and held sessions are included in `/api/snapshot`.

Interruption holds the queue. Restart preserves pending inputs, converts
undelivered steers into queued inputs, and holds them until explicit release.
A hold only says "do not start queued work by itself": removing the last pending
input clears it, so the next queued message after an interrupt is not stranded.
Both clients react to an interrupt by taking the queue back: they delete every
pending steer and queued message, oldest first, and put the parts in the
composer joined by newlines above the draft, so nothing stays held out of sight.
Archive and deletion refuse pending inputs unless `drop_inputs: true` is given.
`input.added`, `input.updated`, `input.removed`, `input.delivered` and
`input.held` events update client activity. Delivery includes the stored
`message_id` and `turn_id`; the corresponding user message has `delivery:
"steer"` or `"queued"`. Steers delivered at the same boundary share one user
message, whose text joins them with newlines, and their events carry the same
`message_id`. Pending items themselves are not timeline messages.


## Handoff, titles and `/btw`

Titles and handoff make one tool-free model request (`nexus/oneshot.py`); tool specs ride along so the prefix matches
the session's cache, and a tool call in the reply is ignored. Each request and reply is stored as a `oneshot` event
(`kind` `handoff`, `title`), never as model context.

- **Handoff.** `POST …/handoff` or, after a completed turn of a root session whose newest measured request used at
  least `[context] handoff_percent` (default 80, 0 = off) of the model's window, automatically. The new session's
  first message is `[Handoff from session <id>]` plus the summary and has `delivery: "handoff"`. Events:
  `session.handoff {from, to, reason}` on the old session (reason `user` or `auto`), `session.created` and the
  message on the new one, and `session.handoff_failed {error}` when an automatic attempt failed (the session is
  unchanged and the next completed turn tries again). The new session reloads the MCP configuration and connects
  enabled servers like any new root session; see [mcp.md](mcp.md#lifecycle). Session JSON carries `continued_from` (the session whose summary this one starts from; stored on the session row and cleared if that session is deleted) and `continued_to` (the newest session started from this one). The handoff card names the original and `/origin` opens it. `/continuation` has been removed from both clients; manual `/continue` is unchanged.
- **Titles.** The first words of the first prompt are the title at once; a model-written title (Title Case, a few
  words, from the first message only) replaces it unless the user renamed the session meanwhile. The model comes from `[context] title_models`, tried in order; if all fail the first words stay. The `mock/` provider is skipped.

`/btw <question>` opens a live modal in both clients, including mobile. Core copies the parent's settled
request prefix, model, effort and prompt-cache key into a hidden `kind: "btw"` fork, with its own marker,
messages, turn records and inspector history. Its relational `parent_id` is null so it does not make the parent
busy or deliver a report; `fork.parent_id` records the source. Session JSON includes `kind`; clients exclude
btw creation/update events from user-facing session lists. Deleting the source session cleans up its side runs;
project removal/deletion includes them in its pending-work checks and shutdown.

The daemon permits at most 10 tool calls, counting nested code calls and refused attempts. On the tenth it adds
a visible answer-now notice; every subsequent call is refused. Write tools, workspace/control tools, subagents,
forks, messaging, user extensions, interactive questions and MCP tools without an explicit read-only annotation
are refused. Shell execution is limited to a small exact foreground allowlist (`pwd`, `git status`,
`git status --short`, `git diff`, `git diff --stat`, `git log -5 --oneline`); Git optional locks, external diff,
textconv and filesystem-monitor hooks are disabled where applicable. Ordinary configured command hooks do not
run in a btw. Tool definitions remain identical to the parent's for caching, not as an execution grant.

The modal folds the inherited request prefix (`i`) and full ordered side-run context (`c`); expanding context
shows the marker, canonical tool arguments/results, model-visible notices and replies. Compact tool progress,
the `N/10` counter and streamed Markdown answer stay visible when folded. Closing/Esc cancels only the side run
with `POST …/interrupt`. A request token prevents late responses attaching to a different modal; periodic child
checkpoints recover missed SSE events and an idle outcome after reconnect. Past-answer navigation remains deferred.
The parent's messages and timeline never change. Real-provider cache reuse for this new path has not been measured.

Session outcomes (errors, interruptions, retries, recovery and cancelled waits) are displayed above the composer,
not as timeline/footer rows. The non-dismissible strip derives the latest outcome and count from the newest turn
summary or its live scratch, restores on revisit and clears on busy. It offers `/continue` when applicable and the
desktop inspector for full HTTP/body/history details. Model-visible notice/recovery messages and `via Manager`
decisions remain in the timeline; ordinary directory/notification/wait metadata is inspector-only.

## Search, paging, approvals, artifacts, writes, usage (K5–K11)

### Search (K5)

`GET /api/search?q=` matches the sessions' titles, the user's own messages and each turn's final assistant text
(notices and tool output are not indexed) with SQLite FTS5 (`search_index`, kept up to date as messages and titles
change and when a turn closes; final answers for older sessions are backfilled once in bounded slices). Every word of
`q` is a prefix, all must match, and `q` is never FTS syntax. A hit is the session's title when that matches, else
its best-ranked user message or final answer; `snippet` marks the match with `[ ]`. Prompt history
(`/project/{id}/prompts?q=`) stays a plain substring match, which is what Up-arrow recall wants.

### Message paging (K6)

`GET /api/session/{id}/message` returns the whole list as before. With `limit` (1..500, default 100) or `before`
it returns a page, `{messages, more, oldest_seq}`: the `limit` messages before sequence `before` (the newest when
absent), oldest first. Pass `oldest_seq` as the next `before` until `more` is false. `/api/snapshot` still caps its
selected session at `message_limit` and reports `messages_more` and `oldest_seq`, so a client loads older messages
with this endpoint. Turn summaries page with `before=<turn id>` as before.

### Approvals (K7)

`POST /api/session/{id}/permission/{pid}` takes `{reply: "allow" or "deny", scope?: "once" or "session", note?}`.
`scope: "session"` (allow only) also grants the same call for the rest of the session: the tool, for `bash` the
exact command, and for an MCP call the exact `server/tool` target. A deny rule still wins over a grant, and a grant
covers an `ask` rule too. Grants live in memory
(a daemon restart forgets them; `GET`/`DELETE …/grant` shows and clears them). `note` (deny only) is added to the
refusal the model reads: `the user declined this tool call: <note>`. Both show in `permission.replied`.

### Project MCP servers

A project's `.mcp.json` and `.agents/mcp.json` are trusted configuration. Every enabled, valid
entry starts with a new session in that project; there is no approval endpoint. The files are
arbitrary code: opening an untrusted project and starting a session runs their commands
([mcp.md](mcp.md#project-servers)). `GET /api/project/{id}/mcp` returns
`{project_id, file, exists, revision, error, warnings, servers, files, duplicates}` and never
starts a server (it reads the files and may close a changed or removed connection). `revision`
combines the SHA-256 of each file's bytes, or `missing` when neither exists. `files` lists each
file's `name`, `file`, `exists`, `revision`, `error`, `warnings` and parsed `servers`; a duplicate
name also appears in `duplicates` as `{server, used, ignored}`. `.mcp.json` wins a duplicate name.
Each `servers` row has `server`, `state`, `error`, `tools`, `enabled`, `source: "project"`,
`project_id`, `file` (the winning file), `warnings`, optional `duplicate` and the non-secret facts
from `config.describe()`. `state` is `idle`, `connecting`, `connected`, `failed`, `disabled` or
`invalid`; a file error is the special server `(file)` with `state: "invalid"`. Environment and
header values are never returned; a remote URL has credentials, query and fragment removed.

`POST …/restart` reconnects a project entry. `GET …/mcp/{name}` adds `tool_names` and `log`.
Changes are picked up at the next read; a new session uses them ([mcp.md](mcp.md#changes-and-invalidation)).

### Artifacts (K8)

Media a session produced is stored once per content in `~/.nexus/artifacts/<sha256>` with a row per session in
`artifact` (id, media type, name, size, source). Today the source is an MCP `image` or `audio` result: the tool
result names the artifact. An image (PNG, JPEG, GIF, WebP) also reaches the model beside the text, in each dialect's
tool-result item ([llm-providers.md](llm-providers.md#tool-result-media)); for a model without image input the result
says `[N image not sent: …]`; audio stays text only. Media types are
cleaned to `type/subtype`, anything else is `application/octet-stream`; up to 16 MiB each. `GET /api/artifact/{id}`
serves the bytes with `content-disposition: attachment`, `nosniff` and `content-security-policy: sandbox`, so a
server's HTML or SVG never runs. Deleting a session removes its rows and any bytes no row names.

### Cache warning (K9)

`GET /api/session/{id}/switch-cost?model=&agent=` says whether the switch would make the next request start cold:
`reasons` lists why (a different model; a different agent, which replaces the system prompt and tool definitions at
the front of the request), `cached_tokens` is what the newest request read from the cache, and `cache_lost` is true
only when there is a reason and something was cached. Clients ask before `…/model` or `…/agent` and confirm with
the user; the switches themselves are unchanged.

### File writes (K10)

`PUT /api/project/{id}/file?path=&session=` with `{content, expected_revision}` writes a UTF-8 text file (at most 1
MiB) only if it is still at the `revision` a read returned; `"new"` creates a file that must not exist. A stale
revision is 409 `stale_file` with `current_revision`. Paths are opened without following symlinks and cannot leave
the workspace; the write is a temporary file renamed over the target and keeps the file's mode. The event
`files.changed` follows.
The request body may be up to 8 MiB because JSON escaping can expand a 1 MiB file; the content limit itself is
unchanged.

### Usage (K11)

Per-session totals are the `session_usage` ledger (`GET /api/session/{id}/usage`, and in `/turn`). `GET /api/usage`
returns the last saved snapshot without contacting providers (`fetched` is null before the first snapshot). Core
refreshes on startup when due and every `[usage] refresh_minutes` minutes (default 10, range 1–1440); POST
`/api/usage` refreshes on demand. The snapshot survives daemon restarts. Each snapshot has one row per connected
provider. Codex, Copilot, OpenCode, Grok and Claude are asked for their plan windows; a
window is `{label, used_percent, resets_at, period_seconds, kind, detail}`. Providers that expose no plan limits
(OpenAI, Groq, NVIDIA, Ollama) still get a row with a note saying so and no windows. A provider that fails has an
`error` in its row and the others still answer. Nothing in the response is a credential. Both clients open the
usage modal immediately, show the snapshot age, and offer `r` to refresh; closing it does not wait for the response.
The clients hide xAI prepaid credits and Copilot chat, completions and AI-credit details, including in older saved
snapshots; the API retains those details.
The planned usage table shows a ten-cell usage gauge, percent left, reserve and expiry. Window `detail` remains
available to other API consumers but is not shown in the table. Reserve is elapsed period percentage minus used
percentage, calculated by the client only for periods of at least seven days with a future reset and known usage.

Subscription usage is data-driven, not a table of hardcoded quotas by plan name:

- Codex reads `wham/usage`, including individual monthly credit limits at the root,
  within `rate_limit`, or within `spend_control`. Managed accounts with spend controls
  can additionally read their selected account's `spend-controls/current-user/monthly-usage`
  and `remaining_balance`. Optional lookup failures preserve the main windows and add
  an availability note. Individual limits and workspace balances are distinct.
- Claude reads `api.anthropic.com/api/oauth/usage` with the selected CLI profile's
  existing OAuth file when available. The CLI still owns sign-in and refresh; Nexus
  never rotates or persists its tokens, and never reads Keychain through `security`.
  Missing/stale file credentials or rejected authorization fall back to an isolated
  interactive CLI `/usage` probe, with hooks, tools and ambient MCP disabled. The probe
  verifies the usage command palette before pressing Enter; unknown setup fails closed.
  This fallback requires POSIX. Personal, Max, Team and Enterprise response shapes
  include five-hour, weekly, model-specific and spend-only limits. Extra-usage money
  is in minor currency units. A spend-only response is valid even without quota windows.
- Missing utilization or reset times stay unknown, not zero or unlimited. Monthly spend
  endpoints without a reset do not get an invented expiry or reserve. API-key providers'
  note means limits are not exposed by this integration, not that the provider has none.

Both desktop clients consume these same windows and notes without a client-specific
subscription implementation. Monetary amounts remain in API `detail` (the table still
shows percentage left). Account plan labels reflect provider metadata; a reported
`Business` is not silently relabeled `Enterprise`. Protocols were researched against
[CodexBar](https://github.com/steipete/CodexBar). Approved read-only checks on 2026-11-11
returned a Codex monthly credit limit and a Claude Enterprise spend cap through CLI
2.1.322. The CLI fallback accepts both known usage-action captions; it never submits
an unverified command. Date-only CLI spend resets remain unknown until their exact
reset instant can be established. Other subscription types have offline fixture
coverage, not live-account verification. UI screenshots were not captured for this
backend-only change.

### Leased workspace watching

`POST /api/project/{id}/watch?session=` accepts `{client_id, action: "renew" | "release"}`.
It uses the same project/session confinement as Files and returns `{status, lease_seconds: 60, poll_seconds: 3}`.
This ephemeral lease needs no idempotency key: renewing the same client/workspace
extends its expiry, and releasing it is harmless when already absent. Desktop
clients renew every 20 seconds only while Files or the file editor is visible;
closing the surface releases the lease. Mobile never leases a watcher. Core ends
a watcher when all leases expire, on release, or during shutdown. Limits are 32
watched workspaces and 256 client leases.

Native events debounce for 250 ms, skip Git-ignored paths, and watch checkout
metadata plus a linked worktree's common Git directory. A batch publishes at most
256 workspace-relative paths; `omitted` announces further paths and tells clients
to invalidate broadly. No file contents are stored in these global events. If
the native watcher cannot start, `files.watch` announces polling and clients show
**Polling every 3 seconds**. The fallback invalidates Files and Git every three
seconds only while a lease is alive. A disk invalidation reloads clean editor
text; a different disk revision marks unsaved edits stale without replacing them.
