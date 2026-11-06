# Terminal resources: proposed Core contract

**Design only; not implemented or verified.** This is the first terminal slice
of [PLAN.md section 2](../PLAN.md#2-terminal-inside-nexus). The browser resource
and both terminal UIs are separate work. These routes must not be advertised as
available until implemented and tested.

## Resource and authority

Core owns each PTY, not a client or `bash_jobs`. A resource has an opaque `id`,
`project_id`, absolute creation `workspace`, optional `session_id`, `creator`
(client identity, not an agent), `status` (`running`, `exited`, `closed`, or
`interrupted`), creation and end timestamps, dimensions, exit code or terminating
signal where known, and current controller identity or null. A session directory
change never moves an existing terminal. Validate the chosen workspace against
the project and any associated session before spawning; reject mismatches.
List and individual-resource operations must enforce the same project access
checks as creation. IDs are not authorization credentials.

Terminal bytes are untrusted and are not session events or model context. A
future explicit user attachment must become a normal visible message with
provenance and clipping, not a hidden context injection. No agent may attach as
controller or type in this version.

## HTTP operations

All routes below are proposed under `/api/terminal`; JSON requests and replies
use the API's normal error envelope. Unknown or unauthorized resource IDs must
not reveal resource existence. Responses containing terminal state or output use
`Cache-Control: no-store`.

| Method and path | Request | Result |
| --- | --- | --- |
| `POST /api/terminal` | `project_id`, `workspace`, optional `session_id`, `client_id`, `rows`, `cols` | 201 and metadata; fail rather than silently choose a different workspace |
| `GET /api/terminal?project_id=…` | project identity | metadata list, including ended resources |
| `GET /api/terminal/{id}` | | metadata |
| `POST /api/terminal/{id}/attach` | `client_id` | new opaque attachment ID, initially read-only; an attachment belongs to one stream connection |
| `GET /api/terminal/{id}/stream?attachment_id=…` | optional `after` sequence | versioned stream described below |
| `POST /api/terminal/{id}/control` | `attachment_id`, `action` (`acquire` or `release`) | updated controller; acquisition while another attachment holds it returns 409, never steals |
| `POST /api/terminal/{id}/input` | `attachment_id`, `input_seq`, base64 `data` | acknowledgment of the input sequence; controller only |
| `POST /api/terminal/{id}/resize` | `attachment_id`, `rows`, `cols` | updated dimensions; controller only |
| `POST /api/terminal/{id}/signal` | `attachment_id`, allowlisted `signal` | accepted signal; controller only |
| `POST /api/terminal/{id}/detach` | `attachment_id` | detach and release its control, without killing the process |
| `DELETE /api/terminal/{id}` | | explicitly terminate; idempotently return ended metadata |

An attachment is revoked on stream disconnect, explicit detach, or server
shutdown. It cannot be reused for a later connection. On reconnect create a new
attachment and reacquire control explicitly; control never transfers implicitly.
Stale attachments cannot input, resize or signal. A stream that never connects
must expire: the implementation must document and test a bounded attach lease.
Control changes should be observable to all viewers and recorded as bounded
diagnostic metadata, without recording typed bytes.

Input sequence numbers start at 1 per attachment and increase by exactly one.
Core serializes writes per attachment, records the last accepted sequence and a
digest of that request, and acknowledges only after accepting the entire input
for the PTY. Retrying the last accepted sequence with identical bytes returns the
same acknowledgment without writing again; a different payload, an older
sequence, or a gap is rejected. The client must not issue the next input until
it has an acknowledgment. A failed or partially written PTY input is not safely
retryable: revoke the attachment and report an indeterminate outcome rather
than silently duplicate bytes. This guarantee is scoped to a live attachment;
a lost connection or Core restart does not allow a client to replay an
unacknowledged command on a new attachment.

## Stream v1 and bounded replay

Use a dedicated SSE stream of JSON frames, not `/api/event`. Each frame has
`version: 1`, a resource-local monotonic `seq`, a `type`, and type-specific
payload. Output frames carry base64 bytes, never independently decoded chunks;
clients feed a streaming UTF-8/VT parser. Sequence numbers cover all frames,
including lifecycle, control and discard notices. Event IDs carry the sequence.
A stream begins with a checkpoint or a replay strictly after `after`, and then
continues live with no race between snapshot and subscription.

Core retains a bounded raw output ring and a pyte screen checkpoint, with the
private-SGR and alternate-screen fixes noted in the spike. A checkpoint must
represent the screen, cursor, attributes, active buffer, relevant terminal modes
and incremental decoder/parser state at its sequence boundary, or explicitly
report what it cannot restore. Its encoding and compatibility with both client
parsers must be verified before implementation is considered complete. Raw
bytes after that boundary are replayed in order. Neither a screen checkpoint nor
raw ring implies unlimited scrollback.

When a requested sequence has been discarded, or a viewer falls behind the
bounded outgoing queue, emit `gap` with the requested cursor, retained floor,
head and discard count, then require resynchronization from a fresh atomic
checkpoint and following bytes. Announce scrollback loss rather than pretending
it was retained. A slow viewer must not block PTY reads or other viewers; close
its stream if it cannot receive the gap notice. Never silently skip output.
Core restart marks persisted running metadata `interrupted`; it must not claim
to resume a PTY, checkpoint, attachment, or input acknowledgment.

## Safety and lifecycle requirements

Terminal routes are enabled only on loopback or with independently verified
authentication; the current tokenless non-loopback mode must reject every
terminal operation, including stream and metadata. Validate browser origins and
protect mutations against cross-origin requests; query-string tokens must not
leak in logs. Do not rely on a remote peer address alone behind a proxy. Do not
ship remote authenticated access until its authentication and transport are
actually enforced and tested.

Clipboard writes (including OSC 52), file access and URL launches requested by
terminal escape sequences must be disabled or gated behind explicit user action
in **both** renderers; raw output is not authorization. Core must not execute
these actions while parsing checkpoints. The shell runs with the user's
permissions and is not a sandbox.

Set a defined `TERM`, validate dimensions and bound resource count, attachments,
input, replay, scrollback and per-viewer queues. Define concrete limits and the
idle policy in the implementation, reject excess work, and test them. Explicit
Close and normal daemon shutdown terminate the process group gracefully, then
force-stop after a bounded grace period, reap children and close descriptors.
Abrupt Core death needs an OS-backed orphan strategy, not just an in-process
shutdown callback. Ended metadata survives restart; output persistence is not
required. Platform support and any unverified cleanup behavior must be stated
honestly.
