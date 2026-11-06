# Terminal resources: Core contract

This is the Core terminal slice of [PLAN.md section 2](../PLAN.md#2-terminal-inside-nexus).
The browser resource and both terminal UIs are separate work. The client behaviour
below is the shared target, not a claim that either UI is complete. The TUI has
an initial list, creation, attachment, screen, control, input and resize slice;
see its README for the current controls and limitations.

## Client behaviour

The desktop workspace has a **Terminal** tab with a resource list and the selected
terminal's screen. Mobile does not expose this tab. The list shows each name,
creation workspace, status (`running`, `exited`, `closed`, or `ended`) and
controller identity, or `No controller`. **New**, **Rename**, **Close**, and
**Take control** / **Release control** are explicit actions. Selecting a resource
switches the screen without terminating the previous resource. Closing the panel
only detaches; **Close** terminates the selected resource. Closing a running
terminal first asks `Close running terminal?` with **Close terminal** and **Cancel**.
An exited terminal can be closed without that warning.

The screen receives input only while focused and while this attachment holds
control. Taking control never steals it from another attachment. A failed control
request is visible; reconnecting creates a new attachment and requires taking
control again. Input is serialized and acknowledged before the next input; an
indeterminate input is reported, not replayed on a new attachment. A gap resets
the parser and visibly says `Terminal history was discarded; screen may be incomplete.`
The screen uses the retained replay and subsequent live output; this is not a
lossless screen checkpoint. Scrollback is bounded.

When the screen has focus, keyboard input, including Esc and Ctrl+C, belongs to
the terminal rather than global Nexus shortcuts. **Ctrl+]** returns focus to the
resource list without sending that key to the shell; from the list, Esc closes
the panel. Paste goes only to the focused terminal, using bracketed paste when
requested by the application. Mouse interaction is owned by the terminal screen
when focused and by Nexus otherwise; selection and an explicit **Copy** action
are required before emitting OSC 52. Escape sequences in terminal output never
write the clipboard or open URLs or files by themselves. Resizing the screen
resizes the PTY only while this attachment has control. Terminal contents are
never automatically added to model context.

## Resource and authority

Core owns each PTY, not a client or `bash_jobs`. A resource has an opaque `id`,
`project_id`, absolute creation `workspace`, optional `session_id`, `creator`
(client identity, not an agent), `status` (`running`, `exited`, `closed`, or
`ended`), creation and end timestamps, dimensions, exit code or terminating
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

All routes below are under `/api/terminal`; JSON requests and replies
use the API's normal error envelope. Unknown or unauthorized resource IDs must
not reveal resource existence. Responses containing terminal state or output use
`Cache-Control: no-store`.

| Method and path | Request | Result |
| --- | --- | --- |
| `POST /api/terminal` | `project_id`, `workspace`, optional `session_id`, `client_id`, `rows`, `cols` | 201 and metadata; fail rather than silently choose a different workspace |
| `GET /api/terminal?project_id=…` | project identity | metadata list, including ended resources |
| `GET /api/terminal/{id}` | | metadata |
| `PATCH /api/terminal/{id}` | `name` | updated metadata; rename any resource, including ended resources |
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
Control changes are observable to viewers without recording typed bytes.

New and legacy resources default to `Terminal`. Names are trimmed, must contain
1–80 Unicode characters, and cannot contain Unicode control, format, surrogate,
private-use or unassigned characters. Renames are persisted and live attachments
receive a `rename` frame with the new `name`.

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
A stream begins with replay strictly after `after`, and then
continues live with no race between snapshot and subscription.

Core retains a bounded raw output ring, cut at UTF-8 and parser boundaries;
pyte is only an optional text snapshot, not a reconnect checkpoint. A client
that retains its parser state can resume after its last sequence. A fresh client
replays from the ring start. If output was discarded, the `gap` frame announces
the discarded byte count and carries base64 RIS (`ESC c`) plus current rows and
columns; apply the dimensions and reset before replaying. **This is lossy**:
reset and remaining bytes cannot reconstruct screen contents, modes or cursor
state that were established solely by discarded output. A running full-screen
program may need to redraw. The ring does not imply unlimited scrollback.

When a requested sequence has been discarded, or a viewer falls behind the
bounded outgoing queue, resynchronize from the retained ring. A `gap` carries
the retained floor, head and discard count. A slow viewer is disconnected and
must reattach; it does not block PTY reads or other viewers. Core restart marks
persisted running metadata `ended`, with reason `core_restart`; it does not
resume a PTY, attachment, or input acknowledgment.

## Safety and lifecycle requirements

Terminal routes require the daemon bearer token; the current tokenless
non-loopback mode rejects every
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

`TERM=xterm-256color`. Limits are 16 resources per daemon, 16 attachments per
resource, 16 KiB per input, a 1 MiB raw ring, 4,096 retained frames and 256
queued frames per viewer. An attachment that has not connected expires after
30 seconds. There is no idle timeout for a running shell. Explicit Close and
normal daemon shutdown signal the shell and current foreground process groups,
reap the shell and close descriptors. On abrupt Core death, closure of the PTY
master sends SIGHUP to its foreground group. A subprocess test verifies that a
foreground shell exits when Core is killed on macOS; a separately grouped job
and other platforms are **not verified**. Stray `nohup` and `setsid` processes
are out of scope. Ended metadata survives restart; output does not.
