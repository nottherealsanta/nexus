# Decisions

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

Made (user):

| Date | Decision |
| --- | --- |
| 2026-11-11 | **Plan 41: the terminal dock is user-only and the bash tool is separate.** Reverses the 2026-11-08 decision that running background bash jobs appear in the dock with interactive input: agent bash jobs are pipe processes again with their own `N shell` indicator; background jobs lose interactive input; evidence provenance drops session/turn/tool-call/job ids; the inspector `terminal` lane is removed (a `job` lane is a follow-up); old job-linked terminal metadata is marked closed on load |
| 2026-11-11 | **Plans 29–32:** question composer locking does not change approval Deny / “tell it what to do instead”; pending-row edit/up/down/remove icons are for **queued messages only**, with no Steer button (existing keyboard actions stay); Worktree details includes branch and main-checkout path, with no Manage worktrees link. |
| 2026-11-04 | Manager is a top-bar dropdown; Settings value rows are segmented; only a session's first message pins to the top; pending rows are `S` / `Q`; finished sessions' events are compacted with a `session.compacted` record; CI deferred |
| 2026-11-05 | Manager header indicator is a gray braille wave field; Stop on hover while Live; an open Manager panel dims the rest |
| 2026-11-06 | Project remove (unregister) and delete (permanent) both stay. Composer voice/send buttons are an approved NSH-only difference. Scroll bar and segmented Settings are shared |
| 2026-11-06 | **NSH terminal backend dropped.** `nexus-tui` stays; `nexus nsh tui` is retired, and its code is kept in the tag `archive/nsh-terminal-backend` (pushed) |
| 2026-11-06 | **Modals are translucent in the TUI again** (the 50% `paint::scrim` blend), reverting the solid cover of `0452ee80`; NSH is back to its translucent backdrop, so they match |
| 2026-11-06 | **Mobile = Sessions, Manager and Settings tabs only** (section 5). It can approve and answer everything a desktop client can (allow/deny, questions), showing less on the small screen |
| 2026-11-06 | **ZMX is for later.** Keep `ZMX_plan.md` and section 6 as they are; build nothing now. The Claude Code Agent SDK / ZMX Settings toggle is part of that later work |
| 2026-11-10 | **Resume ZMX implementation**, referencing `nexus01`, and keep it separate so non-ZMX behavior does not change. Start with isolated worker transports; no live/paid calls are authorized by this direction. |
| 2026-11-11 | **The Manager is done** (section 3). Its unrun verification items stay as an unscheduled list; new Manager work is sections 27 and 28 |
| 2026-11-11 | **PLAN.md became the `plans/` folder**; `ZMX_plan.md` moved to `plans/zmx-design.md`. Mobile redesign and the NSH look are one piece of work (section 5) |
| 2026-11-06 | **macOS GPUI direction:** spike a Rust GPUI host embedding the existing NSH app; keep the current AppKit client intact and expose the spike separately as `nexus gpui`. |

## GPUI host spike

- [~] Added an independent `nexus-gpui` host and launcher. The NSH 0.4.0 bundle and host compile; window rendering, Core connectivity, theme and parity remain unverified. `nsh run App.tsx --target gpui --port 0` produced no output before a 45-second timeout, so its development host is not confirmed working.
- The host pins the same NSH 0.4.0 revision as `nexus-nsh/native` and the installed CLI, rather than the tag, so their registry types match. It registers the app's native Mermaid, annotation and microphone modules. GPUI uses runtime Metal shaders.
- GPUI now loads the built bundle's capabilities metadata: without it, the NSH runtime denies reading the local daemon file. Bundle loading has a regression test; a live GPUI/Core connection remains unverified. Native discovery now honors an explicit `NEXUS10_HOME`, refuses fallback to a personal daemon, and has bounded-bootstrap regression tests (8 native tests pass).
- NSH documents GPUI differences including no blur/backdrop filter, different opacity, partial transforms and rounded clipping, macOS video placeholders, web views above NSH content without ancestor clipping, and limited accessibility actions. Manager consent now uses a translucent solid surface without backdrop blur. Verify the theme, assets, network/auth and native features against AppKit and the TUI before considering this more than a spike.

Open (each has a default that holds unless the user objects):

| Decision | Default |
| --- | --- |
| **Z1. ZMX scope (when ZMX is picked up).** `ZMX_plan.md` D1 says subagents only. The new hint ("does not use Nexus agents or tools") reads as if a user would pick ZMX for whole sessions | Keep D1: root sessions always use the SDK; the toggle picks the backend for Claude models a **subagent** uses |
| **Z2. Is the toggle per vendor?** Antigravity has no SDK route | Claude Code: `Agent SDK` / `ZMX`. Antigravity: a plain on/off, shown only in ZMX terms |
| Browser (old 14.4): still wanted? | Deferred. Only the terminal is in this plan; the browser stays in 9.21. If picked up, decide first: rendering in terminals without image support (a lossy text view needs approval and a ledger row), remote browser access (no powerful resource on a tokenless non-loopback server), profile persistence and scoped agent control (start isolated, user-controlled), Chromium distribution, and Core-parsed grid vs raw bytes per client (`reports/section14/spikes.md`) |
| Terminal parser strategy, access model, agent typing (section 2) | Defaults in section 2; confirm at the design review |
| Does `nexus update` install or only report? | Report only until decided |
| Manager Q4: can `navigate` open Settings (now a mobile tab too)? | Open to show a section; never change a value or sign in |
| Do Anthropic Messages and Gemini ship in 0.10.x? | — |
| Can a parent card steer a child session? Persist session grants? Output caps (lines or bytes)? | — |
| Show the Claude SDK's hidden additions (and is sending the account email acceptable)? | — |
| Turn summaries: show "Thought for Ns"; the 140-column footer; store `max_attempts`? | Not shown / accept / today's wording |
| Fold the starting context after the first message; underline prose links? | Leave open / underline in prose |
| Project order in the Projects list and picker (rail only today) | Rail only |
| **Worktree integration for agents (plan 9.15).** Review and integrate exist as user control-plane API only. Should an agent be able to call them, and with what approval? The `worktree` control tool is ungated today. | Not exposed; the user integrates from a client |
| **MCP resources and prompts for the model (plan 9.13).** Core serves them read-only to the user API. Should the model read resources or prompts, through `mcp_call`-like tools or `@` mentions? | Not exposed to the model |
| **Durable pending directory moves (plan 9.16).** A move queued during a turn is memory only and is lost with the daemon. Persist and apply it at recovery, or drop it with a notice? | Drop; a model-requested move is not replayed after a crash |

- **No blaze (user, 2026-11-11).** blaze (indexable-inc/blaze, in-memory Rust build daemon) was tried and dropped:
  stable 0.20261027.0 refused any `std::thread` program (all test binaries, the TUI, a 3-line repro); canary
  0.20261105.4 stalled compiling the standard library. The user's system then froze while opening the TUI terminal;
  whether blaze (two daemons on a 16 GB Mac) caused it is unknown (no panic report, no reboot). It was uninstalled and the launcher opt-in removed. Do not add blaze or similar unofficial cargo replacements
  without asking.
