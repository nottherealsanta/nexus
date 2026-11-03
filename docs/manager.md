# The Manager

The Manager is a voice agent above all sessions: one per daemon, across every project. It finds, reads, starts,
steers and answers sessions, and it shows things to the user (cards) and can move the user's screen. It is not a
coding agent: it reads no files and runs no commands. [MANAGER_PLAN.md](../MANAGER_PLAN.md) is the design and
the milestone status; this page describes the code as it is.

What is built in Core: the typed Manager (M2), the pipeline-voice server side (M3: `manager.speech` per sentence,
`POST /api/voice/speak` with text, barge-in, activity) and attention with the section 9 guards finished and the
privacy notice (M4). Not built: GPT-Live (M5), hands-free and the clients' stage, orb and audio. `ManagerService.wake()`
remains as a seam but nothing in Core uses it: attention needs no model.

```
nexus/manager/
  service.py    one per daemon: the call, the holder, the turn worker, episodes, world and facts
  turn.py       TurnState (authority), RateLimiter, SentenceSplitter
  context.py    snapshot, delta, the user's view, the recap, data blocks for session text
  tools.py      the 16 tools and their catalog, with the authority checks
  stage.py      card validation (exactly the 10.7 shapes) and the one-card stage
  view.py       the holder's view, navigation handshake, back stack
  retrieval.py  ranking, fuzzy matching, spoken handles
  state.py      the unified session state
  topics.py     lazy one-sentence topic lines
  prompt.py     the system prompt
  attention.py  unprompted lines: bus subscriber, ranked queue, deterministic templates
  consent.py    the once-per-provider privacy notice
  redact.py     secret masking for speech and for text sent to the model
nexus/mock/scenarios/manager_demo.py   the scripted Manager model
scripts/scripted_manager_core.py       a real Core with that model, for client tests and screenshots
```

## Hidden sessions

The Manager is a hidden session: `Session.kind == "manager"`. One row per *episode*, linked by `continued_from`,
all in a hidden project (`Project.hidden`, named "Manager", under `$NEXUS10_HOME/manager`). They give the Manager
the loop, retries, provider auth, the wire log, the usage ledger and crash recovery for free.

They are excluded in `Store`, so a call site that forgets cannot leak one: `sessions()`, `projects()`, `search()`,
`stale_root_sessions()` (and so the idle archive sweep) and `state_snapshot()` return only the user's own
sessions unless asked (`kind="manager"`, `hidden=True`). A stream drops every event of a hidden session unless it
is filtered to that episode's own id (`eventlog.Stream`), so no client's rail ever sees one. The events stay in the
durable log: `GET /api/manager/episode` lists the episode ids, and `/api/session/{id}/event`, `/message` and the
inspector open one by id ("the full log is in the inspector only", D5). `tests/test_manager_store.py` is the leak
test.

`Session` gained `kind`, `topic` and `topic_seq`; `Project` gained `hidden`; `message` gained `origin`. An existing
0.10 database gains the columns in place on open (schema version unchanged, like the payload-storage columns).

## The call

One call at a time. A client opens `GET /api/event`, reads `stream_id` from `server.connected`, and starts the call
with it (`POST /api/manager/start`). It becomes the **holder**. Every route that takes a `stream_id` accepts only the
holder's; anything else is `403 {"code": "not_holder"}` with no identifier echoed. Starting from another client moves
the call: the old holder is locked out, the new `call_id` differs, and the first client's `manager.state` shows
it. Starting again from the same stream returns the same call. If the holder's event stream closes, the call ends
(`manager.state` with `active: false`). Ending a call never stops a session.

Every event carries `call_id` (random, not secret) and never `stream_id`. The Manager's events are ordinary durable
global events, so a reconnecting client resumes them from its cursor and `GET /api/manager` restores the state.
Routes and event fields are in [api.md](api.md#the-manager).

A turn: `say` queues the utterance and returns `202 {turn_id}`. The worker runs one turn at a time:

1. **Admit.** Pick the episode (below). Build the turn input: recap and a full snapshot if the episode is new,
   otherwise a delta; the user's view only if it changed; the user's words last, tagged `typed` or `speech`.
2. **Run.** `Runtime.prompt` on the episode, so it is an ordinary loop turn with the Manager's code-defined prompt
   and tools. `manager.state` goes `thinking`, then `replying` at the first sentence, then `idle`.
3. **Speak.** The model's streamed text becomes `manager.speech`, one sentence at a time (`seq` 0, 1, ... within
   the turn). A completed message without deltas is split the same way.
4. **Fail visibly.** A model that cannot run fails the turn: `manager.state.error = {code, message, fix}` and a
   `manager.caption` with `role: "manager"`. There is **no fallback model** (`[manager] model` is the only model;
   the episode's model is re-synced to the config at each turn). The error stays until a turn succeeds.

`POST /api/manager/cancel` is barge-in: it mutes the turn's remaining speech and drops queued utterances, and
interrupts the turn only if no tool has changed a session yet (`TurnState.acted`); otherwise the turn keeps going
and the response says so.

## Episodes, recap, snapshot and delta (plan section 6)

Within an episode the context only grows. A new episode starts at turn admission (no timers) when the current one
has been idle for `idle_episode_minutes` or has passed `max_episode_tokens` (measured input tokens, else a
character estimate). A new call does not start one.

The recap is written before the old episode closes and is carried as the new episode's first context:
the last four exchanges verbatim, the sessions discussed (handles from the tool calls), the actions taken (from the
audit), items shown to the user that were not answered, and one same-model narrative call. The narrative is
optional: if it fails (`ManagerService.narrator` is injectable), the deterministic parts are used and the rollover
proceeds. Session states are not carried; the snapshot is fresher.

The first turn of an episode gets a state snapshot (projects, needs the user, running, finished in 24 h, five rows
each, "and N more" when clipped). Later turns get only what changed since the previous turn
("auth-refactor: working -> needs approval"). If the daemon restarted mid-episode there is no baseline, so a fresh
snapshot is sent (append-only, so still valid). The user's view arrives as one sentence in `<user_view>`, only when
it changed, together with "The user closed the board card you showed" after a card was dismissed.

Session titles and words are always quoted or wrapped in `<session_data>` with tag-like text neutralised, and the
prompt says they are data. A turn started with no user words says so in its `<user_view>` block.

## Tools

Defined in code (`tools.definitions()`); no agent file, skill, MCP server, hook or extension can add to them or to
the prompt. Parameters never include a session id: sessions are *handles* (`auth-refactor`), persisted in
`manager_handle` and stable for the session's life, or a `row` number on the card now on the stage. A raw id is
refused.

| Group | Tools |
| --- | --- |
| read | `overview`, `find_sessions`, `session_status`, `read_session` (`last_answer`, `last_user`, `recent`, `question`, `changes`), `search_conversations`, `current_view` |
| act | `start_session` (`worktree=true` writes the worktree instruction into the first message), `send_to_session` (un-archives as needed), `answer_question`, `decide_approval`, `interrupt_session`, `archive_session`, `unarchive_session` |
| see and show | `navigate`, `show`, `clear_stage` |

There is no tool for files, shell, web, MCP, settings, models, permissions, deletion, worktree deletion or merging.

### Authority (plan section 9), enforced in code

* **Authority comes from the user's words only.** `TurnState.authority` is true only for sources `typed` and
  `speech` (the only sources `POST /api/manager/say` accepts). A turn started by `wake()` has none: every act tool
  and every `show`/`navigate` refuses with a message telling the Manager to inform and ask. Refusals are audited.
* **Session text is data**, wrapped and neutralised; a title cannot close the block it sits in or forge a handle.
* **An approval or a question is decided only if** it names a pending id on that session; it was returned by a read
  or `show` at an earlier utterance and the user has spoken since (`presented`, per call); the request is unchanged
  since (a fingerprint of the tool and arguments); for an allow, the user's own words contain a clear yes and no
  negation; and when another approval of the same kind that was shown is still waiting, the words name the
  session (a bare "yes" gets "which one?"). One decision per turn; `scope` is always `once`; there is no
  "all".
* **Every act is on the record.** One `manager_audit` row (heard words, tool, target, result, ok), a
  `manager.acted` event in the target's own timeline, and `origin = "manager"` on the message or the reply.
* **Rate caps** per turn (starts 2, sends 5, interrupts 3, archives 3, one decision, 8 acts) and 12 acts a minute.
* **Honesty.** A refused or failed act is returned to the model as a tool error with the reason; the prompt and the
  scripted model speak it as a failure. Clipped results say how much was cut.

Known limit, by design: authority is "the user spoke", not "the user asked for this". A typed or transcribed
utterance authorises ordinary acts (send, start, interrupt, archive), so a model steered by injected session text
could still send a message or start a session in a turn where the user said something unrelated. Those acts are
rate-capped, recorded with the heard words, and labelled `via Manager` in the target; approvals and question answers
have the stricter guards above. The red-team evals (`tests/test_manager_redteam.py`) pin exactly this boundary.
Not built: checking what a voice model's own words did (M5).

## Attention (unprompted lines)

`attention.py` subscribes to the bus while a call runs and keeps a ranked queue; it calls no model.

| Priority | Item | When it is said |
| --- | --- | --- |
| 1 | an approval or a question is waiting | about a second after it appears (coalescing); also between two sentences of the Manager's own reply |
| 2 | a session failed (not an interruption); a child escalated | at the next pause |
| 3 | a root session finished | only at `proactive = "all"`, after 8 s of no user activity, coalesced ("Three sessions finished.") |
| 4 | started, working, progress | never |

Rules, all enforced and tested: nothing while the client reports `listening` (the microphone is open); while the
client is still playing speech only priority 1; while the Manager is replying only priority 1 and only right after
a sentence (`on_sentence_break`), with `urgent: true`; at most one line per `proactive_interval_seconds`; a barge-in
(`cancel`) holds everything for one interval; items answered before they are said are dropped; the Manager's own
episodes never raise items; a focused card is never replaced.

Lines are fixed templates ("The auth refactor wants to run a command. Allow it?", "Two things need you: ...", "The
docs pass stopped with an error.") and are masked like all speech. A priority-1 line also puts an **unfocused**
`approval` or `question` card on the stage (one card at a time, so the first of several). The line and the card
count as *presented* for the approval guards, but a decision still needs a user utterance after them and everything
else in the authority list. Coalesced lines name at most three items; items counted only as "more" are not marked
presented and remain queued for a later line. A line is not a turn: it has no user words, so it cannot act, and attention never
navigates. On joining a call the Manager opens with what is already pending (approvals, questions, blocked
sessions) in one such line, and with nothing when nothing is.

The Manager's context records it: each line is an audit row (`proactive`) and is added to the next turn's input
("Unprompted, Nexus said aloud: ... and put an unfocused approval card for ... on the stage"), once. The model is
woken only by the user's reply, which is an ordinary turn.

## Privacy notice and redaction

The Manager reads any session, so session text reaches the provider that runs it. `consent.py` stores an
acknowledgement per provider with a notice version; `GET /api/manager` carries it (`consent`), `POST
/api/manager/consent` records or revokes it, and `start` and `say` are refused with `409 consent_required` until
it is given (the offline `mock` provider is exempt). `redact.py` masks secret-shaped text (API keys, tokens, bearer
values, private keys, `password = ...` and quoted JSON-style credential keys) in session text before the model sees it, in every `manager.speech`
sentence, and in `POST /api/voice/speak` text. It is a pattern pass, not a guarantee, and the notice says so.

## Speech (pipeline voice, server side)

`manager.speech` events leave as the model writes: a sentence is published the moment its end is seen, not when the
turn finishes (`tests/test_manager_voice.py`). A client fetches each sentence from `POST /api/voice/speak {stream_id,
text}` and plays the streamed WAV in order. `POST /api/manager/cancel` mutes the rest of the turn's speech, drops
queued utterances, stops the sentence being synthesised, interrupts the turn only if nothing has changed a session
yet, and holds attention for an interval. `POST /api/manager/activity` is stored (`listening`, `speaking`) and read by
attention. The thinking cue is client-only: Core's `phase` is enough.

### `origin = "manager"`

Messages the Manager sends (`start_session`'s first message, `send_to_session`) are stored with `origin =
"manager"` (`message.origin`; queued and steered inputs carry it through `pending_input.origin`). It appears as
`"origin": "manager"` on the message object in `message.added` events, `GET /api/session/{id}/message` (both forms),
`/api/snapshot` selected messages and `input.*` items. Approval and question replies the Manager made carry
`origin: "manager"` and `heard: "<the user's words>"` on `permission.replied` and `question.replied`. Interrupts,
archive changes and failed acts are announced by a session-scoped `manager.acted` event
`{tool, origin, ok, heard, result}`. A message with no `origin` key was typed by a person at a client. A client
labels `via Manager` from these.

## Finding "that session" (plan section 7.1)

`retrieval.Retriever.find_sessions`. What the code does, with the changes the offline eval forced:

* **Index.** Titles, user messages, final answers and topic lines (FTS kinds `title`, `user`, `final_answer`,
  `topic`), plus a fuzzy pass over titles, topics, project names and a real `Session.branch` (inert: Store has no
  branch yet) using edit distance, trigrams and a small phonetic key, so "tara form" finds "Terraform".
* **Prefix noise.** `Store.search` matches every word as a prefix, so "over" finds "overflow". The snippet's
  highlighted words are read back: a token that matched only as a much longer word earns 0.35 of a match.
* **Query.** Spoken commands are dropped only at the start ("find the ...", "search for ..."), then stop words.
  A word that names a known project ("the bug in notebooks") boosts that project's sessions and is not searched for
  in the text; a best match in another project is shown but never offered as a confident choice.
* **Rank.** text score x `(0.85 + 0.15 x half-life decay)` x `(1 + boosts)`: needs attention, on screen (session or
  project), discussed this episode, project in focus, project named. Recency is a tie-break: the plan's plain
  half-life decay buried an exact match from last week under a loose one from today (changed; ledger).
* **Window.** A query searches all history. The 3-day default window applies to browsing (no words), and widens
  to 30 days and then all time when it finds nothing, with a note. `since` is a hard window. Archived sessions are
  included by default.
* **Ties are questions.** Candidates within 0.85 of the best by how well the words fit (boosts count, recency does
  not) make `ambiguity` and `choice` is `None`. A best match whose text score is under 0.55 is `weak` and also has
  no `choice`. Acting on `choice` is therefore always unambiguous and reasonably strong.
* **Topic lines** (`topics.py`) are one sentence per session from the Manager's model, written in the background
  after a turn finishes and only when the Manager is enabled and the topic moved (the session's newest words share
  under 25% with the topic and title). `find_sessions` only *schedules* sessions that lack one (three at a time),
  so a spoken query never waits on a model call. The writer is injectable (`Topics.writer`).

`state.session_state()` is the unified state (`needs_approval`, `needs_answer`, `blocked`, `failed`, `working`,
`archived`, `finished_unread`, `idle`; `unknown` when a source is missing). The Manager computes facts for roots by
rolling their whole tree up, as the clients do for rail counts. Core has no unread source, so the Manager reports a
settled session as `idle` and never produces `finished_unread`. Rail counts are computed in the clients from the
snapshot; Core has none to switch, and switching them to `session_state` would need the clients' unread set, so
they are unchanged.

## Cards and the stage

`stage.py` builds the seven cards of plan 10.4 in exactly the shapes of 10.7. The Manager's `show` tool names what
to show; Core fills the facts from the source: an `approval` card is the pending request's own command or arguments,
a `question` card its real question and option labels, an `excerpt` is cut from the session's stored text, `board`
and `sessions` rows come from the retriever and the unified state. Only `diagram` and `note` carry the Manager's
own text. One card at a time (`show` replaces, `clear_stage` removes). Bounds: a board column shows 4 rows and
`more`; a sessions card 8; an approval detail 4000 characters (`clipped`); an excerpt 1200; a note 1500; a
diagram over 4000 characters is refused, not cut. `n` numbers rows across the whole card and `show`/`navigate`
accept `row` so "the second one" is the row the user saw even if the world changed since.
`POST /api/manager/card/dismiss` removes the card; the next turn tells the Manager.

## Moving the user's screen

`navigate` sends `manager.navigate` to the holder only and waits up to 5 s for `POST /api/manager/navigate/result`.
It returns success only after `ok: true`; a refusal carries the client's `reason`; silence is a timeout failure,
so the Manager never says "opened it" for a move that did not happen. A confirmed move closes the card
(`manager.card` with `card: null`). `navigate(back=true)` goes back: Core keeps a stack of the views the user was in
before each Manager move (their last `POST /api/client/view`), sends `back: true` and the resolved target fields
(`project`, `session`, `panel`), and a client may use either its own history or those fields. Needs a user
utterance; a wake never moves the screen. Opening Settings is not available (plan Q4).

## Config

`[manager]` in `config.toml` ([config.md](config.md#manager-settings)): `enabled` (default false), `mode`,
`model` (default `copilot/gpt-6-luna`), `idle_episode_minutes`, `max_episode_tokens`, `proactive`, `approvals`,
`talk`. There is no `stage` key.

## The CLI

`nexus manager` is a typed chat: it holds the call as client `cli`, prints sentences and cards (`render_card`),
refuses navigation ("the CLI has no screen"), and stops the call on exit. `nexus manager "one line"` sends one
line and exits. `nexus manager episodes` lists the past conversations (open one in the inspector by id);
`nexus manager audit` lists what the Manager did on the user's words.

## Driving a scripted Manager (tests and screenshots)

The model `mock/manager_demo` is a scripted model that reads the user's words and calls the real tools, so cards,
navigation and `via Manager` come from the real code path. The offline `mock` provider needs `NEXUS10_DEV=1` at
import (the scripts set it).

* **A running core for screenshots:** `cd nexus-core && NEXUS10_HOME=$(mktemp -d) NEXUS_PORT=4813 uv run python
  scripts/scripted_manager_core.py`. It refuses to run without a scratch `NEXUS10_HOME`, writes `daemon.json` like
  `nexus daemon start`, enables `[manager]` with the scripted model, and seeds two projects and sessions in every
  state (an approval, a question, a running turn, finished and archived). The script's docstring lists the phrases:
  `what's going on?` (board), `list sidebar` (sessions), `approval`, `question`, `yes`, `diagram`, `note`,
  `excerpt sidebar-polish`, `open the second one`, `go back`, `start fix the cue in nexus`, `what am i looking at`,
  `clear`.
* **In a Core test:** `tests/manager_support.py` (`Harness`) builds a real runtime and app with the scripted model
  and a fake event stream: `open_stream()`, `start()`, `say()`, `answer_navigation()`, `report_view()`, `card()`,
  `spoken()`. `tests/manager_seed.py` seeds sessions.
* **Your own script:** subclass nothing; add a `Scenario` to `nexus/mock/scenarios/` and set `[manager] model =
  "mock/<name>"`.

## Evals

`tests/test_manager_eval.py` is the offline retrieval eval of plan section 11 (50 seeded sessions in five projects
over 20 days, 100 phrasings: exact, paraphrase, mangled speech-to-text, project-qualified, "should ask" and "no
match"; `tests/manager_eval_data.py`). It prints top-1 and false confidence, with and without topic lines, and
fails past a bound. The authority, stage and view, and honesty evals are `tests/test_manager_authority.py` and
`tests/test_manager_stage.py`; the leak test is `tests/test_manager_store.py`. All run offline with the scripted
model. `tests/test_manager_attention.py`, `test_manager_consent.py`, `test_manager_voice.py` and
`test_manager_redteam.py` cover attention, the privacy notice, speech and the red-team evals (authority, injection,
mishearing, the conversation seam, correction, interruption during a tool call). A real-model run and the
spoken-quality sample are not done: `scripts/spikes/pipeline_latency.py` and `luna_tools.py` are written for you to
run against real services (they use a scratch home, print what they will spend, and need `--yes`); they have not been
run.

## GPT-Live voice (M5; experimental, not verified against the real service)

`[manager] mode = "live"` swaps the pipeline for GPT-Live over the ChatGPT (Codex) sign-in, model `gpt-live-1-codex`,
with **client delegation**: OpenAI runs only the voice, the Manager stays the one brain. **Core owns the whole call**,
so every client (TUI, NSH on every target, iOS) only captures the microphone and plays speech. Code:
`nexus/manager/live/`: `protocol.py` (wire shapes) and `transport.py` (signaling POST and sideband socket), ported from
old Nexus `nexus/live/`; `media.py` (Core's WebRTC peer on `aiortc`, Opus both ways); `voice.py` (the glue and the client
audio socket); `prompt.py` (the voice's instructions, with the three required headings).

```
client mic --PCM16 24 kHz mono--> Core --Opus/WebRTC--> OpenAI
client speaker <--PCM16 24 kHz--- Core <--Opus/WebRTC--- OpenAI      sideband WebSocket: Core <-> OpenAI
```

Flow. After `POST /api/manager/start` returns `mode: "live"`, the holder calls `POST /api/manager/live/start
{stream_id}`: Core makes its own SDP offer, signs and posts it with the Codex sign-in, opens the sideband, applies the
answer, and returns `{call_id, audio}` (the audio format). The client then opens the WebSocket `GET
/api/manager/live/audio?stream_id=&token=` (close codes 4401 bad token, 4403 not the holder, 4404 no call, 4409 replaced
by another client): **binary frames are raw PCM16 little-endian mono at 24 kHz, 20 ms or any length, both ways**; text frames `mute`
and `unmute` (client to Core) silence the microphone. A client that drops its socket does not end the call. Errors from
`live/start` are 409: `live_mode_off`, `consent_required` (a separate notice for `codex`, `POST /api/manager/consent
{accept, version, provider: "codex"}`), `live_busy`, `live_unsupported`, or a `live_*` code from the service.

- **Authority.** `session.delegation.created` carries no request. The Manager hears the *user's* `input_transcript` since the last hand-off as a
  `speech` utterance; the delegation's own text and everything the voice said are ignored. A delegation with no user
  speech is not sent to the Manager; the voice is told to ask again.
- **Replies.** Each Manager sentence goes to the voice as a `say` append on that delegation (the voice paraphrases
  it); attention lines go with no delegation. `manager.speech` is **not** published in this mode (the client must not
  also synthesize); the same sentence is a `manager.caption`, so the stage looks the same. Appends are 500 UTF-8 bytes,
  redacted, never replayed after a failed send.
- **Two wire routes.** `protocol.outbound(kind, ...)` maps `say`/`quiet`/`instruct` to the Codex route
  (`delegation.context.append` with channel `speakable` = said, `commentary` = quiet; no instruction event) or the public API
  (`session.commentary|thinking|instructions.append`). Only `codex` is wired. The public docs name `commentary` as the spoken one,
  the Codex source names `speakable`: the mapping is the one place to fix after the first real call.
- **Failure.** A lost or closed sideband, a lost peer or a fatal error ends the voice but not the Manager call:
  `manager.state.error` says so and the typed field still works. Barge-in (`/api/manager/cancel`) also tells the voice to stop speaking.
- **Echo.** Browsers cancel echo. The TUI has none: it holds the microphone while speech plays unless
  `NEXUS_LIVE_HEADPHONES=1`. Real echo cancellation is M6.

Clients. NSH: `nexus-nsh/src/core/gptlive.ts` (AudioWorklet capture, Web Audio playback; web only, since native NSH has
no `getUserMedia`), wired in `core/manager.ts`; the web host (`scripts/serve.mjs`) proxies the audio WebSocket and adds the
token. TUI: `nexus-tui/src/live.rs` (cpal in, rodio out, `tokio-tungstenite`). Both show a failed start as an error on the
open call and keep the typed field. The talk shortcut only interrupts. iOS needs only the same mic and speaker loop over this
socket (not built). Tested with fake transports and peers, plus two real `aiortc` peers exchanging audio through Opus
in-process; never with a real call, real devices or real echo.
