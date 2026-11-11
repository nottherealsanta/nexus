# Terminal resources: Core contract

This is the Core terminal slice of [plans/02-terminal.md](../plans/02-terminal.md).
The browser resource is separate work. Both desktop clients expose the shared
terminal dock, resource list, creation, attachment, screen, control, input and
resize operations described below; see their READMEs for current limitations.

## Client behaviour

The desktop workspace has a bottom terminal dock below the composer and activity bar;
mobile never exposes it. The dock holds only terminals a user opened; nothing the
agent runs appears there. The terminal-shell activity indicator shows the terminal
count; clicking it opens or closes the dock, remembering its state. **``Ctrl+` ``** toggles the dock from anywhere, including while the
terminal screen owns the keys; **`Ctrl+~`** and the Shift spellings (**``Ctrl+Shift+` ``**,
**`Ctrl+Shift+~`**) are the same physical key and do the same. The shortcut is never
forwarded to the shell. Legacy terminals that collapse ``Ctrl+` `` and `Ctrl+~` into NUL
look like Ctrl+Space, which Nexus deliberately leaves to the shell instead of
hijacking. Closing with the shortcut or Hide detaches and never terminates.
The agent's running background bash jobs have their own **N shell** indicator, which
opens the Shells modal; the user can stop a running job there (↑/↓ select, `s` or **Stop**); see [tools.md](tools.md#background-bash-jobs). The distinct
**kernel** indicator has no number (there is exactly one kernel). The dock header
reads **Terminals (N)**.
Each terminal has a tab showing a running/exited status dot, its name, and a close
**×**; **+** creates a terminal. Select a tab to switch, and double-click it or use
the pencil control to rename. The compact toolbar orders New, Rename, Close,
Take/Release control, Copy output and Hide terminal panel. Controls show descriptive
hover tooltips in both clients; text actions use square brackets, including
`[Take control]` / `[Release control]` when terminal-safe lock glyphs are unavailable.
Taking or releasing control is always explicit.
Opening or creating a terminal takes control if available and focuses its screen;
otherwise a read-only banner offers **Take control**. Closing the dock detaches;
the tab close action terminates. Closing a running terminal first asks
`Close running terminal?` with **Close terminal** and **Cancel**. An exited
terminal can be closed without that warning. Closed resources are omitted from the
list; opening the dock prefers a running resource, falling back to an ended one.
An exit frame immediately marks its tab ended and disables input, even if an
older successful control request is still completing.

The screen receives input only while focused and while this attachment holds
control. Taking control never steals it from another attachment. A failed control
request is visible; reconnecting creates a new attachment and must reacquire
control before typing. Input is serialized and acknowledged before the next input; an
indeterminate input is reported, not replayed on a new attachment. A gap resets
the parser and visibly says `Terminal history was discarded; screen may be incomplete.`
The screen uses the retained replay and subsequent live output; this is not a
lossless screen checkpoint. Scrollback is bounded.

When the screen has focus, keyboard input, including Esc and Ctrl+C, belongs to
the terminal rather than global Nexus shortcuts, except the dock toggle above.
**Ctrl+]** returns focus to the resource list without sending that key to the
shell; from the list, Esc closes the panel. Closing the panel returns focus to
the composer. Paste goes only to the focused terminal, using bracketed paste when
requested by the application. Mouse interaction is owned by the terminal screen
when focused and by Nexus otherwise. Applications that request mouse reports receive them; hold Shift to select text instead. Selection and an explicit **Copy** action
are required before emitting OSC 52. Escape sequences in terminal output never
write the clipboard or open URLs or files by themselves. Resizing the screen
resizes the PTY only while this attachment has control. Interactive terminal contents
are never automatically added to model context.

### Explicit evidence capture

Both desktop clients offer **Attach selected output** beside Copy. The action
freezes selected rendered text, not raw escape-bearing output. A scrollable preview
shows the exact eventual draft payload, resource ID/name, project/workspace and
stream-sequence watermark, an untrusted-data warning and exact included/omitted
UTF-8 byte counts. Provenance is only those fields; no session, turn or tool-call
IDs are recorded or inferred from the current draft. At most 16 KiB of selected text is included, cut only at a code
point boundary. Counts refer to the selection; unavailable earlier terminal history
is disclosed without inventing raw-byte omission counts. Text is JSON-string encoded
so embedded fences or closing tags cannot masquerade as provenance. DEL/C1
characters are escaped too, keeping the preview byte-identical through composer
control-character filtering.

**Add to draft** appends that immutable payload, never replacing selected draft
text or sending it. An empty draft gets no leading separator. Cancel is inert;
normal Send/Queue/Steer uses the existing canonical message path. A changed or
read-only destination fails visibly, and late NSH selection replies from another
resource generation are discarded. Output, rename, close or reconnect cannot change
a preview or evidence already in a draft. This action grants no agent control.

## Resource and authority

Core owns each PTY, not a client; agent bash jobs are separate pipe processes
([tools.md](tools.md#background-bash-jobs)) and never create a terminal resource. A resource has an opaque `id`,
`project_id`, absolute creation `workspace`, optional `session_id`, `creator`
(client identity, not an agent), `status` (`running`, `exited`, `closed`, or
`ended`), creation and end timestamps, dimensions, exit code or terminating
signal where known, and current controller identity or null. A session directory
change never moves an existing terminal. Validate the chosen workspace against
the project and any associated session before spawning; reject mismatches.
List and individual-resource operations must enforce the same project access
checks as creation. IDs are not authorization credentials.

Terminal capacity errors do not fall back to anything else. The service admits at most 16
running terminals and retains at most 64 PTYs with replay. When that retention
limit is reached, the oldest finished resource without viewers is evicted from
memory; its persisted metadata remains listed, but its replay and attachment are
no longer available. If all retained resources are running or attached, creation
fails rather than disconnecting viewers.

On forced close Core drains available PTY output before the exit frame, bounded
to 256 reads of at most 16 KiB each per drain. A detached descendant that keeps
the PTY open cannot indefinitely block close: Core closes the master after a
bounded wait. Such a descendant may survive if it left the shell's process
group, and output it writes after the master is closed is not retained.

Terminal bytes are untrusted. They are not session events or model context. A
future explicit user attachment must become a normal visible message with
provenance and clipping, not a hidden context injection. No agent may attach as
controller or type in this version.

## Agent-control boundary

The terminal dock is a user-controlled surface, not an additional agent tool.
The approved `bash`, `bash_output` and `bash_kill` tools are the agent's only
execution path; they never create, read or type into a terminal resource.

| Operation | Current authority and scope |
| --- | --- |
| Start a command with `bash` | Existing tool permission checks, visible arguments and result; one invocation, not a terminal-control grant |
| Send raw keystrokes, paste or signals through the dock | User controller only; attachment-scoped, revoked on detach, overflow, disconnect or Core restart |
| Acquire another viewer's writer control | Rejected; the current controller must release it first |
| Clipboard, files, URLs or credentials requested by terminal output | Output grants no authority; clipboard copy requires an explicit user action, other actions are not executed by the terminal renderer |
| Destructive commands or credential-bearing input | Same host-user powers as a local shell; neither PTY parsing nor command allow patterns make interactive input safe |

Do not expose raw agent input by wrapping it in the `bash` permission matcher:
an interactive program can interpret arbitrary bytes, shell state can change,
and approval of one command cannot authorize an indefinite shell session.
Any future agent-control feature needs a separate reviewed grant contract
(resource/session scope, expiry, revocation, takeover, credential handling and
visible arguments/results). No such grant is implemented here. Captured output
is untrusted data, never instructions or implicit permission.

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
| `DELETE /api/terminal/{id}` | | explicitly terminate a running resource; an exited or ended one is marked `closed`, which clients omit from their lists. Idempotent: returns the `closed` metadata |

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
pyte is only an optional text snapshot, not a reconnect checkpoint. Its diagnostic
parser caps control sequences at 4,096 characters: oversized sequences are ignored
until their terminator without accumulating their payload or announcing a false
safe replay boundary. This does not modify raw output delivered to clients or
retained in the ring; diagnostic titles and snapshots may omit oversized controls. A client
that retains its parser state can resume after its last sequence. A fresh client
replays from the ring start. If output was discarded, the `gap` frame announces
the discarded byte count and carries base64 RIS (`ESC c`) plus current rows and
columns; apply the dimensions and reset before replaying. **This is lossy**:
reset and remaining bytes cannot reconstruct screen contents, modes or cursor
state that were established solely by discarded output. A running full-screen
program may need to redraw. The ring does not imply unlimited scrollback.

When a requested sequence has been discarded, or a viewer falls behind the
bounded outgoing queue, resynchronize from the retained ring. A `gap` carries
the retained floor, head and discard count. A slow viewer is immediately revoked, including its writer control, and its
stream receives an end sentinel that later output cannot overwrite. It must
reattach; it does not block PTY reads or other viewers. Core restart marks
persisted running metadata `ended`, with reason `core_restart`; it does not
resume a PTY, attachment, or input acknowledgment. Persisted controller identities
are cleared for every resource on restart, including already-exited resources.

## Diagnostics (not model context)

Terminals carry no tool, turn or job provenance, so they have no inspector lanes
and publish no `terminal.updated` events. Lifecycle timestamps and aggregate
control acquisition/release counts are persisted with the resource. Metadata
persisted by an older build with a `background_job_id` (a PTY that was an agent job)
is marked `closed` on load, so it leaves the list, and its tool provenance fields
are dropped.

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

`TERM=xterm-256color`. Limits are 16 running resources and 64 retained PTYs per
daemon, 16 attachments per resource, 16 KiB per input, a 1 MiB raw ring,
4,096 retained frames and 256
queued frames per viewer. An attachment that has not connected expires after
30 seconds. There is no idle timeout for a running shell. Explicit Close and
normal daemon shutdown signal the shell and current foreground process groups,
reap the shell and close descriptors. On abrupt Core death, closure of the PTY
master sends SIGHUP to its foreground group. Subprocess tests verify that a
foreground shell exits and a separately grouped foreground job receives SIGHUP
when Core is killed on macOS. Other platforms are **not verified**. This is a
kernel hangup guarantee, not forced termination of jobs that ignore SIGHUP on
abrupt Core death. Stray `nohup` and `setsid` processes
are out of scope. Ended metadata survives restart; output does not.
