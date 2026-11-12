# Nexus 0.10 promo: plan

Track: `Concrete_Mandate.mp3` (2:57, ~123 BPM, 1 bar = 1.95 s, 2 bars = 3.9 s).
The video uses the first ~1:43, ending on a fade after the logo reveal.
Timestamps were **measured** from the file (`analysis/analyze.py`, `analysis/edges.py`);
the earlier draft's map was off by several seconds.

Render: `hf/` (HyperFrames, 1920×1080, 60 fps; `npm run render`). Every UI on screen is a capture of the real current Nexus driven by simulated data: `captures/tui/` (the TUI in a real Ghostty window) and `captures/nsh/` (the NSH web client). Voice: `tts/` (local Ming-3.0-tts, see `tts/speak.py`).

## Audio map (measured)

| Time | What the music does |
| --- | --- |
| 0:00.0 – 0:13.6 | Dark pad + ticking pulse; hats/filter open from ~0:07.8, brighten 0:12–13.6 |
| 0:13.6 – 0:15.45 | **Drumless breath** (−28 dB) |
| 0:15.45 | **DROP 1**: 4/4 groove, 16 bars, phrase changes every 2 bars (19.35, 23.25, 27.15, 31.05, 34.95, 38.85, 42.75) |
| 0:46.6 – 0:48.3 | **Pre-drop vacuum** (−37 dB) |
| 0:48.3 | **DROP 2**: loudest section, 16 bars (52.2, 56.1, 60.0, 63.9, 67.8, 71.7, 75.6) |
| 1:19.5 – 1:33.0 | **Breakdown**: highs and drums gone, bass pad only |
| 1:33.0 – 1:34.7 | **Silence** |
| 1:34.7 | **Re-entry hit**: logo reveal; music fades out by ~1:43 |

## Storyboard

Facts checked against the repo (docs/, README, PLAN.md). Wording avoids what is
not built: there is no automatic model fallback, no Windows/Linux desktop app,
and no phone-to-Manager voice yet.

| Time | Visual | On-screen text |
| --- | --- | --- |
| 0:00 – 0:04 | Black. A cursor blinks and types `nexus`. | `nexus` |
| 0:04 – 0:07.8 | The TUI (dark theme, Monaspace) assembles cell by cell and tilts into 3D. | — |
| 0:07.8 – 0:13.6 | Low macro flyover: user band, streaming answer, code fence, composer with `╱ 82k · 41%`. | — |
| 0:13.6 – 0:15.45 | Breath: camera settles on the whole window. | *One agentic loop.* |
| 0:15.45 – 0:19.35 | **Drop.** Model picker cycles on the beat: codex, claude, copilot, grok, opencode, openai, groq, nvidia, ollama. | *Any model. Nine providers.* |
| 0:19.35 – 0:23.25 | `orchestrate` fans out background children `scout`, `plan`, `build` in agent colours; an `.md` agent file is saved and the list refreshes (`agents.changed`). | *Parallel sub-agents. Agents are Markdown.* |
| 0:23.25 – 0:27.15 | Approval prompt: allow once · allow for session · deny with a note. | *Real approvals.* |
| 0:27.15 – 0:31.05 | One timeline: the camera travels down a real session in model-context order (thought, tools, answer, table, turn footer). | *What the model sees is what you see.* |
| 0:31.05 – 0:34.95 | Usage: the context meter, the turn footer (`524i · 0o · 0r · 49%c · 36s`) and the composer's window share. | *Every token. Every cent. Live.* |
| 0:34.95 – 0:38.85 | Dictation: red recording square, live partial text streaming into the composer; `/speak` reads a reply aloud. | *Local speech-to-text & text-to-speech.* |
| 0:38.85 – 0:46.6 | Rapid montage, ~1 s per card on the beat: Worktrees → Background jobs → Mermaid → Skills → Files → Diff → Palette → Questions. | kinetic one-word labels |
| 0:46.6 – 0:48.3 | Vacuum: TUI freezes, edges glow, compresses to a point. | — |
| 0:48.3 – 0:56.1 | **Drop 2.** Burst into NSH (Void palette, Google Sans): web, macOS window and iPhone side by side, same session updating live. | *NSH. Web, macOS and iPhone.* |
| 0:56.1 – 0:63.9 | NSH desktop close-ups: file tree with Git letters, diff review, expanding tool calls, command palette. | *TUI and NSH are peers. Every feature, both places.* |
| 0:63.9 – 1:19.5 | **The Manager** (music ducked −9 dB, 1:04–1:18): header orb listening → thinking → speaking; dropdown attention cards across sessions; approval card; the Staging migration session shows the result. | *Meet the Manager. A voice above every session.* |
| | **User:** "Hey Manager, what needs me?" · **Manager:** "One session needs you. Staging migration wants to run the migration script on staging." · **User:** "Approve it." · **Manager:** "Approved. The migration is running on staging." (Matches the real Manager captures: board card → approval → the session result "3 revisions applied to staging".) | captions |
| 1:19.5 – 1:33.0 | Breakdown: everything falls away into near-black; slow typography. | VO: *"What does a software creation interface from the future look like?"* … *"This is Nexus."* |
| 1:33.0 – 1:34.7 | Full black. | — |
| 1:34.7 – 1:43 | The **N** logo, a beam of light sweeping left to right on the hit; wordmark and tagline; fade. | *A provider-agnostic agent harness for your terminal and browser.* |

## Sound notes

- Cut a frame early (≈ −16 ms) on the drops at 15.45 and 48.3 so pictures land with the kick.
- Manager dialogue 1:04–1:18: music duck −9 dB with a 300 ms ramp; back to 0 dB by 1:19.
- VO in the breakdown sits on the bass pad without ducking (no highs to fight).
