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
