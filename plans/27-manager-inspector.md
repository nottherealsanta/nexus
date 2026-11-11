# 27. Manager inspector

User request (2026-11-11): build a Manager inspector so the user can debug how the Manager works.

The Manager is the one exception to the timeline rule ([docs/manager.md](../docs/manager.md)): its own context has
no timeline. Debugging it today means switching between several partial views:

| Exists today | What it shows |
| --- | --- |
| Header dropdown / focused panel | Current caption, current model context, latest tool (bounded, current state only) |
| `manager.context` modal | Episodes, recap, state snapshots/deltas, recent log |
| **Open Manager session** | The current backend session, read-only, as an ordinary timeline |
| Session inspector | Full raw wire log of a hidden Manager session, by id |
| `nexus manager episodes` (CLI) | List of past episodes; open one in the inspector by id |

## Goal

One **Manager inspector** that answers: *what did the Manager hear, what did it know, what did it decide, what
did it do, and what did the user see?*, for each Manager turn, live and after the fact.

## Plan

- [ ] **Design pass first** (short doc + mock in both clients): a turn list on the left (time, trigger: user speech /
  typed / attention / proactive, outcome), and for the selected turn these tabs:
  - **Input:** the transcript (STT text, with confidence if available) or typed text; for GPT-Live, the input events.
  - **Context:** the exact request: system prompt, recap, the state snapshot and the delta given this turn, the
    episode boundary. Diff against the previous turn's snapshot.
  - **Decisions:** tool calls with arguments and results, the authority check for each (allowed / refused and why,
    `docs/manager.md` "Authority"), retrieval candidates and scores for "that session" lookups.
  - **Output:** spoken text, cards shown (exactly what the card displayed), navigation performed, `via Manager`
    messages sent into other sessions (links to them).
  - **Timing:** STT → model → TTS (pipeline) or the GPT-Live event timeline, first-audio latency, interruptions.
  - **Raw:** the wire log for that turn (existing inspector records).
- [ ] **Core:** an inspect endpoint for the Manager (`/api/manager/inspect?episode=&cursor=`) built on the
  existing inspect API v2 (cursors, bounds, `omitted`), reading the hidden sessions' state tables. Record what is
  missing today, prospectively: authority decisions, retrieval candidates, per-stage voice timings. Nothing is
  reconstructed or guessed; unknown stays unknown.
- [ ] **Where it opens:** from the Manager dropdown ("Inspect"), the palette (`manager.inspect`), and the existing
  inspector's session selector (a "Manager" entry). Desktop/web only, both clients in the same change (TUI
  `inspector.rs`, NSH `ui/inspector.tsx` and the web inspector page). Not on mobile.
- [ ] **Privacy:** reuse the Manager's redaction (`manager/redact.py`); raw audio is never stored by the inspector.
- [ ] Live updates through the existing SSE invalidation; episode list paged.
- [ ] Tests: Core inspect tests with the scripted Manager (`docs/manager.md` "Driving a scripted Manager"), a shared
  scenario in `interface/scenarios/`, client tests, matching screenshots.
- [ ] Docs: `docs/manager.md`, `docs/inspector.md`, `docs/api.md`, both READMEs. The dropdown's "Open Manager
  session" stays; decide with the user whether the context modal is folded into the inspector afterwards.
