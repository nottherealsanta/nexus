# Turn summary: TUI load measurement


One seeded session in a throwaway home (never `~/.nexus`): 40 turns, 160
messages, 30 616 events (11 600 `text_delta`, 3 200 `reasoning_delta`,
14 800 `wire_event`, 84 `wire_request` carrying the conversation, 80 `usage`,
tool, retry and lifecycle events; every tenth turn retried, every seventh tool
failed), with the same turns in the state tables. Event `data` is 4.56 MB;
`nexus.db` is 8.1 MB. Commands, from `nexus-tui/`:

```sh
cargo build --release
tools/bench.sh <scratch>/bench 40 3     # seeds <scratch>/bench/home once (tools/bench_seed.py),
                                        # starts its Core, runs nexus-tui --bench 160x48 three times
```

`--bench` opens the most recent session and reports the time until its stored
history is applied, the time to the first full frame (history plus one
`render_buffer`), RSS from `ps` after that frame, and `render_buffer` time over
400 frames while scrolling a half page per frame (200 up, 200 down). Frame time
is building and painting the frame into a buffer, not terminal output. Warm
runs (the first run after seeding is cold: 690 ms before):

| | Before (event history, 20 000-event cap) | After (turn summaries) |
| --- | --- | --- |
| Open to history applied | 230–250 ms | 68–83 ms |
| Open to first full frame | 237–258 ms | 73–88 ms |
| RSS after the first frame | 133 MB (7.7 MB before open) | 19 MB (7.8 MB before open) |
| Frame time while scrolling, mean / p95 | 4.4–4.7 / 4.6–5.4 ms | 3.6–3.7 / 3.8 ms |
| Bytes read on open (event pages / `/turn`) | 7.97 MB of events + 139 KB messages | 59 KB of summaries + 139 KB messages |

Before, the session also lost 10 617 events to the cap (`… older diagnostics
are not loaded`), so its sidebar counted 5.2M input tokens; the ledger counts
all 5.9M.

## Render replay (phases 1–2, 2026-10-31)

`--bench WxH --replay` is a deterministic in-memory stream through the same
`frames::run_batch` policy and timeline cache as the interactive loop. It needs
no Core, pty or home state beyond a valid `NEXUS10_HOME`, and reports the first
frame, timeline build time, burst and paced frame counts, draw/reducer times,
cache hits/misses/evictions and RSS. `NEXUS_TUI_REPLAY_MESSAGES` (default 200)
sets the loaded history; `NEXUS_TUI_REPLAY_EVENTS` (default 900) sets the
stream. Every message carries ~1.2 KB of Markdown and a code fence, and the
stream is half invisible `wire_event`s, half text/reasoning deltas plus a final
message and summary. `NEXUS_TUI_REPLAY_TOOLS=1` adds an `edit` call with an
80/90-line old/new pair and a 120-line result every tenth assistant message, at
high detail.

```sh
cargo build --release
mkdir -p <scratch>/replay-home && touch <scratch>/replay-home/.nexus10-home
NEXUS10_HOME=<scratch>/replay-home NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 \
  target/release/nexus-tui --dark --bench 160x48 --replay
NEXUS_TUI_REPLAY_MESSAGES=1000 ...          # heavy history
NEXUS_TUI_REPLAY_TOOLS=1 ...                # long diffs, high detail
```

Measured on an Apple M4, macOS 27.0, arm64, 160×48, base commit `8622de5` plus
the phase 2 change, release build, three light and two heavy runs. Phase 1
numbers are the same run before the layout cache:

| | 200 messages | 1,000 messages |
| --- | --- | --- |
| Paced draw, phase 1 mean / p95 | 8.4–8.7 / 9.1–9.7 ms | 164–166 / 178–237 ms |
| Paced draw, phase 2 mean / p95 / max | **1.13–1.17** / 1.21–1.37 / 6.4–6.7 ms | **5.15–5.23** / 5.89–5.97 / 31.5–31.9 ms |
| Burst draw, phase 2 mean / p95 | 1.20–1.26 / 1.32–1.48 ms | 4.99–5.23 / 5.58–5.97 ms |
| Timeline build, phase 2 | 1.46–1.58 ms | 7.3–8.5 ms |
| First frame, phase 2 | 7.5–10.0 ms | 33.8–34.1 ms |
| Paced applied rate, phase 2 | 1,615–1,678 events/s | 363–368 events/s |
| CPU user / sys, phase 2 | 0.58–0.60 s / 4–6 ms | 2.64–2.65 s / 15–18 ms |
| RSS, phase 2 | 25–26 MB | 53–55 MB |

With long tool diffs at high detail (`NEXUS_TUI_REPLAY_TOOLS=1`), release draws
cost 2.0 ms at 200 messages and 9.4–9.9 ms at 1,000; RSS is ~32 MB / ~309 MB,
the latter the allocator's high-water from 100 diffs' rows per frame (the cache
itself holds 2–15 MB). Debug builds are 9.3 ms (200) and 32 ms (1,000), about
8× release.

Representative raw output (release, light then heavy):

```text
replay messages 200 events 902 invisible_events 428
replay_first_frame_ms 7.48
timeline_build_ms 1.46 (width 85)
burst frames 16 applied 902 total_ms 20.4 applied_per_s 44235
burst_draw_ms mean 1.23 p95 1.32 max 1.32 (n=16)
paced frames 475 applied 902 total_ms 552.3 applied_per_s 1633
paced_draw_ms mean 1.16 p95 1.33 max 6.66 (n=475)
paced_cache hits 95264 misses 672 evictions 0 entries 672 bytes 1325301
cpu_ms user 593 sys 4
rss_kib 26320

replay messages 1000 events 902 invisible_events 428
replay_first_frame_ms 34.05
timeline_build_ms 8.54 (width 85)
burst frames 16 applied 902 total_ms 83.9 applied_per_s 10747
burst_draw_ms mean 5.19 p95 5.79 max 5.79 (n=16)
paced frames 475 applied 902 total_ms 2449.0 applied_per_s 368
paced_draw_ms mean 5.15 p95 5.89 max 31.86 (n=475)
paced_cache hits 474464 misses 1472 evictions 0 entries 1472 bytes 4205302
cpu_ms user 2635 sys 15
rss_kib 53168
```

Both provisional targets are met: under 2 ms for realistic 200-message draws
and 5–15 ms for the 1,000-message case. Batching cuts 902 events to 16 burst
frames; the cache makes unchanged bodies and tool rows cheap to re-assemble.
Residual cost is copying and painting all loaded lines. The replay does not
measure terminal output or input latency; the scratch-Core pty run in the
section above remains the end-to-end check.
