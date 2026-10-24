# 05. Subagent worktrees

Status: implemented under the simplified 2026-10-23 scope.

## Behavior

Session mode and subagent isolation are separate choices. The `subagent` tool
keeps a `worktree` parameter: omitted defaults on for a child whose selected tools
may edit, and off for read-only children. Explicit `true` or `false` overrides
that default, regardless of the session's three-mode preference.

An isolated child starts from the parent's current files, including uncommitted
changes. Reuse the existing authenticated worktree helpers. Its shell starts in
its own checkout. Save child changes in a Git commit when the child finishes.

Successful children return their changes automatically. Merge their branch into
an owned session branch. For a shared checkout, apply just the saved child diff
to working files, preserving HEAD and the index. If integration conflicts, keep
the child branch and worktree, leave parent edits intact, and report the reason.
Failed or cancelled children keep their saved partial work without integrating.

## Boundaries

No new role frontmatter, metadata format, merge queue, watcher, merger agent,
parent tool family or PR flow. Existing child continuation limits remain.
Subagents still cannot spawn subagents. Outside Git, automatic isolation is
unavailable; explicit requests report the reason. Cleanup is explicit in 07.

## Acceptance

Test the parameter's omitted/true/false cases and read-only selection through the
runner. Real Git tests cover the current-files baseline, child saves, two writers,
successful integration, failed children, conflicts, and shared HEAD/index safety.
Update agent/tool docs and report actual checks before committing this step.

## Verification — 2026-10-23

The adapter reuses v3 records and the existing Git helpers. Real repositories cover
parent leftovers, owned-branch merging, shared HEAD/index preservation, failed
children and concurrent/conflicting writers. Scripted Runtime tests exercise owned
and shared parents plus the explicit false override; runner tests cover read-only,
explicit true, child profiles, permission keys and result summaries. Focused peer
regression: 200 passed. Native presentation remains step 08. Existing worktree
children still cannot be continued. Successful child checkouts remain available
for explicit cleanup in 07.
