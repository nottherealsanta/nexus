# Changelog

## [0.7.0](https://github.com/nottherealsanta/nexus/compare/v0.6.1...v0.7.0) (2026-10-23)


### Features

* add explicit safe session worktree cleanup ([80a48fc](https://github.com/nottherealsanta/nexus/commit/80a48fcde3c0f8ef0674a9660a78d1f1e474f149))
* add simple native session worktree modes and saved turn review ([67df53a](https://github.com/nottherealsanta/nexus/commit/67df53a2f42882b0631b7358958e8c7a9ca89b88))
* **agents:** save isolated child work and return it to sessions ([a610fc4](https://github.com/nottherealsanta/nexus/commit/a610fc4066155678d25b43ad5f97480aba3a8b63))
* **host:** share one daemon across projects ([e753e20](https://github.com/nottherealsanta/nexus/commit/e753e208eec29c63959a1584ac8a303646d96b95))
* **manager:** compact idle manager context with machine memory and a state snapshot ([d4af491](https://github.com/nottherealsanta/nexus/commit/d4af4911e7e0d4765a48f33c6dfc1182285efc8b))
* **manager:** launch sessions in worktrees by default ([b47196e](https://github.com/nottherealsanta/nexus/commit/b47196e2b88e5fd52c861d444ae33c35199288b4))
* **session:** capture hidden checkpoints and expose turn diffs ([19dc207](https://github.com/nottherealsanta/nexus/commit/19dc20762d741fcee8ff40bab5bb3c7481e95a5a))
* **session:** confirm local integration of saved session work ([abe3cad](https://github.com/nottherealsanta/nexus/commit/abe3cadc8f36e495db4ea8377c7e2a5d0d91eae0))
* **session:** create owned worktrees and turn commits ([b10f6c6](https://github.com/nottherealsanta/nexus/commit/b10f6c6c51652a454bc848c42feed1306601d27c))
* **session:** isolate per-session working roots ([9585b1f](https://github.com/nottherealsanta/nexus/commit/9585b1f9e8f2a90e8bad23c0975490d527783985))
* **tui:** always show where the session runs in the top bar ([500e2a7](https://github.com/nottherealsanta/nexus/commit/500e2a711ffaa7a2f68b99887d6df84ab6f1d8f9))
* **voice:** ship local dictation and speech as base dependencies ([647eb23](https://github.com/nottherealsanta/nexus/commit/647eb2345ff3efb40268a507e6f25d292d9c1c52))
* **voice:** ship local dictation and speech as base dependencies ([65224df](https://github.com/nottherealsanta/nexus/commit/65224df976e1ebd731b0e61a25d0089cc02c8ab2))
* **worktrees:** name session worktrees at the first message ([a3855ea](https://github.com/nottherealsanta/nexus/commit/a3855ea85a487a7979ef2128f78ac93f19cdec8d))


### Bug fixes

* **codex:** omit temperature and top_p for the Codex backend ([8913a96](https://github.com/nottherealsanta/nexus/commit/8913a96d43ad6545bb71aac73d0e57adb3873d50))
* repair the 28 failing tests and cap the handoff suffix to the room left ([0a31c51](https://github.com/nottherealsanta/nexus/commit/0a31c515bd320bb2065be5c8cd210bd34add538d))
* save unreported turn edits with existing worktree safeguards ([4afebb0](https://github.com/nottherealsanta/nexus/commit/4afebb0ab991d921c1bd73aed693320334e0e798))
* skip saved subagent worktree records during session recovery ([60d70ba](https://github.com/nottherealsanta/nexus/commit/60d70ba52f5d6bf57a43da82fb7081b3eef19107))

## [0.6.1](https://github.com/nottherealsanta/nexus/compare/v0.6.0...v0.6.1) (2026-10-22)


### Features

* add Bark voice model, subagent continuation, and related updates ([edbdcaf](https://github.com/nottherealsanta/nexus/commit/edbdcaf2ebf5845deca94727bbdbf98d27fc9221))
* add live settings page, narrow details drawer, and subagent rows to the TUI ([09bd639](https://github.com/nottherealsanta/nexus/commit/09bd639939e282edf676b82124f23f870b9dfd1f))
* mirror the manager session in the desktop client ([1edba88](https://github.com/nottherealsanta/nexus/commit/1edba88b70ce58d6f4c0920b7d1704ed2e357446))
* run subagents in the background with persistent children, reports, and usage routing ([217d47b](https://github.com/nottherealsanta/nexus/commit/217d47b83a4b1cc83d228ef026731a825119f664))
* ship manager session, settings installs, and subagent roster controls ([1f4a3d5](https://github.com/nottherealsanta/nexus/commit/1f4a3d557d590e274066fe601ae00f108b3b498e))
* subagents back to blocking ([17cd8f4](https://github.com/nottherealsanta/nexus/commit/17cd8f41d581a1710a040717ab274622a3e5ad52))


### Bug fixes

* release 0.6.1 as a patch ([64bc1bc](https://github.com/nottherealsanta/nexus/commit/64bc1bc1c5953c969d2c836f0c8e550c10e61e23))

## [0.6.0](https://github.com/nottherealsanta/nexus/compare/v0.5.2...v0.6.0) (2026-10-19)


* request 0.6.0 release ([693ff5a](https://github.com/nottherealsanta/nexus/commit/693ff5aa14a6aa27d7a8b02b23a51c23e603dea1))


### Features

* add Antigravity workers, device gateway, and provider usage controls ([0aca0c4](https://github.com/nottherealsanta/nexus/commit/0aca0c403f30e7c348c1bf16670ed5a38cada2a5))
* add Claude Code subagent backend switch and harden the worker ([7c7006a](https://github.com/nottherealsanta/nexus/commit/7c7006aaf8c62f442ac273e33552f3522599e24b))
* add manager orchestration and code-mode surfaces ([#88](https://github.com/nottherealsanta/nexus/issues/88)) ([db6e5dc](https://github.com/nottherealsanta/nexus/commit/db6e5dc661d388fe90566d1968594a227f09a338))
* add xAI (Grok) provider over the SuperGrok subscription ([0919e4c](https://github.com/nottherealsanta/nexus/commit/0919e4cc38d44c5c0e6f0dd2007feb8f7625c927))
* **agents:** remove worker wall-clock deadlines and add session planning docs ([a53607e](https://github.com/nottherealsanta/nexus/commit/a53607e21d74aef0926fc5c62a30087a3a0556ff))
* antigravity worker updates and troubleshooting doc ([a019366](https://github.com/nottherealsanta/nexus/commit/a0193669c1d4bb7ca2f8d05c65e64cd2de4dc5cd))
* Codex-style GPT Live handoff with transcript context and speakable replies ([148e356](https://github.com/nottherealsanta/nexus/commit/148e356d6ad33c302caa262ec42047d244ecb304))
* expandable terminal Live pane above the composer ([c29d98d](https://github.com/nottherealsanta/nexus/commit/c29d98d9d1d60049f351857c5855f2c0de0f0b43))
* Grok-style native TUI with sessions dashboard and settings modal ([82bdf52](https://github.com/nottherealsanta/nexus/commit/82bdf52232a4257fdb9a798ba6a1975e8c335f97))
* hot-apply tiers, client project sessions and session DB changes ([0db032f](https://github.com/nottherealsanta/nexus/commit/0db032f60520bed4c643d240534d3f27dcd55fae))
* improve provider, worker, and terminal behavior ([b8cbd7f](https://github.com/nottherealsanta/nexus/commit/b8cbd7fc44ffa2a5110e54ebf3b8ab6e6dc0c3d1))
* improve session dashboard ordering and live controls ([4443a9d](https://github.com/nottherealsanta/nexus/commit/4443a9dead6d1fda4b724de6b0620d078bd4d43d))
* **manager:** add opt-in session orchestration across native surfaces ([0e2055d](https://github.com/nottherealsanta/nexus/commit/0e2055dd04ab638de98a1885eba19885e1e3fabb))
* merge Antigravity, gateway, and provider usage updates ([2ddfd33](https://github.com/nottherealsanta/nexus/commit/2ddfd33347e4a2d24007d0671ec7183aa06b3f12))
* one-line session rows and finished-subagent diamonds ([37e5033](https://github.com/nottherealsanta/nexus/commit/37e5033544ec1822b4ee1cd8a88d794d34e8148f))
* pause and resume GPT Live calls with Ctrl+X P ([493a394](https://github.com/nottherealsanta/nexus/commit/493a394ec2290736a23a0c8f0b88fd500c8bd4f5))
* polish native Ratatui terminal experience ([b646f42](https://github.com/nottherealsanta/nexus/commit/b646f42ef606b20c5502e94558d1a254ac7d0992))
* record heard text, source and verbatim in Live delegation ([af3d9a4](https://github.com/nottherealsanta/nexus/commit/af3d9a4d7624ed3fe84fd7f4b7842f2689a6a1a4))
* show turns waiting for a free slot under the global limit ([0bbcf2c](https://github.com/nottherealsanta/nexus/commit/0bbcf2c67d1f86b1cab8eaa2e9ba0519a1b62de8))
* sticky user message while scrolling and top bar spacing ([323e700](https://github.com/nottherealsanta/nexus/commit/323e7006e6e7b0a6bbeed9c30f3e11c6e6ded9df))
* support tool-result images on OpenAI wire and refine Ratatui TUI ([28c1da4](https://github.com/nottherealsanta/nexus/commit/28c1da491d398820e5a27b02d20e7d49c90a1c25))
* time-grouped sessions dashboard with search and keyboard preview ([01ab9e7](https://github.com/nottherealsanta/nexus/commit/01ab9e77d3a272fcd50ea3c3a768ee6abae43226))
* unified sessions, session trees, and context handoffs ([0787104](https://github.com/nottherealsanta/nexus/commit/0787104fcd42036fb9c2966e53af80377c239c75))


### Bug fixes

* banded user cards and a pill row that never covers content ([e19e940](https://github.com/nottherealsanta/nexus/commit/e19e940351e63a8a68b69849d81f54430d9d3bd8))
* clicking a context header line opens its inspector ([5a9d528](https://github.com/nottherealsanta/nexus/commit/5a9d528084d703faa51cefbb8132917cea2bffc3))
* Codex-style GPT Live sideband reconnect without client pings ([87f08a4](https://github.com/nottherealsanta/nexus/commit/87f08a4f7906413ffd9120a17e47d1c1a4844c8f))
* drop read-only role enforcement and update UI projections ([54265ed](https://github.com/nottherealsanta/nexus/commit/54265ed919e90dbbeeab87b02d4e14063aa69460))
* drop the blank row between the composer and the activity meter ([0d5d1fb](https://github.com/nottherealsanta/nexus/commit/0d5d1fbcec32a7f740fe651fbcc0e45ad65d9b51))
* keep GPT Live calls alive past 35s by upgrading webrtc to 0.17.2 ([453ab89](https://github.com/nottherealsanta/nexus/commit/453ab89ddabe28234233c35d5151dc203a0b50ff))
* keep trailing tool groups running until their turn closes ([ec66fce](https://github.com/nottherealsanta/nexus/commit/ec66fceda3faa0c764dd22d3a14273631f1ac8c5))
* report an empty Antigravity SUCCESS as needs-attention, not completed ([fc504cc](https://github.com/nottherealsanta/nexus/commit/fc504ccae96bb34939853bbb48fcd7a607a5eb62))
* sentence-case tool names and purple accents on sessions and settings ([fdacf5b](https://github.com/nottherealsanta/nexus/commit/fdacf5b76ae7820ae7d2ca54b90b1d698718098a))
* simpler top bar, details sidebar on Sessions, context click opens inspector ([81d1451](https://github.com/nottherealsanta/nexus/commit/81d1451730dc89a27eb873a7ae427600a68f6215))
* stabilize offline tests and target release 0.5.4 ([#86](https://github.com/nottherealsanta/nexus/issues/86)) ([7567151](https://github.com/nottherealsanta/nexus/commit/7567151f1621c76ba3bc8fe5c413ac783dc6dc69))
* stabilize PTY snapshots and active tool groups ([f50cb2a](https://github.com/nottherealsanta/nexus/commit/f50cb2a5b8097cde5549b97a85e8f247c87c2daf))
* surface agy denials and --mode fallback; close ERROR tool rows ([b6acf5f](https://github.com/nottherealsanta/nexus/commit/b6acf5f122c3dcbe711d0637c9c97bccb7bf8c0a))
* trigger patch release 0.5.3 ([8a69304](https://github.com/nottherealsanta/nexus/commit/8a6930478c9f3f058c5e876dccbc773b42fa1321))
* **tui:** finish overhaul rendering and startup polish ([b2f2c77](https://github.com/nottherealsanta/nexus/commit/b2f2c775d2d087c19a6103364117b78e3e7a3289))
* user card without prompt glyph, time in turn details, stable turn numbers ([ad7d555](https://github.com/nottherealsanta/nexus/commit/ad7d5557046e729295679cb7074bbe397db71d90))

## [0.5.2](https://github.com/nottherealsanta/nexus/compare/v0.5.1...v0.5.2) (2026-10-13)


### Bug fixes

* bundle live audio helper and dependencies by default ([#80](https://github.com/nottherealsanta/nexus/issues/80)) ([44faceb](https://github.com/nottherealsanta/nexus/commit/44faceb517fc48d27ca0e769b1058b20d0155102))

## [0.5.1](https://github.com/nottherealsanta/nexus/compare/v0.5.0...v0.5.1) (2026-10-13)


### Bug fixes

* harden Claude Code workers and polish client rendering ([#78](https://github.com/nottherealsanta/nexus/issues/78)) ([502242f](https://github.com/nottherealsanta/nexus/commit/502242fad5947c2726c8c01971de9d4df93deec0))

## [0.5.0](https://github.com/nottherealsanta/nexus/compare/v0.4.2...v0.5.0) (2026-10-13)


### Features

* add GPT Live voice calls ([34c2ccc](https://github.com/nottherealsanta/nexus/commit/34c2ccc1e8f7dbf8f95f3535a52879cf117b3c4d))

## [0.4.2](https://github.com/nottherealsanta/nexus/compare/v0.4.0...v0.4.2) (2026-10-08)


### Features

* run shell commands from the composer with `!command`; output joins the context without starting a turn, and Stop kills the run ([ea7ed98](https://github.com/nottherealsanta/nexus/commit/ea7ed98e102106c2d3d13785c2ad3521149565cf))


### Bug fixes

* build the desktop client again ([ea7ed98](https://github.com/nottherealsanta/nexus/commit/ea7ed98e102106c2d3d13785c2ad3521149565cf))
* cap bash and `!` output at 2,000 lines / 50 KiB, spilling the rest to a file ([ea7ed98](https://github.com/nottherealsanta/nexus/commit/ea7ed98e102106c2d3d13785c2ad3521149565cf))
* collapse large pastes into one [Pasted ~N tokens] marker in the composer and send the full text on submit ([a225adb](https://github.com/nottherealsanta/nexus/commit/a225adb6768f47e74ccb7ede9ea708120b4b700b))
* list a message's attachments as highlighted chips below its text, where clicking an image chip opens that image ([a225adb](https://github.com/nottherealsanta/nexus/commit/a225adb6768f47e74ccb7ede9ea708120b4b700b))
* play the turn completion cue once instead of also ringing the bell ([589404f](https://github.com/nottherealsanta/nexus/commit/589404f7ffc8dd13edcd2fe34c545b62a76ed243))
* quit the native client on a Ctrl+C double tap without arming it when Ctrl+C only clears the draft ([ea7ed98](https://github.com/nottherealsanta/nexus/commit/ea7ed98e102106c2d3d13785c2ad3521149565cf))
* recall prompts up to 1M chars in history within a 16 MiB file budget ([d1fd604](https://github.com/nottherealsanta/nexus/commit/d1fd6049429807d03fb3be1fd469da32a55ea4c3))
* render pasted images with the terminal's graphics protocol and open each attachment chip ([#75](https://github.com/nottherealsanta/nexus/issues/75)) ([a225adb](https://github.com/nottherealsanta/nexus/commit/a225adb6768f47e74ccb7ede9ea708120b4b700b))
* show image previews as real images in Kitty, Ghostty, WezTerm, iTerm2 and Sixel terminals, detected once at startup; set NEXUS_IMAGE_PROTOCOL to force a protocol ([a225adb](https://github.com/nottherealsanta/nexus/commit/a225adb6768f47e74ccb7ede9ea708120b4b700b))
* show long user messages in full instead of clipping at 8192 chars ([fc1433e](https://github.com/nottherealsanta/nexus/commit/fc1433e19af12a8d7929b06a173fda63bcac6b79))
* show MCP config dialect errors, invalid entries and unreadable files as labelled rows instead of failing silently ([ea7ed98](https://github.com/nottherealsanta/nexus/commit/ea7ed98e102106c2d3d13785c2ad3521149565cf))
* start file-change rows closed in the Ratatui transcript ([b33eb96](https://github.com/nottherealsanta/nexus/commit/b33eb964b25da6432c7b1231409f932bab3da137))

## [0.4.0](https://github.com/nottherealsanta/nexus/compare/v0.3.7...v0.4.0) (2026-10-08)


### Features

* web/TUI parity, uninstall, desktop settings and Paradee speech ([#71](https://github.com/nottherealsanta/nexus/issues/71)) ([0b9032b](https://github.com/nottherealsanta/nexus/commit/0b9032bb18256a73cd7799055764a00ff2d1f58c))

## [0.3.7](https://github.com/nottherealsanta/nexus/compare/v0.3.6...v0.3.7) (2026-10-06)


### Features

* extend Ratatui context and host tools ([6b10816](https://github.com/nottherealsanta/nexus/commit/6b10816208ee902f160501949ed3e19182285109))


### Bug fixes

* force 0.3.7 release ([c44b5d8](https://github.com/nottherealsanta/nexus/commit/c44b5d8261fa23328bcb25aaf045a005a52af66c))

## [0.3.6](https://github.com/nottherealsanta/nexus/compare/v0.3.5...v0.3.6) (2026-10-06)


### Bug fixes

* force a live PyPI lookup when updating ([f7d46d0](https://github.com/nottherealsanta/nexus/commit/f7d46d0134138d9a583ce3ee749ee502bb3f4132))
* remove daemon token and pid files before the socket on shutdown ([#68](https://github.com/nottherealsanta/nexus/issues/68)) ([fb2f210](https://github.com/nottherealsanta/nexus/commit/fb2f210d5cbf644e27f8e37f386febfae078bf59))

## [0.3.5](https://github.com/nottherealsanta/nexus/compare/v0.3.4...v0.3.5) (2026-10-05)


### Bug fixes

* restore MCP settings toggles and startup diagnostics ([#65](https://github.com/nottherealsanta/nexus/issues/65)) ([6ca0b52](https://github.com/nottherealsanta/nexus/commit/6ca0b52307d7371a8be68673b38ea925172483da))

## [0.3.4](https://github.com/nottherealsanta/nexus/compare/v0.3.3...v0.3.4) (2026-10-05)


### Bug fixes

* use _request for default-model client calls ([#63](https://github.com/nottherealsanta/nexus/issues/63)) ([5e0240f](https://github.com/nottherealsanta/nexus/commit/5e0240f4a66de2e828161e4ec9806476ea50404a))

## [0.3.3](https://github.com/nottherealsanta/nexus/compare/v0.3.2...v0.3.3) (2026-10-05)


### Bug fixes

* settings revamp, run-ordered models and agent run modes ([#61](https://github.com/nottherealsanta/nexus/issues/61)) ([e837a50](https://github.com/nottherealsanta/nexus/commit/e837a5097835934c7a3c48df34ffd1475f30b5c1))

## [0.3.2](https://github.com/nottherealsanta/nexus/compare/v0.3.1...v0.3.2) (2026-10-05)


### Features

* **mcp:** search-based MCP tool loading ([#59](https://github.com/nottherealsanta/nexus/issues/59)) ([208a7e2](https://github.com/nottherealsanta/nexus/commit/208a7e223aa36574ce872e222cf7a9e9007a431c))


### Bug fixes

* consolidate native clients and session improvements ([#58](https://github.com/nottherealsanta/nexus/issues/58)) ([22a2038](https://github.com/nottherealsanta/nexus/commit/22a2038f478b7b8ccf1990eaa11c887cf41b7fe8))

## [0.3.1](https://github.com/nottherealsanta/nexus/compare/v0.3.0...v0.3.1) (2026-10-02)


### Bug fixes

* refine native terminal layout and preserve agent recovery context ([#56](https://github.com/nottherealsanta/nexus/issues/56)) ([35c63b7](https://github.com/nottherealsanta/nexus/commit/35c63b7455be9beb98b823ac993dad96732e5923))

## [0.3.0](https://github.com/nottherealsanta/nexus/compare/v0.2.17...v0.3.0) (2026-10-02)


### Features

* add the native Ratatui terminal client and make it the default chat renderer ([#52](https://github.com/nottherealsanta/nexus/issues/52)) ([1a5b693](https://github.com/nottherealsanta/nexus/commit/1a5b693dcd57ff2eea60cd57ff5d1a8eea9f98ca))

## [0.2.17](https://github.com/nottherealsanta/nexus/compare/v0.2.16...v0.2.17) (2026-10-02)


### Bug fixes

* group project sessions and isolate shell job environments ([#50](https://github.com/nottherealsanta/nexus/issues/50)) ([5fabafc](https://github.com/nottherealsanta/nexus/commit/5fabafc03cb35ea08294ecb95e5fc4cf3a0ba4d7))

## [0.2.16](https://github.com/nottherealsanta/nexus/compare/v0.2.15...v0.2.16) (2026-10-01)


### Bug fixes

* store credentials locally and improve voice commands ([#48](https://github.com/nottherealsanta/nexus/issues/48)) ([0beee57](https://github.com/nottherealsanta/nexus/commit/0beee57607c950bcc899b423fd4e1c7fde33049c))

## [0.2.15](https://github.com/nottherealsanta/nexus/compare/v0.2.14...v0.2.15) (2026-10-01)


### Bug fixes

* initialize enabled model catalogues and repair diff preview interactions ([#46](https://github.com/nottherealsanta/nexus/issues/46)) ([ad536ab](https://github.com/nottherealsanta/nexus/commit/ad536abbafb1b933faa1222b94fb02f4ce9aa64e))

## [0.2.14](https://github.com/nottherealsanta/nexus/compare/v0.2.13...v0.2.14) (2026-10-01)


### Bug fixes

* use one shared agent model settings page ([#44](https://github.com/nottherealsanta/nexus/issues/44)) ([9773632](https://github.com/nottherealsanta/nexus/commit/97736324dedd77812786128d5b816a339b183467))

## [0.2.13](https://github.com/nottherealsanta/nexus/compare/v0.2.12...v0.2.13) (2026-10-01)


* release 0.2.13 as a patch ([#43](https://github.com/nottherealsanta/nexus/issues/43)) ([ad64d8f](https://github.com/nottherealsanta/nexus/commit/ad64d8f9459d7cbd1d7713b4e845325348eae16d))


### Features

* UI redesign from the design mock-ups (TUI + web) ([#41](https://github.com/nottherealsanta/nexus/issues/41)) ([c78b8cf](https://github.com/nottherealsanta/nexus/commit/c78b8cf444b89998095a4dc28ccc83b8c0a9609d))

## [0.2.12](https://github.com/nottherealsanta/nexus/compare/v0.2.11...v0.2.12) (2026-10-01)


### Bug fixes

* resolve MCP server working directories from the workspace ([#39](https://github.com/nottherealsanta/nexus/issues/39)) ([9c0ad5c](https://github.com/nottherealsanta/nexus/commit/9c0ad5ceb390dc0d4e8652f143d34f21932ea267))

## [0.2.11](https://github.com/nottherealsanta/nexus/compare/v0.2.10...v0.2.11) (2026-10-01)


### Features

* provider usage modal (Ctrl+U) and Claude sign-in in Settings ([#37](https://github.com/nottherealsanta/nexus/issues/37)) ([b5fa4aa](https://github.com/nottherealsanta/nexus/commit/b5fa4aacd3488f75d17d27171c2e014db8b9846d))

## [0.2.10](https://github.com/nottherealsanta/nexus/compare/v0.2.9...v0.2.10) (2026-10-01)


### Bug fixes

* live dictation previews, timeline polish, and bundled dependencies ([#35](https://github.com/nottherealsanta/nexus/issues/35)) ([9f8f4e9](https://github.com/nottherealsanta/nexus/commit/9f8f4e99e5b8313c49c55e8ba79576de4fda5d7c))

## [0.2.9](https://github.com/nottherealsanta/nexus/compare/v0.2.8...v0.2.9) (2026-10-01)


### Bug fixes

* restructure docs into per-subsystem pages with a task index ([#30](https://github.com/nottherealsanta/nexus/issues/30)) ([4080500](https://github.com/nottherealsanta/nexus/commit/4080500c86960e20c830201074b52c0d483de283))
* simplify root prompts and deduplicate AGENTS.md display ([#33](https://github.com/nottherealsanta/nexus/issues/33)) ([129255c](https://github.com/nottherealsanta/nexus/commit/129255cde6e9413703b118fe89451889dd969663))
* support clipboard image paste in terminal composer ([#32](https://github.com/nottherealsanta/nexus/issues/32)) ([85ad5fe](https://github.com/nottherealsanta/nexus/commit/85ad5feb160ae00942e21dee9c7ef15d0926b673))
* update build prompt test for simplified root prompts ([#34](https://github.com/nottherealsanta/nexus/issues/34)) ([251a7c1](https://github.com/nottherealsanta/nexus/commit/251a7c1b8f04cdf80883577f2bc2ae82b2f3d948))

## [0.2.8](https://github.com/nottherealsanta/nexus/compare/v0.2.7...v0.2.8) (2026-09-30)


### Bug fixes

* accept image and document attachments in both chat clients ([#28](https://github.com/nottherealsanta/nexus/issues/28)) ([ded98b0](https://github.com/nottherealsanta/nexus/commit/ded98b039a64e26e61d7040a5c40091d1206023c))
* present complete tool details and align settings across clients ([#27](https://github.com/nottherealsanta/nexus/issues/27)) ([2b027c3](https://github.com/nottherealsanta/nexus/commit/2b027c380e527c3b6c2d1bd1d124a6ca982a7be3))
* queue messages and add steer and interrupt controls ([#25](https://github.com/nottherealsanta/nexus/issues/25)) ([6b47f02](https://github.com/nottherealsanta/nexus/commit/6b47f02cdc6011d0b41f90dbc74f45d4ab489d80))

## [0.2.7](https://github.com/nottherealsanta/nexus/compare/v0.2.6...v0.2.7) (2026-09-30)


### Bug fixes

* discover scoped extensions and lock session context choices ([222e958](https://github.com/nottherealsanta/nexus/commit/222e95893e4dae6469f929f7011dab7060c2235e))

## [0.2.6](https://github.com/nottherealsanta/nexus/compare/v0.2.5...v0.2.6) (2026-09-30)


### Bug fixes

* show provider thinking in context panes ([#21](https://github.com/nottherealsanta/nexus/issues/21)) ([dd921c5](https://github.com/nottherealsanta/nexus/commit/dd921c540ff90f2c96c8f8d5d6b812b72d4d096a))

## [0.2.5](https://github.com/nottherealsanta/nexus/compare/v0.2.4...v0.2.5) (2026-09-30)


### Bug fixes

* autosave settings and unify scoped reset controls ([#19](https://github.com/nottherealsanta/nexus/issues/19)) ([0706ed4](https://github.com/nottherealsanta/nexus/commit/0706ed4e0dae2d4fdfd1e70d7320590b6bf00846))

## [0.2.4](https://github.com/nottherealsanta/nexus/compare/v0.2.3...v0.2.4) (2026-09-30)


### Bug fixes

* give web client a browser-native visual finish ([768bc3e](https://github.com/nottherealsanta/nexus/commit/768bc3e1ee07b52083fdb7c7d25f8e5455bebb73))

## [0.2.3](https://github.com/nottherealsanta/nexus/compare/v0.2.2...v0.2.3) (2026-09-30)


### Bug fixes

* add Claude subscription provider through Agent SDK ([#15](https://github.com/nottherealsanta/nexus/issues/15)) ([a1a70d5](https://github.com/nottherealsanta/nexus/commit/a1a70d5678dc40ff0c928221671be26c66d50b45))

## [0.2.2](https://github.com/nottherealsanta/nexus/compare/v0.2.1...v0.2.2) (2026-09-30)


### Bug fixes

* simplify CLI startup and manage local search service ([#13](https://github.com/nottherealsanta/nexus/issues/13)) ([f3f340c](https://github.com/nottherealsanta/nexus/commit/f3f340c8e7dc5dcb7e68208832e3faff4fcfc02d))

## [0.2.1](https://github.com/nottherealsanta/nexus/compare/v0.2.0...v0.2.1) (2026-09-30)


### Bug fixes

* complete patch release requests through merge and publication ([#12](https://github.com/nottherealsanta/nexus/issues/12)) ([5293830](https://github.com/nottherealsanta/nexus/commit/5293830e233cae7828e33542f3a24d9ee7c0c43b))
* drop unsupported --refresh-package from uv tool upgrade in nexus update ([#8](https://github.com/nottherealsanta/nexus/issues/8)) ([f64ce73](https://github.com/nottherealsanta/nexus/commit/f64ce733065e4b7aa2070ef2f4350207893c4390))
* restore last released version so release-please proposes 0.2.1 ([#10](https://github.com/nottherealsanta/nexus/issues/10)) ([a3d7ba0](https://github.com/nottherealsanta/nexus/commit/a3d7ba03e93737c30d2fb4609c64cdca87f8649c))

## [0.2.0](https://github.com/nottherealsanta/nexus/compare/v0.1.2...v0.2.0) (2026-09-30)


### Features

* show an update-available notice and keep early daemon output ([#5](https://github.com/nottherealsanta/nexus/issues/5)) ([5b6cbb5](https://github.com/nottherealsanta/nexus/commit/5b6cbb523256995cf6cee79cd8fe719f1a266f41))
* yield long bash commands to the background and wait until exit ([#7](https://github.com/nottherealsanta/nexus/issues/7)) ([8a41bdf](https://github.com/nottherealsanta/nexus/commit/8a41bdf395a7d5e7bf1fc041456bf3d912ba8bba))

## [0.1.2](https://github.com/nottherealsanta/nexus/compare/v0.1.1...v0.1.2) (2026-09-30)


### Features

* add local voice dictation and grouped tool timelines ([66c584d](https://github.com/nottherealsanta/nexus/commit/66c584da09f687e047fa3bc0426e1b2e7b5913fe))

## [0.1.1](https://github.com/nottherealsanta/nexus/compare/v0.1.0...v0.1.1) (2026-09-30)


### Features

* install from PyPI by default and make nexus update follow releases ([4febed4](https://github.com/nottherealsanta/nexus/commit/4febed41f61312887cef2ef625a36abba4059bb2))

## 0.1.0 (2026-09-29)


* add release-please automation ([fdbf33f](https://github.com/nottherealsanta/nexus/commit/fdbf33f82bb8f9dde7551cafa07c4b86e126ffcd))
