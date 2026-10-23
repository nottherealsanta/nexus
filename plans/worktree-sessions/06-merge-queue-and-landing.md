# 06. Manually integrate a session when requested

Status: implemented in the host backend; end-to-end daemon/client verification is pending.

## Proposed behavior

A session keeps its work on its own branch. Nexus does not automatically change a
user branch or push changes. When the user asks to integrate a session, show the
session branch, target branch, commits, and diff; then perform the requested Git
operation with clear success or error reporting.

The implemented backend previews one idle, clean owned session against its
recorded local branch. Integration is allowed only from the target checkout on
that branch when it is clean and has no Git operation in progress. Confirmation
is bound by the host to the full preview state. The backend rechecks under the
session and worktree service locks, then fast-forwards to the reviewed session
commit. It refuses divergence, stale previews, conflicts, ignored-file
overwrites, and unsafe checkout state. The operation is local and never pushes.

## Boundaries

- No merge queue, background coordination, base watchers, sync agent, PR creation,
  push automation, merge strategy menu, checks framework, or automatic integration.
- Do not silently rewrite user changes. Use existing safe Git operations and show
  the target and resulting commit.
- Integrating one session does not trigger actions on other sessions.

## Acceptance

Without an explicit user request, user branches remain unchanged.
The host backend exposes a bounded preview and confirmed local integration
operation. Focused real-Git backend tests cover successful integration and
refusals. Native UI wiring and live daemon confirmation are not verified here.

## Docs

The host, security, session, and module-map documentation describe the backend
contract. CLI and native UI wiring are deferred to their own plan sections.

Verification: 276 combined backend/host/docs/layering checks passed, plus two
additional divergence/operation/foreign-branch cases. Ruff passed across `nexus`
and `tests`. Host tests use a real Runtime and exercise stale and reused tokens.
