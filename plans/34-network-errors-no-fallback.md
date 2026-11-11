# 34. Network errors: subagents wait, never fall back

User request (2026-11-11): if the error is a network problem, a subagent must **not** switch to its fallback model.
It should wait until the network is back and continue on the same model.

## What happened on 2026-11-11 (16:20–16:23 local)

Read from `~/.nexus/nexus.db` (read-only). One network outage of about 2½ minutes: the first failed request at
16:20:45, the last at 16:23:04. Every provider failed at once (`opencode`, `codex`, `copilot`), so the problem was
the machine's connection, not one provider.

Sessions hit: two roots, `ses_1a1ca1139300279081d` (orchestrate, "Composer Navigation and Tab UI Fixes") and
`ses_1a1ca0dae2820c0c528` (build, "Fix Terminal Toggle Shortcut"), and their five `build` children.

What each child did:

1. Primary `opencode/deepseek-v4.1-flash`: `ConnectTimeout` / `ConnectError` / `ReadError`, 2–3 attempts, then
   `retry_exhausted`. Some gave up after **2 of 3 attempts**: each `ConnectTimeout` takes several seconds, so the
   60 s `max_elapsed` ran out first.
2. `model_fallback` → `codex/gpt-6.1-terra`: the same network errors (`ConnectError`, `ConnectTimeout`, `ReadError`,
   `RemoteProtocolError`), exhausted again.
3. `model_fallback` → `copilot/gpt-6.1-terra`: the same, exhausted again.
4. With no fallbacks left: `session.error {kind: "provider", message: "copilot: network error: ConnectError"}`, and the
   turn failed after 69–140 steps of work. The parent was told "Sub agent … failed".

The roots failed too. When the build root was woken by the child's failure notice at 16:23:00, it retried three times
in 2.5 seconds and failed with `codex: network error: ConnectError`. The user then interrupted what was left
(16:25–16:26).

**Second problem, after the network came back.** The fallback had **permanently changed the children's model**:
their session rows now say `copilot/gpt-6.1-terra`. When they were resumed (16:26:57–16:27:32), three of them failed
with `copilot: HTTP 400 model_not_supported`. So a 2-minute outage turned into failures that outlasted it.

Error text comes from `nexus/llmhub/responses.py:294` and `chat.py:250`: any `httpx.HTTPError` becomes
`ProviderError("<provider>: network error: <ExceptionName>", phase="transport")` with no status. The exception
names seen were `ConnectError`, `ConnectTimeout`, `ReadError` and `RemoteProtocolError`. All of them occurred before
any response event (the loop only retries then).

## Code today

- `nexus-core/nexus/loop.py` ~334–352: when `_stream_with_retry()` raises `ProviderError`, `AuthError` or
  `CredentialError` before any response stream event, and the child has `fallback_models`, the loop pops the
  next model, emits `ModelFallback`, adds the `[Nexus notice: model fallback]` message and carries on with that model.
  **The kind of error doesn't matter**, so a dropped Wi-Fi, a DNS failure or a timeout switches models just like a
  real provider rejection.
- Retries before that (`nexus/retry.py`, `RetryPolicy`): 3 attempts, 30 s max wait, **60 s max elapsed** by default
  (`NEXUS_RETRY_*`). `retryable()` accepts the retryable phases with `status is None` (no HTTP response, i.e. a network
  failure) or a transient status. After exhaustion: `RetryExhausted`, then the fallback above.
- Fallbacks come from `[agents] fallbacks` per child agent, frozen into the child's snapshot as `model_fallbacks`
  (`runtime.py` ~1221–1256, ~2299). Docs: `docs/loop.md` ~35 and ~72, `docs/agents.md`.

## Plan

- [ ] **Classify network failures** in one place (`retry.py`): `ProviderError` with `phase == "transport"` and no
  status, whose cause is an `httpx.TransportError`. That covers `ConnectError`, `ConnectTimeout`, `ReadError`,
  `ReadTimeout`, `WriteError`, `PoolTimeout` and `RemoteProtocolError`, which is everything seen on 11-11. Keep the
  exception name on the error (`network_kind`) rather than parsing the message text. Not network: any HTTP response
  (4xx, 5xx, 429), auth or credential errors, and malformed or ended streams. Ask the user: should 502/503/504 count as
  network? Default: no, because the connection itself worked.
- [ ] **Wait instead of exhausting:** for a network failure, the retry loop drops `max_attempts` and `max_elapsed`
  and keeps retrying **the same model** with capped backoff (1 s, 2 s, 4 s … up to 30 s, with jitter) until it
  succeeds or the session is interrupted or cancelled. Interrupt stops the wait at once. Any non-network error
  during the wait goes back to the normal policy.
- [ ] **No fallback on network errors:** in `loop.py` ~342 a network `ProviderError` never pops a fallback model.
  (After the change above it only ends by interrupt, but keep the guard explicit and tested.)
- [ ] **Roots too:** a root session gets the same wait, because both roots failed on 11-11. A parent woken by a child
  notice waits the same way. Ask the user to confirm; default yes.
- [ ] **A fallback must not permanently change the session's model** (the 16:27 HTTP 400s). Check what
  `ModelFallback` writes to the session row and snapshot. Default: a fallback lasts for that turn only; the next
  turn or a resume starts on the primary model again. If the model has to stay switched for cache reasons, show it and
  let the user switch back. Ask the user which. Separately, `copilot/gpt-6.1-terra` returned `model_not_supported`,
  so the child's fallback list contained a model this account can't use. Validate fallbacks against the live catalog
  when the child starts.
- [ ] **Visible state:** while waiting, show "Waiting for network · retrying in Ns" (new `waiting_network` phase on
  `RetryScheduled`) in the session status strip ([21](done/21-session-status.md)) and on the parent's child card, in both
  clients. Today's `retry_scheduled` events don't say they are network waits. Nothing is added to model context.
  Inspector Timing shows the waiting span.
- [ ] Parent `wait: true` / child-notice paths: a child that is waiting is still running, not failed. Make sure no
  parent-side timeout reports it as failed.
- [ ] Later: on a connectivity change, retry at once instead of waiting out the backoff.
- [ ] **Tests:** replay the 11-11 sequence with a fake provider raising `httpx.ConnectTimeout` / `ConnectError` /
  `ReadError` / `RemoteProtocolError` for longer than the old 60 s budget (fake clock). Expect no `ModelFallback`, no
  `retry_exhausted`, success on the same model once the fake recovers, and an unchanged session model. HTTP 500/401
  with fallbacks still falls back. Interrupt during the wait stops promptly. A partial stream followed by a network
  error is unchanged (no retry after response events). Covers both a root and a child.
- [ ] **Docs:** `docs/loop.md` (retry and fallback), `docs/agents.md` (fallbacks), `docs/stream-events.md` (new phase),
  `docs/api.md` if `retry_scheduled` changes; porting-ledger row.
