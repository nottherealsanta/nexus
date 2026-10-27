# Fixtures

Shared JSON/SSE fixtures for reducer, transport and supervisor tests.

- `real/` captured from a real daemon (`nexus daemon start` in an isolated
  `NEXUS10_HOME`, no provider call): `info.json`, `snapshot.json`, `replay.sse`
  (`GET /api/event?cursor=<generation>:0`), `cursor_generation.json`,
  `cursor_ahead.json`, `revision_conflict.json`. Absolute paths are rewritten to
  `/work/...`. The log generation is `log_c915bfc9b6d826c4`; the snapshot head is 3.
- `documented/` hand-written from `docs/api.md` and `docs/stream-events.md`,
  continuing that log after id 3: a full turn with an approval and a question
  (ids 4-16, with gaps), session/project lifecycle and tombstones (20-23), and a
  `bus.overflow` frame. They are the shapes the docs promise, not captures.
