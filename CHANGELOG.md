# Changelog

## Unreleased

- Improve TUI rendering performance with coalesced updates, cached Markdown/layout, and shared timeline rows; render streaming Markdown and bound live thought previews.
- Add paged, multi-question `ask` interactions, clearer activity counts, session status, and completion/needs-input notification sounds in both clients.
- Replace Bark speech recognition with pinned Parakeet Redux in an offline worker, with verified caching, cancellation, timeout handling, and updated voice documentation.
- Add expirable, content-addressed wire payload storage while preserving durable event envelopes; introduce bounded retention maintenance, protected-session handling, and expired-payload visibility in exports and the inspector.
- Make project MCP entries start automatically for new sessions unless explicitly disabled; document the trust implications.
- Improve NSH project navigation, activity indicators, session restoration, layout, typography, and composer context display.
- Refine TUI and NSH settings, dictation, image handling, and timeline presentation.
- Running `nexus` or `nexus tui` opens the terminal client directly, passing through command-line flags.

## [0.10.0](https://github.com/nottherealsanta/nexus/compare/v0.7.2...v0.10.0) (2026-10-30)

Nexus 0.10 is a rebuild around a shared Core and an ordered, visible context timeline.

- A terminal client, session trees, subagents, context controls, and approvals.
- Provider integrations, model selection, workspace tools, skills, MCP servers, and hooks.
- Session search, file editing, worktrees, voice features, and migration from old Nexus.
- Release installation with checksum verification for macOS and Linux. The web client requires a checkout.

The detailed changes included in this release follow.

### Features

* agent hot reload events and quarantined user tools (E) ([ba5621d](https://github.com/nottherealsanta/nexus/commit/ba5621d04a31f32e244d6e8bab6922256e206c88))
* API 1.4 (info.code); clients pin 1.4 ([65c7ff1](https://github.com/nottherealsanta/nexus/commit/65c7ff15971ed3a60eb31bba492c9aa38616e9be))
* API-key sign-ins and OpenAI-compatible providers (openai, groq, nvidia, ollama) ([9f6de87](https://github.com/nottherealsanta/nexus/commit/9f6de87f2eb6f38caf974e9f4ef7476e546c43d3))
* built-in editor in both clients (K10): edit, save with revision, reload, files.changed ([44a2f59](https://github.com/nottherealsanta/nexus/commit/44a2f5942a70eac34e13261128579d2eb2af4517))
* **claude:** add the Claude provider through the Agent SDK ([6c3f79a](https://github.com/nottherealsanta/nexus/commit/6c3f79af66152e0c445f0cb1103129ef8a176527))
* clients load older messages, confirm cache-losing switches, show plan limits and tool images (K6, K9, K11, K8) ([a00c6be](https://github.com/nottherealsanta/nexus/commit/a00c6bea41792545b1336528412b557ad7e1e0b4))
* clients offer allow-for-session and deny-with-a-note (K7) ([a27b67b](https://github.com/nottherealsanta/nexus/commit/a27b67bead58d2e7269262c84dbb82a34b294b87))
* clients search sessions through Core, with the matching message (K5) ([762456f](https://github.com/nottherealsanta/nexus/commit/762456f6e2c3b140fb34d243df7fada7d129e29b))
* **clients:** TUI markdown, mermaid, editor, files tab; NSH and core updates ([13651aa](https://github.com/nottherealsanta/nexus/commit/13651aa74fecb295e653006fb4910cb2bdd454eb))
* command hooks (H) ([ad34efa](https://github.com/nottherealsanta/nexus/commit/ad34efa52e8507982161534e61a79dce406607f5))
* complete config, mock provider and context budget foundations ([eb7168f](https://github.com/nottherealsanta/nexus/commit/eb7168fbbec43dfa2a132806cbb9229669ae2f23))
* complete section D tools (grep, glob, ls, multiedit, web, permissions, escalate) ([af5f527](https://github.com/nottherealsanta/nexus/commit/af5f52793fc74a85ff34cb748d343b0a5ea7c185))
* **core:** add durable event cursors and reconnect replay ([419fc81](https://github.com/nottherealsanta/nexus/commit/419fc81fb5ec25a740738a30a5df865961d2f730))
* **core:** add interactive ask tool and question lifecycle ([c7e8e31](https://github.com/nottherealsanta/nexus/commit/c7e8e3136b932d1594d179fd57d35caafea7f3ab))
* **core:** add manual session continuation ([69cc68b](https://github.com/nottherealsanta/nexus/commit/69cc68b528cb459a06cf47837953172651107b52))
* **core:** add on-demand workspace and worktree controls ([cda0907](https://github.com/nottherealsanta/nexus/commit/cda0907821fa7317232b7d17729bfbe187b92ae1))
* **core:** add shared kernels, tool switches, and bash jobs ([c384e1a](https://github.com/nottherealsanta/nexus/commit/c384e1a60eeccd4ad678575079adb0a6b9f80ccf))
* **core:** archive and unarchive session trees ([75be0ce](https://github.com/nottherealsanta/nexus/commit/75be0ce1684ffb77a7da950b4ded6e42f426151c))
* **core:** complete mutation replay, retries and retention ([d7c95e3](https://github.com/nottherealsanta/nexus/commit/d7c95e30c3d9c9d10ac3cd784fb4ffe837b9ff38))
* **core:** enforce file read freshness without edit approvals ([364c5cf](https://github.com/nottherealsanta/nexus/commit/364c5cfc911b2655ea46af3cd35b2084e305bfc7))
* **core:** export session context as Markdown and JSON ([369806b](https://github.com/nottherealsanta/nexus/commit/369806b0e86922373e6ab0212938283578baa0c2))
* **core:** persist session snapshots and instruction files ([0b9423e](https://github.com/nottherealsanta/nexus/commit/0b9423e8487ce7b902d94c38f3b17c6ecee5eb64))
* **core:** recover interrupted turns on daemon startup ([656e71c](https://github.com/nottherealsanta/nexus/commit/656e71c6161e361ca6166b324fbd97d33229f011))
* **core:** report api_version in /api/info ([8339ad8](https://github.com/nottherealsanta/nexus/commit/8339ad8b556e5a48d659b4c2dde5661581c59b7d))
* expand settings and context controls ([0888d6d](https://github.com/nottherealsanta/nexus/commit/0888d6db5a017c4abe1678feecb26ed71e1ff5b7))
* **files:** add workspace file search for @ completion ([9c41ca3](https://github.com/nottherealsanta/nexus/commit/9c41ca369e1501b738bf95ad43cfc7ddf407fe56))
* handoff, auto titles and /btw (K1-K3) with link back and title-model settings ([5124d09](https://github.com/nottherealsanta/nexus/commit/5124d093485d3d817634b461c701020e6f6e4594))
* image context and interface improvements ([fdb1860](https://github.com/nottherealsanta/nexus/commit/fdb186026570a8722ffcf9d07dceaa9c2d82b8e7))
* improve agent configuration and session interfaces ([764a854](https://github.com/nottherealsanta/nexus/commit/764a8540808c9e16d945d7e096e70abca3893307))
* improve iOS gateway session experience ([01fec6a](https://github.com/nottherealsanta/nexus/commit/01fec6a83452cb68eb3bebbc224c1617890a43ec))
* improve native navigation and worktree lifecycle ([4883a23](https://github.com/nottherealsanta/nexus/commit/4883a23a60038407cccf39139bc020f0c91d178a))
* improve timeline and client parity ([da32113](https://github.com/nottherealsanta/nexus/commit/da3211351b5a01f1363077af20f489296a447eff))
* install.sh installs from a GitHub release ([175b7ba](https://github.com/nottherealsanta/nexus/commit/175b7ba2176dd2c1213a619e45ac989a8f7ee03e))
* installer warns when libasound is missing; record the release dry run ([4083f02](https://github.com/nottherealsanta/nexus/commit/4083f0277b4ffbb9f2adf528a15187077742a414))
* **interface:** add NSH session tabs and timeline detail levels ([0624c37](https://github.com/nottherealsanta/nexus/commit/0624c370c269f5789f4a47353bd7496ade360af1))
* **interface:** add the shared interface map and design language ([3e43425](https://github.com/nottherealsanta/nexus/commit/3e4342567849327b67f4d2f02f9bb436e6f59113))
* merge NSH web replacement ([cebce3d](https://github.com/nottherealsanta/nexus/commit/cebce3d92c472f017e82571fe5e995066a1f1282))
* merge project explorer sidebar improvements ([f7c92f7](https://github.com/nottherealsanta/nexus/commit/f7c92f74eb725cbe66b452ff54b24d984aecec28))
* migrate to ~/.nexus and prepare the 0.10.0 release plan ([dc6a392](https://github.com/nottherealsanta/nexus/commit/dc6a3921a34e7178e4727f84c41a3e7c93f93b56))
* nexus 0.10 core: providers, daemon, agents, session trees, inspector ([b4ded72](https://github.com/nottherealsanta/nexus/commit/b4ded72e6fc8368f8603796e68884bf0c79ee94f))
* **nsh:** add the NSH web client and hardened host proxy ([78935b8](https://github.com/nottherealsanta/nexus/commit/78935b80318e4b505c50340ccf41fd7a24c0dac9))
* **nsh:** one modal recipe (DESIGN_PLAN I7) ([ef9a9a0](https://github.com/nottherealsanta/nexus/commit/ef9a9a099d504f8be6df8a4ee4181f83d2a44afe))
* **nsh:** parity with the TUI: composer, context section, footer cost, output caps, session totals, row menus, project remove, settings ([b6efec1](https://github.com/nottherealsanta/nexus/commit/b6efec15deb1604d82ed705f4339f642b834a2f8))
* **nsh:** sessions and right sidebars (DESIGN_PLAN I6) ([5f2d183](https://github.com/nottherealsanta/nexus/commit/5f2d18388dddc7d4c54037577ead93054d807149))
* **nsh:** waiting card and alerts (DESIGN_PLAN I8) ([b2e899f](https://github.com/nottherealsanta/nexus/commit/b2e899f0e8f0d2b87e44566e34e9bcc6d37e8646))
* operations (N1-N3): nexus doctor with daemon code fingerprint, install.sh, nexus update ([b7f3562](https://github.com/nottherealsanta/nexus/commit/b7f3562b9c5f9e7b0aa08955f97a45028628754e))
* prompt history from Core with TUI and NSH recall ([4cdaf06](https://github.com/nottherealsanta/nexus/commit/4cdaf06f17dbd3d23f73c4879ff37e71e4aada73))
* Refactor agent orchestration and improve UI for subagent interactions ([ed3e4ce](https://github.com/nottherealsanta/nexus/commit/ed3e4ce6f132c48a91ecff6acf71e3220ac44fe7))
* remember the last selected model instead of a default model ([dc12063](https://github.com/nottherealsanta/nexus/commit/dc12063d280fe17121abfc1cd166a2811166b9e4))
* replace web and GPUI desktop clients with the NSH client ([8c4da14](https://github.com/nottherealsanta/nexus/commit/8c4da1487475dcde234ab43232d742930a175136))
* report terminal program status ([81b81e5](https://github.com/nottherealsanta/nexus/commit/81b81e57768dcbe328d0074af1a44f52a60e1827))
* search, paging, session approvals, artifacts, file writes, usage (K5-K11) and tool-result media ([3a03a00](https://github.com/nottherealsanta/nexus/commit/3a03a007ea37769b9d943a9aec17239145327648))
* session drafts (K4): Core builds the real starting context for a new session ([5fb6ef0](https://github.com/nottherealsanta/nexus/commit/5fb6ef0a323816c099f2163c4dec4abde48faba3))
* show a starting card while a ratatui turn begins ([56b2cd3](https://github.com/nottherealsanta/nexus/commit/56b2cd31025e0c8c83d9aef7568d3f51e3aad1d3))
* skills and MCP redesign (F, G) ([8b73101](https://github.com/nottherealsanta/nexus/commit/8b731016307809a8d654ae5188c51ba65e9eeae6))
* skills, /learn and MCP (F, G) ([78478a8](https://github.com/nottherealsanta/nexus/commit/78478a80be246a130ae9acaf9f1cb49f0614c1fc))
* steer and queue pending inputs across Core, TUI and NSH ([2ba1eac](https://github.com/nottherealsanta/nexus/commit/2ba1eac0be1c1dd9564f0a3c10670b583590b7c3))
* TUI and NSH skills and MCP UI ([71bf443](https://github.com/nottherealsanta/nexus/commit/71bf443c5fe96bddf2e950419f566de857eaf340))
* TUI design pass, snapshot and runtime updates, docs ([3fae937](https://github.com/nottherealsanta/nexus/commit/3fae93744ce16bbed76d1744fb8af7e526b175c7))
* **tui:** add the Ratatui client with the full session timeline ([11ccdf7](https://github.com/nottherealsanta/nexus/commit/11ccdf708d425c3cbcad745e6ff14270113355ce))
* **tui:** agent-coloured Working shimmer, all tool verbs, bash exit codes ([d336d29](https://github.com/nottherealsanta/nexus/commit/d336d29c254f5a1527ff9ae6071c1b933903c721))
* **tui:** answer ask questions from the waiting card ([fe363bc](https://github.com/nottherealsanta/nexus/commit/fe363bc45bb49bb57b15c05dc77f777f175b61f6))
* **tui:** as-you-type composer completion for / commands and @ files ([0a771ba](https://github.com/nottherealsanta/nexus/commit/0a771bacff918b3f84e3040a030604eca361640d))
* **tui:** composer location strip and half-row panel edge, richer context section, priced turn footer, K counts ([43586c2](https://github.com/nottherealsanta/nexus/commit/43586c27e91783e9326d09dd1bedb358ce2d0a5d))
* **tui:** files, narrow layouts and states (DESIGN_PLAN I10) ([79ccecd](https://github.com/nottherealsanta/nexus/commit/79ccecd2fd36a5fe4bd40cd617c0142ab635aadf))
* **tui:** header tabs, resizable sidebar with rail, settings modal, tighter diagnostics ([b46f58f](https://github.com/nottherealsanta/nexus/commit/b46f58f4eefe4b9361c57210f4c74bc9fa8c235e))
* **tui:** interface: TUI composer and project strip (DESIGN_PLAN I5) ([2f4c710](https://github.com/nottherealsanta/nexus/commit/2f4c71027a53f39f3a348e9102065a2c677cac83))
* **tui:** new-session view and starting context (DESIGN_PLAN I9) ([8907ae6](https://github.com/nottherealsanta/nexus/commit/8907ae66a21b2996532753681394588d4ea33baf))
* **tui:** old-Nexus context chips and right-aligned turn footer ([d431e34](https://github.com/nottherealsanta/nexus/commit/d431e347cc95c50484825c457314614315cdf998))
* **tui:** one modal recipe (DESIGN_PLAN I7) ([bd2e721](https://github.com/nottherealsanta/nexus/commit/bd2e72118c0736511ab6a4a91d08b96718f0c1d9))
* **tui:** palette group headings (DESIGN_PLAN D13) ([b461763](https://github.com/nottherealsanta/nexus/commit/b461763467b5371f32203fcffad6085027dac2fd))
* **tui:** search the file view with / ([2aac954](https://github.com/nottherealsanta/nexus/commit/2aac95430187885cd930981ce6e394e461393acc))
* **tui:** session info on/off tools, failed children; follow/anchor/jump test at 80x24 and 160x48 ([27ba5f8](https://github.com/nottherealsanta/nexus/commit/27ba5f822eff78f0cdc490ac1c30812c22c1a555))
* **tui:** session row time and child count; persist client preferences in tui.json ([e321a42](https://github.com/nottherealsanta/nexus/commit/e321a4249803b93db95e5264fab2b1f9d5028dd2))
* **tui:** session tabs, composer rail and status band, detail levels, drafts for new sessions, sidebar edge drag ([5409281](https://github.com/nottherealsanta/nexus/commit/54092811bc6af8b1a8da393621c6e164a10b363a))
* **tui:** sessions and right sidebars (DESIGN_PLAN I6) ([1cdead9](https://github.com/nottherealsanta/nexus/commit/1cdead97669943be5ae23f348769e66b5fa21223))
* **tui:** sidebar tab icons with hover labels, real model names, new-session context preview, plain diagnostics ([ed35ebb](https://github.com/nottherealsanta/nexus/commit/ed35ebbc75a9e9867e361af746d7de096dd3d8aa))
* **tui:** split Git status into groups and open file diffs ([a1f0db8](https://github.com/nottherealsanta/nexus/commit/a1f0db8ecfd6dddfbcbb1392e6af9e84cf78431e))
* **tui:** terminal notifications for sessions that need the user ([3cb085e](https://github.com/nottherealsanta/nexus/commit/3cb085ef0466f72b74e73d07f4f1fea8c6849890))
* **tui:** tool output head+tail with show all, session info totals, row menu, project add/remove, settings from Core ([dd25583](https://github.com/nottherealsanta/nexus/commit/dd25583e08d9ddd99d859048da2e8ae36ab2c0e9))
* **tui:** waiting card and alerts (DESIGN_PLAN I8) ([96aa2ff](https://github.com/nottherealsanta/nexus/commit/96aa2ff926d4f69e0ad65cdc9d271bd4f4c382b2))
* turn summaries instead of loading event history ([3155d66](https://github.com/nottherealsanta/nexus/commit/3155d66141be5849f7caeb3d2b8749b30d4fd0f6))
* update client UI and voice behavior ([f2f23df](https://github.com/nottherealsanta/nexus/commit/f2f23df44ac3dd77c2530fa48ee4553578c3c65a))
* update TODO items with implementation status and add new subagent design plans ([409e640](https://github.com/nottherealsanta/nexus/commit/409e6404f51f8d63aecf40f7837bacf5433c8135))
* voice (I1-I6): HF cache status, TTS speak, offline Bark STT, nexus voice init, TUI and NSH playback, dictation and Settings ([47c9702](https://github.com/nottherealsanta/nexus/commit/47c970232876192ae1f64c8123edfbc5077866c5))


### Bug fixes

* align animated status row ([8a983e3](https://github.com/nottherealsanta/nexus/commit/8a983e39ebbfe5a97274f3fc7a79c932fd3e20a6))
* align reasoning rows in activity groups ([59c0742](https://github.com/nottherealsanta/nexus/commit/59c07428d848761800293eaa262ad2ffb2014e80))
* align web sidebar and use descriptive session titles ([bb68921](https://github.com/nottherealsanta/nexus/commit/bb68921aabe0a5a29c7ed366bd8112da95bc1019))
* **ci:** pin the release publish job to Python 3.13 ([388b42b](https://github.com/nottherealsanta/nexus/commit/388b42b15bd31de59e6eb6f41b48263e2bbb350a))
* **ci:** pin the release publish job to Python 3.13 ([#96](https://github.com/nottherealsanta/nexus/issues/96)) ([90b4894](https://github.com/nottherealsanta/nexus/commit/90b48943195b4db39cb91a93f0477cf80aded5c6))
* **ci:** skip musllinux x86_64 wheels so releases can publish ([4dcd1be](https://github.com/nottherealsanta/nexus/commit/4dcd1be76a93705ca61c960ff11bd6ba30a1066c))
* **ci:** skip musllinux x86_64 wheels so releases can publish ([#94](https://github.com/nottherealsanta/nexus/issues/94)) ([16be999](https://github.com/nottherealsanta/nexus/commit/16be999e3673b7c385105de053c32027f5f748da))
* **interface:** OS notifications row is NSH-only; the TUI has terminal notifications ([4c4b9a0](https://github.com/nottherealsanta/nexus/commit/4c4b9a0d3b3135790e8a19dce0f49cd58747889f))
* merge animated status dots ([8b20292](https://github.com/nottherealsanta/nexus/commit/8b20292e719d9cd7e7f02c8c91be4cee08d95d4f))
* **nsh:** one Thought row per reasoning part ([422654d](https://github.com/nottherealsanta/nexus/commit/422654d56c598c9c6f43a2f285b4cb7e59fc431e))
* **nsh:** verify I1–I5 on nsh 0.3 (check, tests, shots) ([b76a120](https://github.com/nottherealsanta/nexus/commit/b76a1202c9bdbe7cd6ab6c18acb8e26c2738c990))
* **nsh:** waiting card keys and edit diff text ([a0f9948](https://github.com/nottherealsanta/nexus/commit/a0f99481372c7a5321647a4b492e52427abf5450))
* repair the Claude and TUI tests CI found; record CI findings in the plan ([54c0c0b](https://github.com/nottherealsanta/nexus/commit/54c0c0b2fa2806d469b18de3a91db44f3302526f))
* route Copilot GPT models to /responses, 404 for missing files, MCP default override, TUI session skills/MCP rows ([97ddfea](https://github.com/nottherealsanta/nexus/commit/97ddfeaa4d43e69edd3f065f6d5796e02370183f))
* **tui:** context preview shows only what the model receives ([087c3e2](https://github.com/nottherealsanta/nexus/commit/087c3e24d372281775b8633bbdb860bdc2c16c7d))
* **tui:** Files and Projects tabs expand the rail; screenshots start from default prefs ([91ff519](https://github.com/nottherealsanta/nexus/commit/91ff5191e1a2d24073e503848caddb56ffd1ed5f))
* **tui:** portable shots.sh with port overrides ([c01db6a](https://github.com/nottherealsanta/nexus/commit/c01db6adbf9f40ac62a0070276d29fc3ef4669d0))
* version_parsing test input; cargo fmt ([21323a2](https://github.com/nottherealsanta/nexus/commit/21323a220cd8be4e171fb2ad3189be4c831eb10e))
