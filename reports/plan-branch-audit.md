# Branch review — 2026-11-07

Local history review only; branches with unique commits are preserved pending explicit deletion approval, as PLAN.md requires.

| Branch | Commits not in main |
| --- | ---: |
| `backup-local-37cc186` | 1 |
| `backup/local-main-20261005` | 6 |
| `chore/restore-release-version` | 1 |
| `design/void` | 1 |
| `design/void-impl` | 0 |
| `docs/ratatui-plan-hosted-results` | 2 |
| `docs/restructure-docs` | 2 |
| `feat/design-mockups` | 0 |
| `feat/gpt-live` | 23 |
| `feat/gpt-live-cli` | 19 |
| `feat/gpt-live-desktop` | 18 |
| `feat/gpt-live-helper` | 5 |
| `feat/grok-style-tui` | 0 |
| `feat/ios-session-experience` | 0 |
| `feat/message-queue-steer-interrupt` | 2 |
| `feat/provider-usage` | 1 |
| `feat/ratatui-prototype` | 52 |
| `feat/shared-daemon` | 2 |
| `feat/ui-polish-and-bundled-deps` | 12 |
| `feat/update-notice` | 1 |
| `feat/update-notice-0.1.3` | 1 |
| `feat/web-visual-refresh` | 1 |
| `feat/xai-supergrok` | 0 |
| `fix/agy-no-read-only-enforcement` | 2 |
| `fix/build-prompt-test` | 1 |
| `fix/claude-agent-provider` | 1 |
| `fix/claude-startup-and-diff-preview` | 1 |
| `fix/cli-loading` | 1 |
| `fix/complete-patch-release` | 1 |
| `fix/daemon-release-order` | 1 |
| `fix/default-model-client` | 2 |
| `fix/discovery-context-controls` | 3 |
| `fix/image-document-input` | 3 |
| `fix/mcp-settings-diagnostics` | 1 |
| `fix/model-thinking-context` | 1 |
| `fix/native-binary-needs-host` | 1 |
| `fix/native-redesign-and-runtime-recovery` | 1 |
| `fix/project-session-groups` | 5 |
| `fix/pty-snapshot-order` | 0 |
| `fix/settings-autosave-reset` | 3 |
| `fix/settings-revamp` | 1 |
| `fix/shared-agent-settings` | 1 |
| `fix/shell-mcp-ctrl-c` | 2 |
| `fix/simple-root-prompts` | 3 |
| `fix/simple-root-prompts-pr` | 1 |
| `fix/skills-mcp` | 1 |
| `fix/tui-clipboard-image` | 2 |
| `fix/tui-clipboard-image-pr` | 1 |
| `fix/tui-images-0.4.2` | 1 |
| `fix/update-uv-upgrade-flag` | 4 |
| `fix/voice-credentials-release-0216` | 1 |
| `live-pane-and-consent-fix` | 0 |
| `nexus/session/Check-subagent-reuse` | 0 |
| `nexus/session/Release-0-7-0` | 0 |
| `nexus/session/apple-container-option` | 0 |
| `nexus/session/build-prompt-change` | 0 |
| `nexus/session/candid-cougar` | 0 |
| `nexus/session/candid-violet-tern-895046` | 0 |
| `nexus/session/child-1-dacaf1` | 0 |
| `nexus/session/child-1-f1ac50` | 1 |
| `nexus/session/child-2-251748` | 1 |
| `nexus/session/exact-bison` | 1 |
| `nexus/session/exact-deer` | 0 |
| `nexus/session/image-preview-fix` | 0 |
| `nexus/session/lucky-ginger-dunlin-cc933d` | 0 |
| `nexus/session/major-gannet` | 0 |
| `nexus/session/mermaid-fix` | 0 |
| `nexus/session/mermaid-fix-2` | 0 |
| `nexus/session/mindful-egret` | 0 |
| `nexus/session/remove-notice-worktree` | 1 |
| `nexus/session/session-modal-redesign` | 1 |
| `nexus/session/still-moose` | 0 |
| `nexus/session/sturdy-dragonfly` | 0 |
| `nexus/session/test-test-test-worktree` | 0 |
| `nexus/session/worktree-name` | 0 |
| `nexus/wt/animate-status-dots` | 0 |
| `nexus/wt/code-mode-kernel` | 0 |
| `nexus/wt/explain-nexus-architecture` | 0 |
| `nexus/wt/list-llm-providers` | 0 |
| `nexus/wt/nsh-desktop-web-parity` | 1 |
| `nexus/wt/nsh-web-replacement` | 0 |
| `nexus/wt/orchestrator-one-child` | 0 |
| `nexus/wt/project-explorer` | 0 |
| `nexus/wt/project-switch` | 0 |
| `nexus/wt/quick-provider-list` | 0 |
| `nexus/wt/ratatui-image-debug` | 0 |
| `nexus/wt/subagent-context-briefs` | 1 |
| `nexus/wt/worktree-auto-delete-guidance` | 0 |
| `nexus10-osc7501` | 0 |
| `osc-7501-program-status` | 0 |
| `polish-timeline` | 0 |
| `release-please-98` | 1 |
| `release/0.4.0-prep` | 4 |
| `release/gpt-live-0.5.0` | 25 |
| `release/grouped-changes-0.3.2` | 20 |
| `release/patch-0.5.3` | 0 |
| `release/target-0.5.4` | 3 |
| `release/trigger-0.3.8` | 1 |
| `worktree-agent-a37ac9db7839666ee` | 0 |
| `worktree-agent-aee1118494a82411e` | 0 |
| `worktree-shared-daemon-plan` | 2 |

`git worktree list` contains only this checkout and the old Nexus reference checkout.
The previously listed `.claude/worktrees/` checkouts are no longer registered.

## Deleted 2026-11-11 (user-approved)

Recover with `git branch <name> <sha>`. Remote deletions: `origin/ci/nsh`, `feat/update-notice`, `fix/default-model-client`, `fix/native-binary-needs-host`.

```
feat/update-notice 9d900a945e386e3ee4d08c21fa66e16d183c358b
fix/default-model-client 053c3c3459361af2f3cd26044cedcf55c29984c1
fix/native-binary-needs-host 87550aa574e46d3440a8d9cb657d2eb91ffef1ca
design/void a64a88f00ec4e8dd96a1455614107b5ef5603703
design/void-impl b5241a3b11cbd8715e7068dd0659b133b204161b
polish-timeline d6b1f2c35a40489656d45a06b1408d87c4a0efeb
origin/ci/nsh 9c5a1277c0737f4f2e367028db20db54f64ec44c
origin/feat/update-notice 9d900a945e386e3ee4d08c21fa66e16d183c358b
origin/fix/default-model-client 053c3c3459361af2f3cd26044cedcf55c29984c1
origin/fix/native-binary-needs-host 87550aa574e46d3440a8d9cb657d2eb91ffef1ca
design/void-impl b5241a3b
feat/design-mockups 23247afe
feat/grok-style-tui e19e9403
feat/ios-session-experience 01fec6a8
feat/xai-supergrok 0aca0c40
fix/pty-snapshot-order ec66fced
live-pane-and-consent-fix 4443a9de
nexus/session/Check-subagent-reuse c7ae7a03
nexus/session/Release-0-7-0 c7ae7a03
nexus/session/apple-container-option 9de32cea
nexus/session/build-prompt-change c95c12dd
nexus/session/candid-cougar 11c652bb
nexus/session/candid-violet-tern-895046 c7ae7a03
nexus/session/child-1-dacaf1 60d70ba5
nexus/session/exact-deer 2b0af036
nexus/session/image-preview-fix 71f30cb4
nexus/session/lucky-ginger-dunlin-cc933d 60d70ba5
nexus/session/major-gannet b96c511b
nexus/session/mermaid-fix 688a0ae7
nexus/session/mermaid-fix-2 f2c62a27
nexus/session/mindful-egret 8cbd2112
nexus/session/still-moose b96c511b
nexus/session/sturdy-dragonfly b96c511b
nexus/session/test-test-test-worktree c7ae7a03
nexus/session/worktree-name 04d11384
nexus/wt/animate-status-dots 358d6ccc
nexus/wt/code-mode-kernel 18395bb6
nexus/wt/explain-nexus-architecture 64980716
nexus/wt/list-llm-providers 64980716
nexus/wt/nsh-web-replacement 7f247be5
nexus/wt/orchestrator-one-child e972fd82
nexus/wt/project-explorer 69ba540f
nexus/wt/project-switch 9c5d4447
nexus/wt/quick-provider-list 64980716
nexus/wt/ratatui-image-debug 64980716
nexus/wt/worktree-auto-delete-guidance c55af409
nexus10-osc7501 81b81e57
osc-7501-program-status e4c35ebf
polish-timeline d6b1f2c3
release/patch-0.5.3 8a693047
worktree-agent-a37ac9db7839666ee 64980716
worktree-agent-aee1118494a82411e 64980716
```
