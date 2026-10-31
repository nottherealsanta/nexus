# TODO

Things that are not blocking the 0.10.0 release. [PLAN.md](PLAN.md) holds the release steps.

## 1. Explain the 14.7K vs ~3.9K token gap

In the real TUI, a Claude Haiku 5.5 session (2026-10-30) answered "hi there" and the turn footer read 14.7Ki input
(47% cached) while the starting-context view said `total ~3,965 tok` and the composer meter 14.7K · 14.7%. The context
meter should match what the model receives, so find where the other ~10K comes from. Candidates, in order of cheapness to check:

- The daemon was started before the worker's empty working directory and the `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  change, so it ran the older provider code. Restart the daemon and repeat the turn.
- Claude Code 2.1.314 (the user's own) adds more than 2.1.285 did (about 175 tokens in a scripted turn; see
  [docs/llm-providers.md](docs/llm-providers.md#claude-agent-sdk)). Capture its request with a local server as in that section.
- The 15 tool schemas plus the `StructuredOutput` schema and the framing sentence. The estimate counts characters ÷ 4 and may
  undercount JSON schemas.
- A user-level Claude Code setting or memory the CLI still loads despite `setting_sources=[]`.

Whatever it is, show it in the starting context or remove it.

## Platform matrix (O5)

Deferred from the release gate. Cells to fill, each with install, `nexus doctor`, daemon start, TUI against the daemon,
NSH web, a scripted turn and a Monty kernel run:

| Platform | Status |
| --- | --- |
| macOS arm64 | development machine; run the full list once from a clean `$NEXUS10_HOME` |
| macOS x86_64 | not run |
| Linux x86_64 glibc | Core and TUI pass on Debian 13 (2026-10-30: 767 passed; TUI release build works with `pkg-config` and `libasound2-dev`); NSH, `install.sh`, a live daemon + TUI session and kernels remaining |
| Linux aarch64 glibc | supported: the release builds a binary on `ubuntu-22.04-arm`; run the same checks as x86_64 |
| Alpine/musl | not supported: `install.sh` rejects it with a clear message |

Linux needs, in the installer's checks and in `docs/development.md`: `pkg-config`, ALSA headers (`libasound2-dev` /
`alsa-lib-devel` / `alsa-lib-dev`), a C toolchain, `git`, `uv`, Rust, Node 22 and `nsh`. Release TUI binaries are built on Ubuntu
22.04 (glibc 2.35). The Monty kernel launcher uses `/usr/bin/env`; confirm the path on Linux (the forced worker crashes and cold
start are not covered).
  
## 2. on installed system nexus should launch the tui ( right now when another system installs nexus - they have todo nexus-tui )  
