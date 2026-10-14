# iOS UX plan (proposed)

Nothing is implemented. Follows the shared principles in
[surfaces.md](../../docs/surfaces.md#principles): present context clearly, render
from the host's reduced view, host text is untrusted, tolerate stale async results.

## Navigation

iPhone: tab/stack — **Sessions** → **Conversation** (sheet for approvals, details).
iPad: split view — sidebar sessions, conversation, optional inspector column.
Settings screen holds only host connections, devices, privacy and cache; Nexus
configuration is not edited from the phone (denied in [GATEWAY](GATEWAY.md#command-allowlist-per-phase-proposed)).

## Sessions

- "All sessions" = every **authorized** registered workspace the device may see,
  grouped by project, with linked worktrees grouped under their repo (the host
  provides `repo` and `worktree`, **current**).
- Search (title, project, branch) is **local and current-page only**: it filters
  the sessions already loaded from the host. The host has no session search,
  filter or pagination contract today (`ProjectSessionsList` takes no query and
  returns a single `truncated` flag), so search does **not** reach sessions the
  host omitted. Label it as such ("Search loaded sessions"). Server-side search
  needs a new host contract first. Row: title, workspace/branch, status
  (running, awaiting approval, done/unread via `completion_seq`), last activity.
- Explicit scope control for archived and trash; default active only. The UI says
  so ("Showing active sessions").
- If the host says `truncated` (the cap is 1,000 sessions, no paging), show
  "Showing first N sessions; search covers only these." Do not promise that
  refining search reveals omitted sessions. Never silently omit them.
- Host unreachable: show last-known list labelled with its age and "offline",
  read-only.

## Conversation

- Rendered from the host presentation snapshot and patches (stable ids keep rows
  from flickering). Streaming text appended as it arrives.
- **Tool calls show full labelled arguments, results, context and errors**, never a
  raw JSON dump (same sections as the other clients). Collapsed rows are summaries;
  expanding shows everything the host provided.
- **Clipping is always declared** ("+N more characters"). A "Load full" action
  appears only where the host supports a detail command; otherwise the clip is
  stated as final ([CONTRACT_GAPS §7](CONTRACT_GAPS.md#7-other)).
- Diffs: two-column on iPad/landscape, unified on narrow iPhone, with line wrap
  choice; monospace, selectable, copyable.
- Subagents open as their own page; running child cancel via `AgentCancel` in P2.
- Live calls render as the labelled Live card from durable events (P4).
- Reconnect: banner, then refetch the snapshot; never show stale state as live.

## Approvals and questions

- Sticky prompt above the composer, same choices the host offers (once/always/deny).
- Shows what will happen: tool, labelled arguments, target path/command, session.
- First responder wins: if another client answered, show "Answered elsewhere".
- Capability off → read-only banner explaining why.
- Notification of pending approvals while backgrounded is **not promised**
  (no APNs in the first release); say so in onboarding.

## Composer

- Send modes mirror the host: steer (default during a turn), queue, interrupt.
- Attachments (photo/file) are a P2+ item using a bounded upload route.
- Stop button: `SessionCancel`; with queued messages returns them to the draft
  (`return_queue`, **current**).

## Drafts and retries

- Drafts persist locally per session (survive relaunch), never auto-sent.
- **No automatic retry of non-idempotent writes** (send, approve). On a timeout
  the app shows "not confirmed" and asks the user to check the transcript before
  resending. Gateway idempotency keys ([CONTRACT_GAPS §5](CONTRACT_GAPS.md#5-write-idempotency))
  would later enable safe retry.
- Offline: compose and keep as draft; send is disabled until connected.

## Accessibility and polish (P5, but design from day one)

Dynamic Type (including accessibility sizes) with no truncation of critical
controls; VoiceOver labels and rotor headings for turns; announce streaming
completion, not every token; dark mode; hardware keyboard shortcuts on iPad
(send, stop, switch session); Reduce Motion; sufficient contrast for status
colors with non-color cues.

## Consent and media destination

Dictation and Live screens state, before first use, where audio goes (host for
dictation; OpenAI via the user's ChatGPT account for Live), what is retained, and
how to revoke. See [VOICE_AND_LIVE](VOICE_AND_LIVE.md).
