# Roadmap (proposed)

No phase is started except P0. Order follows risk: contract and security before UI.

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| **P0** | These planning docs | Reviewed; no code |
| **P1** | Approve host + security contract. Then: pair, list sessions across authorized workspaces, create or open a session, open a structured live transcript, send, approve (first slice). Session creation (`SessionStart`) is included (D17 in [DECISIONS](DECISIONS.md)) | See acceptance below |
| **P2** | Richer control: stop/queue/steer, context, tools, diffs, subagents, archived scope, model/effort | Parity with browser read paths that the allowlist permits |
| **P3** | Dictation (binary upload route, host transcription) | Mic permission, limits, spool deletion verified on device |
| **P4** | Experimental GPT Live | Real-service and device matrix run; honest background UX |
| **P5** | iPad polish, accessibility | Dynamic Type, VoiceOver, keyboard, dark mode audits pass |

## P1 prerequisites (all must land before Swift work)

1. Gateway design reviewed ([GATEWAY](GATEWAY.md), [SECURITY](SECURITY.md)).
2. Host presentation commands/stream over UDS using the existing Python projection
   ([CONTRACT_GAPS §1](CONTRACT_GAPS.md#1-presentation-contract-is-not-remotely-reachable)).
3. Versioned cursor spec: `base_seq`, patch `base_seq → seq`, resync, replay retention
   ([§3](CONTRACT_GAPS.md#3-sequence-numbers-are-monotonic-not-contiguous)).
4. Python fixtures exported for Swift (snapshots, patches including `append`).
5. Tailscale deployment validated on the real host.
6. Identity/cert plan and pairing crypto decisions.

## Acceptance criteria (cumulative)

- **Device auth:** unpaired or revoked devices cannot call anything; revoke closes
  open streams immediately; expired/replayed/wrong-audience tokens fail.
- **Negative security tests** pass ([TESTING](TESTING.md)): denied commands, other
  workspace ids, oversize bodies, malformed patches, pairing secret reuse.
- **No lost or duplicate events**, judged by applying patches to the same final
  state as a fresh snapshot — **not** by seq continuity (seq has gaps by design).
- **Cross-client approvals:** approving on phone resolves for desktop and vice
  versa; "answered elsewhere" shown correctly; first responder wins.
- **No secret leakage:** tokens, pairing secrets, keys, provider credentials absent
  from logs, crash output, cache, screenshots, pasteboard, errors.
- **Background honesty:** the UI never implies live updates, notifications or
  voice when the app is suspended; reconnect always refetches.
- **Truncation and clipping** are visible everywhere they occur.
- **Docs:** each shipped behavior updates the matching repo doc in the same
  commit, per [docs/README.md](../../docs/README.md#keeping-docs-true); moving
  gateway contracts into `docs/` (and `AGENTS.md`/module map) happens then, not now.
