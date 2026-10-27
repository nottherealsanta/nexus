# Session inspector

A web page served by the daemon that shows every session at four levels of
detail, from the conversation down to the raw wire traffic. Code:
`nexus-core/nexus/inspector/index.html` (one static file, no build step).

```sh
nexus sessions inspector             # opens the browser
nexus sessions inspector ses_…       # opens one session
nexus sessions inspector --no-open   # prints the URL
```

The URL is `http://127.0.0.1:4747/inspector#token=…&session=…&level=…`. The
token is in the fragment, which browsers never send to the server. The page
itself holds no data and loads without a token. Every API call it makes sends
the token as a bearer header. The page is served with a strict
content-security policy: no external scripts, styles or connections.

## Layout

- **Sidebar.** Projects with their path, and each project's sessions as a tree
  (who launched whom). Each row shows the title, agent, model and whether the
  session was launched by an agent. A green dot means busy. It updates live from
  `/api/event`.
- **Session header.** Every session field: id, agent, model and where it came
  from, effort, permission, directory, who launched it (with a link to the parent
  and the launching tool call), children (links), and how many messages and events
  are stored. It also shows the snapshot origin and creation time, the agent's
  source path and fingerprint, and the `AGENTS.md` path and status.

## Levels (keys 1–4)

| Level | Shows | Source |
| --- | --- | --- |
| 1 Conversation | Saved messages: user and assistant text, reasoning summaries (with the size of the encrypted state), tool calls with each argument labelled, tool results with line and character counts. A `session` tool call links to the child it started. Every part has its JSON, including the provider's raw wire item. | `/api/session/{id}/message` |
| 2 Steps | One row per model call, grouped by turn: model, input item count, tools offered, HTTP status, time to first byte, total time, tokens in (cached) and out (reasoning), stop reason, and each tool run with its result and duration. | stored events |
| 3 Requests | A snapshot card (origin, agent source and fingerprint, instruction-file status, child catalog), the next request (what the model would see now), then every sent request as labelled sections: redacted headers, parameters, instructions (including the `# Instructions file` section), each input item (preview, expandable to JSON), and tools (name, description, schema). Response headers too. | `/api/session/{id}/context`, the session's snapshot summary, stored `wire_request` / `wire_response` |
| 4 Raw | Every stored event in order, with the time offset, type and a clipped preview; click a row for its full JSON. You can filter by type or content, and token deltas and provider SSE events are hidden by default (one checkbox). Also the messages and session record as stored. | `/api/session/{id}/event` |

Clipping is always announced. Long tool output shows its first 60 lines, with
the rest one click away. Previews say how many characters were cut. The Raw
table shows 1500 rows at a time ("show more"). Up to 50 000 events are loaded
per session.

## Limits

- Sessions from before the event log have messages only. Levels 2 and 3 show a
  note, and Conversation and Raw still work.
- Sessions from before stored snapshots show a `reconstructed` snapshot origin
  and the visible snapshot note in their instructions; the note says the prompt
  is not an exact historical record.
- Text is shown as plain text. Markdown is not rendered.
- Live updates re-render the open session when its events arrive. Events of a
  child show in the child's own page.
- Checked with Playwright (Chromium) in light and dark mode and at 390 px
  width. Not checked in Safari or Firefox.

## Code and kernels

The sidebar also lists live kernels. Opening one shows its creator, users,
cell count, assigned-variable previews and retained cells (last 100; omissions
announced). Conversation nests expandable `code.call` records beneath their
parent `code` call, including intermediate output the model did not request.
Code events also appear in Raw. The list updates after cells and deletions.

Verified with Playwright (Chromium, headless) against a scripted daemon: a
parallel `read` + `bash` cell with three nested calls (one caught error), a
finished background bash job with retained output, the kernel and job lists and
detail views, light and dark mode, and 390 px width. No console errors and no
document-level horizontal overflow. Screenshots:
`artifacts/inspector-kernels-light.png`,
`artifacts/inspector-kernels-dark.png`,
`artifacts/inspector-kernel-light.png`,
`artifacts/inspector-kernel-dark.png`,
`artifacts/inspector-kernels-narrow.png`,
`artifacts/inspector-kernel-narrow.png`,
`artifacts/inspector-job-light.png`,
`artifacts/inspector-job-dark.png` and `artifacts/inspector-job-narrow.png`.

The session header lists the next turn's `tools` and saved `disabled_tools`,
using “none” for an empty list.

## Bash jobs

The sidebar lists background bash jobs, running and finished, with their status
and command. Opening one shows the command, directory, session, status, exit
code, duration, output counts and the retained output. The list refreshes on
`bash.started` and `bash.finished`. Jobs are retained until the job limit
evicts the oldest finished one, so a job's output stays inspectable after it
finishes; the durable event log still has the finish tail after eviction.
