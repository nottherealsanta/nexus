// A session writing its handoff summary shows a Compacting row and takes no new messages.
import { describe, expect, test } from "nsh/test";
import { open } from "./open-bundle";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", status: "idle" };
const messages = [
  { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "do the thing" }], created: 1 },
  { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "Done." }], created: 2 },
];
const sse = (frames: string) => `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n${frames}`;
const frame = (id: number, type: string, data: unknown) => `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ id, type, session_id: "ses_1", cursor: `log_t:${id}`, data })}\n\n`;
const network = (events: string) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: sse(events), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: messages },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
  "GET */api/session/*": { body: session },
});

describe("compacting (handoff in progress)", () => {
  test("shows the row and locks the composer", async () => {
    const app = await open("./App.tsx", { network: network(frame(1, "session.handoff_started", { session_id: "ses_1", reason: "user" })), width: 1100, height: 800, platform: "web" });
    await app.waitFor("id:splash", { state: "gone" });
    await app.press("id:session-ses_1");
    await app.waitFor("id:compacting-row");
    await app.waitFor("text:Compacting · a new session opens soon");
    await app.waitFor("id:compacting-wave");
    await app.type("id:composer-input", "hello");
    await app.keyboard.press("enter");
    expect(app.requests().some((r) => r.method === "POST" && r.url.endsWith("/prompt"))).toBe(false);
  });
});
