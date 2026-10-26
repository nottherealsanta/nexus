# iOS session experience

This page describes the implemented SwiftUI session client. Runtime appearance,
VoiceOver, Dynamic Type extremes and real-device networking are **not verified**.
The gateway and host contracts remain authoritative.

## Navigation and connection

The same split-view selection drives the iPhone stack and iPad sidebar/detail.
A device pairs with a private Tailscale Serve HTTPS address and a short-lived code;
the credential is bound to that gateway and stored in device-only Keychain. The
connection sheet provides reconnect, scope labels and a confirmed forget action.
Revocation clears cached conversation state and returns to pairing. Forget also
removes local drafts; server-side revocation remains a separate gateway command.

A single allowed workspace is selected automatically. Workspace switching clears
the visible conversation and rejects late results from the old selection. Lists
show active sessions, title, state and message count. Local search covers only
loaded sessions; a host `truncated` flag is always announced. Empty lists, missing
workspace access, loading and connection errors have distinct states.

## Conversation

A validated full snapshot supplies the initial view. SSE `view` events carry the
existing host `ops` protocol; patches apply atomically with session/schema/cursor
validation. Sequence gaps are allowed by the host contract. A resync marker,
malformed patch, or dropped stream marks content stale and schedules a fresh
snapshot in three seconds. Backgrounding cancels the stream; returning foreground
refreshes before subscribing. An old request cannot overwrite a different session.

Messages and tools appear in event sequence order with stable IDs. Message cards
identify the speaker; tool cards identify the tool, status and target. Reasoning,
full tool arguments/results, context and every other field are accessible through
labelled expandable details, without JSON dumps or client-side text clipping.
Follow latest can be paused when reading history. Connection warnings identify
stale content and its last update time. Queued messages have their own details.

## Composer and approvals

Drafts persist per gateway/workspace/session. Failed or uncertain sends keep the
draft and explain that the user should inspect the transcript before retrying.
No write is retried automatically. A confirmed send clears only its original
unchanged draft, including when the user navigated elsewhere during the request.
Offline composition remains available. Read-only scope labels explain disabled
send/create controls. Command-Return sends on a hardware keyboard.

New session creates a UUID session using the existing `SessionOpen` command and
opens it only after confirmation. Sending uses `SessionEnqueue` in queue mode.
Stop uses `SessionCancel` with `drop_queue=false`. Pending permissions are visible
above the composer with their full request details. Devices with `approve` may
allow once or deny once; the host resolves races with other clients.

## Remaining work

Persistent approval rules, question-answer controls, attachments, richer Markdown
and diffs, dictation and live voice are not integrated. Background notifications
are not promised. Real-device testing must cover suspension, cellular/Wi-Fi
handoff, Tailscale off, host asleep, accessibility sizes and VoiceOver. See
[testing](TESTING.md) for evidence and the manual checklist.
