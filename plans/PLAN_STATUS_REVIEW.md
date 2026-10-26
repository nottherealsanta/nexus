# Nexus Plan Status Review and Document Inventory

> **Document Status:** Updated plan-status review as of 2026-10-26.
> **Source Documents Audited:** Relevant planning and done ledgers. Inventory counts below are historical and were not recalculated.
> **Rule Adherence:** Status assessments use the current ledgers, cited tests, and working-tree evidence; pending acceptance gates are explicitly retained.

---

## 1. Executive Summary & Inventory Reconciliation

### 1.1 Document Inventory Count
Before this review was generated, exactly **48 Markdown documents** resided within the `plans/` directory tree:
- **`plans/done/`**: 24 files (12 completed, 5 partial/paused, 3 stale/superseded, 4 reference/non-plan artifacts)
- **`plans/in-progress/`**: 4 files (unified sessions, manager orchestration, session names/context, NVIDIA provider)
- **`plans/planning/`**: 7 files (6 completed but misclassified/misplaced, 1 proposed unstarted)
- **`plans/` (top-level proposals)**: 2 files (reference recommendations, subagent continuation)
- **`plans/worktree-sessions/`**: 11 files (README index and phases 01 through 10)
- **Total Audited Plans/Documents:** **48 files** (49 total on disk including this report).

### 1.2 Inventory Fact & Naming Reconciliations
- **Filename Suffixes in `plans/done/`:** Review prompts often reference plan abbreviations (e.g. `CONTEXT_HEADER_TOOLS`, `CLAUDE_CODE_ZMX`, `GROK_BUILD_TUI`). On disk, almost all plan files retain the explicit `_PLAN.md` suffix (e.g., `CONTEXT_HEADER_TOOLS_PLAN.md`, `CLAUDE_CODE_ZMX_PLAN.md`, `GROK_BUILD_TUI_PLAN.md`).
- **Misclassified Planning Files:** Six documents in `plans/planning/` (`04-skill-add-via-npx-skills.md`, `05-mcp-server-add.md`, `06-subagent-roster-and-settings-notice.md`, `07-root-only-subagent-spawning.md`, `08-at-mention-folders.md`, and `09-subagent-page-back-button.md`) are fully implemented and verified offline/PTY as of 2026-10-20. They are functionally complete but reside in `plans/planning/` rather than `plans/done/`.
- **Status Basis (Assessment vs. Assertion):** Where live-model verification or end-to-end service soaks were not run (e.g., GPT Live audio, real NVIDIA token requests, RLM model soak), statuses are recorded strictly as *assessments* rather than *assertions*.
- **Working Tree Separation:** Active changes in the working tree (such as worktree bar additions or subagent worktree flags) are noted where relevant, but are not treated as finished features without test/ledger evidence.

---

## 2. Summary of User Attention

The following items represent outstanding architectural decisions, real-token spend approvals, or temporary states requiring human maintainer guidance:

### 2.1 Live API Tokens & Spend (Soak / Real-Service Gates)
- **Manager Session Orchestration (`02-manager-session-orchestration.md`):** M1–M9 backend, Ratatui pinned manager row, desktop card, and CLI are implemented and verified offline. Live soak against GPT Live / Codex is pending before flipping default enablement (`docs/manager.md`).
- **Code Mode & Recursive Context (`03-session-names-and-recursive-context.md`):** CM1–CM8 offline verified; `[code_mode] enabled` defaults to `true`. Live provider trials and RLM live soak are pending approval for real token spend.
- **NVIDIA Provider (`04-nvidia-provider.md`):** Adapter and key-handling implemented and verified offline. Live API completion validation is pending optional credentials / token spend; can be waived if offline verification is sufficient.
- **Antigravity Worker (`plans/done/ANTIGRAVITY_PLAN.md`):** Offline implementation and verification complete; live model turn is unverified and optional.
- **GPT Live Voice (`plans/done/GPT_LIVE_PLAN.md`):** M1–M5 implemented offline on `feat/gpt-live`; M6 is optional. Real microphone audio and live OpenAI Realtime API soak remain unverified.

### 2.2 Architecture & Policy Decisions
- **Manager Section 10 Live-Only Rethink (`02-manager-session-orchestration.md`):** Proposed architecture where GPT Live attaches solely to the manager session, which routes messages to sub-sessions. Reopens contracts for `session_send` and Live attach. Maintainer decision needed on whether to adopt §10 before proceeding with Live soak.
- **Manager Automatic Compaction (`plans/planning/manager-automatic-compaction.md`):** Unstarted architectural proposal (2026-10-23). Requires maintainer decisions on compaction thresholds, machine memory isolation (`system_override` vs separate namespace), and SQLite durable summary format.
- **Subagent Continuation Scope (`plans/SUBAGENT_CONTINUATION_PLAN.md`):** v1 implemented (P0–P3, code half of P4). Maintainer decision required on whether to close continuation scope at v1 and archive the plan, or proceed with P5 (interactive continuation / prompt caching optimizations).
- **Reference Project Recommendations (`plans/REFERENCE_PROJECT_RECOMMENDATIONS.md`):** Survey of CodexBar and grok-build. Maintainer decision required on which individual items to draft into dedicated plans; note overlap between Recommendation P1 and Worktree Checkpoints.
- **Web / Native TUI Parity Disposition (`plans/done/WEB_TUI_PARITY_PLAN.md`):** 21 unchecked items remain, but the browser client is deprecated per `AGENTS.md`. Maintainer decision recommended to formally waive remaining parity checkboxes and close the plan.
- **Desktop GPUI Development Pause (`plans/done/DESKTOP_OVERHAUL_PLAN.md`):** Desktop development is paused per `AGENTS.md:8`. Phase 3 items remain suspended until desktop work is explicitly resumed.
- **Unified Sessions Soak Waiver (`plans/in-progress/01-unified-sessions-and-handoffs.md`):** P0–P6 completed under explicit maintainer waiver of the real-usage soak gate; P7 optional/deferred. Maintainer should confirm archiving the file to `plans/done/`.

### 2.3 Temporarily Disabled Features & Active Tree Changes
- **Subagent Worktrees (`plans/worktree-sessions/05-subagent-worktrees.md`):** Implemented under simplified scope, but temporarily disabled by current working-tree decision pending session naming and manager delegation resolution. Needs re-enabling.
- **Session Worktrees User-Named Slugs (`plans/worktree-sessions/03-session-worktrees-and-turn-commits.md`):** Implemented on protocol v6; uncommitted working-tree edits present for user-named session branch slugs (`tests/test_worktree_session_names.py`).
- **Worktree TUI Additions (`plans/worktree-sessions/08-tui-and-host-protocol.md`):** Three-mode session modal complete; working tree has active uncommitted UI enhancements (`nexus/ui/ratatui/worktree_bar.py`, `worktree_pages.py`).
- **Manager Disabled by Default:** Manager session code is in place, but remains disabled by default in runtime configuration pending live soak.

---

## 3. Categorized Plan Review Tables

### 3.1 Category 1: Done Folder (`plans/done/` — 24 Files)

#### Completed Plans (12 Files)
| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/done/ANTIGRAVITY_PLAN.md` | Antigravity (`agy`) Subagent Worker | Complete (Offline Verified) | Plan header & §14: offline verification complete; live model turn not run. | Keep as reference. | Optional live model turn can be waived. |
| `plans/done/CI_PLAN.md` | Fast & Correct CI Pipeline | Complete | Status ledger: pytest xdist, cargo caching, wall-clock reduced from ~29m to target. | Maintain CI workflows. | None (minor sequence exceptions noted). |
| `plans/done/CONTEXT_HEADER_TOOLS_PLAN.md` | Context Header Toggles & Quieter Rows | Complete (Stale Header) | Header says "proposed (2026-10-02)", but chip toggles and tool rows landed in Ratatui client. | Retain as historical record. | Header is stale; implementation is complete. |
| `plans/done/CONTEXT_SECTIONS_PLAN.md` | Context Header Sections & Inspectors | Complete (Ratatui Verified) | Status header (2026-10-06): Done for Ratatui via `nexus --dev chat` and unit tests; desktop unverified. | Retain in `done/`. | Desktop unverified but paused per `AGENTS.md`. |
| `plans/done/DESIGN_OVERHAUL_PLAN.md` | Ratatui Design Overhaul | Complete | Steps 1–12, 14, 15 complete; companion ledger `DESIGN_OVERHAUL_VERIFICATION.md`; user verified live. | Maintain current styling. | None. |
| `plans/done/GPT_LIVE_PLAN.md` | GPT Live Voice Calls (`/live`) | Complete (M1–M5; M6 Optional) | Status header: M1–M5 implemented on `feat/gpt-live`; offline tests pass; M6 optional; mic/live soak unverified. | Retain in `done/`. | Live soak unverified; M6 optional. |
| `plans/done/GROK_BUILD_TUI_PLAN.md` | Grok Build Look & Terminal Interaction | Complete (Ratatui Shipped) | Status ledger: Foundation, projection, appearance controls, Mermaid fallback pass PTY tests; web check failed twice. | Maintain Ratatui client. | Web check failures waived due to web deprecation. |
| `plans/done/MCP_SEARCH_PLAN.md` | MCP Tool Search (`McpSearch`/`McpCall`) | Complete (Stale Header) | Header says "proposed, 2026-10-04", but dynamic tool search and bridging landed in `nexus/mcp/`. | Retain as historical record. | Header is stale; implementation shipped. |
| `plans/done/NONBLOCKING_SUBAGENTS_PLAN.md` | Non-blocking Persistent Subagents | Complete (Phases 1–8 Offline) | Status header: Phases 1–8 implemented offline 2026-10-20; Q1–Q8 design questions recorded as open non-blockers. | Retain in `done/`. | Live provider soak unverified; Q1–Q8 open. |
| `plans/done/SESSION_TITLE_PLAN.md` | Model Tiers, Titles & Subagent Tiers | Complete | Tier settings, low-tier background title generator, and subagent allowed tiers fully implemented. | Maintain title generation. | None. |
| `plans/done/SHELL_PLAN.md` | Shell Architecture & Persistent Jobs | Complete (Phases P0–P7) | Status header: P0–P7 implemented and verified across cited suites (jobs tab, persistent jobs, restart recovery). | Maintain shell subsystem. | None. |
| `plans/done/TUI_LOCAL_INTERACTION_PLAN.md` | Native TUI Responsiveness | Complete | Status header: user-authorized responsiveness and O(changed content) rendering implemented and verified. | Maintain performance. | None. |

#### Partial / Paused Plans (5 Files)
| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/done/CLAUDE_CODE_ZMX_PLAN.md` | Interactive Claude Code Workers | Partial | Status header: worker, host/native UI (5C except human handoff), crash recovery done; handoff & live IDs open. | Defer or scope PTY handoff. | Live Haiku run done; Sonnet/Opus live IDs & handoff open. |
| `plans/done/DESKTOP_OVERHAUL_PLAN.md` | Desktop (GPUI) Overhaul | Partial (Paused) | Status header: Phase 1 & 2 complete; Phase 3 open; desktop work paused per `AGENTS.md:8`. | Leave paused. | Development paused; do not modify `rust/desktop/`. |
| `plans/done/RATATUI_DESIGN_MOCKUPS_PLAN.md` | Single-Page Settings & Mockups | Partial | Status header: single-page Settings landed; Phase 6 keyboard navigation polish open. | Polish keyboard navigation. | Phase 6 polish remaining when prioritized. |
| `plans/done/SUBAGENT_USAGE_ROUTING_PLAN.md` | Live Tiers & Usage-Aware Routing | Partial | Status header: steps B–E implemented offline (`usage_snapshot.py`, runner); steps A, F–I open; live unverified. | Complete TUI rendering (F) and docs (G–I). | Live quota gate validation unverified against real provider. |
| `plans/done/WEB_TUI_PARITY_PLAN.md` | Web / Native TUI Parity | Partial (Web Deprecated) | Status header: second pass finished; 21 unchecked items; web client deprecated per `AGENTS.md`. | Close/waive remaining items. | Confirm formal waiver of remaining 21 web items. |

#### Stale / Superseded Plans (3 Files)
| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/done/GROK_TUI_PLAN.md` | Grok-Style Native TUI (Initial) | Stale / Superseded | Superseded by `GROK_BUILD_TUI_PLAN.md` (which completed theme and Mermaid integration). | Retain as historical record. | None. |
| `plans/done/SETTINGS_REVAMP_PLAN.md` | Settings Revamp (Initial) | Stale / Superseded | Explicitly marked superseded by `plans/RATATUI_DESIGN_MOCKUPS_PLAN.md` in plan header. | Retain as historical record. | None. |
| `plans/done/TUI_COMPONENT_REDESIGN_PLAN.md` | Native TUI Component Redesign | Stale / Superseded | Partial migration; superseded by subsequent Design Overhaul and Mockups plans. | Retain as historical record. | None. |

#### Non-Plan Artifacts in Done (4 Files)
| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/done/DESIGN_OVERHAUL_VERIFICATION.md` | Design Overhaul Verification | Verification Ledger | Companion test verification ledger to `DESIGN_OVERHAUL_PLAN.md` (recorded test results). | Retain as verification record. | None (non-plan artifact). |
| `plans/done/GPT_LIVE_CODEX_REFERENCE.md` | GPT Live Codex CLI Reference | Reference Notes | Reverse-engineering notes from reading OpenAI Codex CLI source (`openai/codex`). | Retain as reference. | None (non-plan artifact). |
| `plans/done/GPT_LIVE_PROTOCOL_NOTES.md` | GPT Live Protocol Notes | Research Notes | Status header: "research notes, nothing implemented" (observed realtime API behaviors). | Retain as reference. | None (non-plan artifact). |
| `plans/done/IMPROVEMENTS_PLAN.md` | Nexus Improvements Survey | Survey / Backlog | Status header: "survey only. Nothing in this file has been implemented" (2026-10-08). | Retain as ideas backlog. | None (non-plan artifact). |

---

### 3.2 Category 2: In-Progress Plans (`plans/in-progress/` — 4 Files)

| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/in-progress/01-unified-sessions-and-handoffs.md` | Unified Sessions & Context Handoffs | Complete Under Waiver (P0–P6) | Plan header: P0–P6 complete under maintainer waiver of soak gate; default flipped to `handoff` for v2 configs; P7 optional. | Archive to `plans/done/`. | Maintainer decision to formally archive as done under waiver; P7 optional. |
| `plans/in-progress/02-manager-session-orchestration.md` | Manager Session Above Roots | In-Progress (Offline Verified; Disabled) | Status header: M1–M9 offline verified; disabled by default; real soak pending; §10 rethink proposed; active tree edits. | Resolve §10 architecture; execute live soak. | Live API tokens/spend needed for soak; §10 Live routing rethink open; feature disabled. |
| `plans/in-progress/03-session-names-and-recursive-context.md` | Session Names, Code Mode, Recursive Context | In-Progress (offline work reported; live validation and policy choices open) | Plan ledger reports CM1–CM8, N1–N2, C1–C5 offline verified; code mode default on; E1/RLM live trials and visual review pending. | Resolve U1/U3/U7 policy questions in the plan; then conduct live RLM/provider trials and visual review if approved. | User decision needed on U1/U3/U7; live trials require API-token/spend approval. |
| `plans/in-progress/04-nvidia-provider.md` | NVIDIA Hosted Provider | In-Progress (Offline Verified) | Status header: `NvidiaProvider` implemented and offline verified; live API validation pending credentials. | Test with credentials or formally waive live check. | Requires optional `NVIDIA_API_KEY` for live validation; can be waived. |

---

### 3.3 Category 3: Planning Documents (`plans/planning/` — 7 Files)

> **Important Classification Finding:** Files 04 through 09 below were fully implemented and verified on 2026-10-20. They are classified as **Complete (Misplaced in Planning)** and should eventually be moved to `plans/done/`.

| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/planning/04-skill-add-via-npx-skills.md` | Add Skill via `npx skills` | Complete (Misplaced in Planning) | Status header: implemented and verified (2026-10-20); 781 pytest passed, cargo tests passed, native visual checks passed. | Relocate to `plans/done/`. | Misplaced plan; fully implemented. |
| `plans/planning/05-mcp-server-add.md` | Add MCP Server from Settings | Complete (Misplaced in Planning) | Status header: implemented and verified (2026-10-20); host protocol `SettingsMcpAdd`, Ratatui MCP editor verified. | Relocate to `plans/done/`. | Misplaced plan; fully implemented. |
| `plans/planning/06-subagent-roster-and-settings-notice.md` | Subagent Model Names & History Notice | Complete (Misplaced in Planning) | Status header: implemented and verified in test suite (2026-10-20); 781 pytest passed; cache prefix byte stability verified. | Relocate to `plans/done/`. | Misplaced plan; live token metrics unverified (optional). |
| `plans/planning/07-root-only-subagent-spawning.md` | Root-Only Subagent Spawning | Complete (Misplaced in Planning) | Status header: implemented and verified (2026-10-20); child runtimes restricted from spawning subagents; 781 pytest passed. | Relocate to `plans/done/`. | Misplaced plan; fully implemented. |
| `plans/planning/08-at-mention-folders.md` | `@` Directory Completion in Composer | Complete (Misplaced in Planning) | Status header: implemented in code, unit/PTY verified (2026-10-20), visual check in native capture artifacts passed. | Relocate to `plans/done/`. | Misplaced plan; fully implemented. |
| `plans/planning/09-subagent-page-back-button.md` | Back Button in Subagent Header | Complete (Misplaced in Planning) | Status header: implemented and fully verified (2026-10-20) in Rust TUI, Python suite, and native visual captures. | Relocate to `plans/done/`. | Misplaced plan; fully implemented. |
| `plans/planning/manager-automatic-compaction.md` | Manager Automatic Compaction & Memory | Proposed / Unstarted | Status header: proposed (2026-10-23); Section 0 source audit confirms manager currently lacks summarizer and memory text. | Review architecture; decide thresholds & storage format. | Architecture decision required on threshold, storage format, and memory isolation. |

---

### 3.4 Category 4: Top-Level Proposals (`plans/` — 2 Files)

| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/REFERENCE_PROJECT_RECOMMENDATIONS.md` | Reference Project Recommendations | Proposal / Survey | Status header: "proposal, not a commitment" (2026-10-22); surveys CodexBar and grok-build for adoption into Nexus. | User/maintainer review to select ideas for dedicated plans. | Architecture decision: select candidate features; note P1 checkpoint overlap with worktrees. |
| `plans/SUBAGENT_CONTINUATION_PLAN.md` | Subagent Continuation Plan | In-Progress / Partial | Status header: v1 implemented in working tree (P0–P3, partial P4); Section 15 outcome ledger records open P4/P5 items. | Decide whether to close scope at v1 or pursue P5. | Architecture decision: decide if v1 is sufficient to close or if P5 (prompt caching) should be tackled. |

---

### 3.5 Category 5: Worktree Sessions (`plans/worktree-sessions/` — 11 Files)

| Document Path | Title / Area | Assessed Status | Basis (Evidence / Repo Refs) | Next Step | User Attention |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plans/worktree-sessions/README.md` | Worktree Sessions: Plan Index | Complete / Index | Revised 2026-10-23; indexes sequential implementation of 01–10; records simplified architecture decisions. | Maintain as architecture index. | None. |
| `plans/worktree-sessions/01-shared-daemon.md` | One Daemon, Many Projects | Complete (Phases F0–F1) | Implementation ledger (2026-10-22): protocol v4, lazy project hosts, process leases, connection focus. | Maintain shared daemon foundation. | None. |
| `plans/worktree-sessions/02-per-session-root.md` | Per-Session Roots & Isolated Storage | Complete (Phase F2) | Implementation ledger: protocol v5, per-session directories, isolated caches and logs. | Maintain per-session root isolation. | None. |
| `plans/worktree-sessions/03-session-worktrees-and-turn-commits.md` | Session Worktrees & Turn Commits | Implemented (Active Naming Followup) | Implementation ledger: protocol v6, turn commits; uncommitted working-tree edits present for user-named branch slugs. | Verify and commit user-named worktree followup. | Active uncommitted edits in `nexus/agents/session_worktrees.py` and `tests/test_worktree_session_names.py`. |
| `plans/worktree-sessions/04-checkpoints-for-main-checkout.md` | Checkpoints for Main Checkout | Complete (Phase F4) | Implementation ledger: hidden Git checkpoints for shared checkout turns; verified by suite. | Maintain checkpointing backend. | None. |
| `plans/worktree-sessions/05-subagent-worktrees.md` | Subagent Worktrees | Implemented (Temporarily Disabled) | Status: implemented under simplified scope; active working-tree edits temporarily disable subagent worktrees pending naming settlement. | Re-enable subagent worktrees once naming is settled. | Feature temporarily disabled by design decision; requires re-enabling. |
| `plans/worktree-sessions/06-merge-queue-and-landing.md` | Merge Queue and Landing | Complete (Phase F6 Simplified) | Implementation ledger: local fast-forward integration upon explicit user confirmation; no background pushing. | Maintain landing safety invariants. | None. |
| `plans/worktree-sessions/07-setup-cleanup-recovery.md` | Setup, Cleanup, and Recovery | Complete (Phase F7) | Implementation ledger: safe teardown, orphan recovery, refusal on dirty files, recreate on next turn. | Maintain cleanup logic. | None. |
| `plans/worktree-sessions/08-tui-and-host-protocol.md` | TUI and Host Protocol for Worktrees | Complete (Active UI Additions) | Three-mode session modal implemented; working tree contains active uncommitted UI additions (`worktree_bar.py`, `worktree_pages.py`). | Verify and commit active UI additions. | Active uncommitted UI edits in working tree. |
| `plans/worktree-sessions/09-config-security.md` | Minimal Configuration and Git Safety | Complete | Implemented: project machine preference persists; git safety boundaries and diff limits enforced. | Maintain security posture. | None. |
| `plans/worktree-sessions/10-testing-and-rollout.md` | Focused Delivery and Acceptance | Complete and Verified | Validation ledger (2026-10-23): 743 passed tests in 560.44s across checkpoints, worktrees, and security. | Maintain test suite coverage. | None. |

---

## 4. Verification & Integrity Checklist

- [x] **File Existence:** Created file at `plans/PLAN_STATUS_REVIEW.md`.
- [x] **Inventory Coverage:** Exactly 48 source markdown files inventoried across the five designated categories (24 + 4 + 7 + 2 + 11 = 48). The report itself is omitted from the table to prevent self-referential drift.
- [x] **Integrity Preserved:** No existing plans were edited, renamed, or relocated.
- [x] **Assessment Posture:** Unverified live gates (NVIDIA credentials, real-audio soak, RLM trials) are distinguished as assessments, not assertions.
