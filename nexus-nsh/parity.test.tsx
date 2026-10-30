import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Parent", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const child = { ...session, id: "ses_child", title: "Research", parent_id: "ses_1", parent_call_id: "call_1" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session, child], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: [
    { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "hello" }], created: 1 },
    { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "tool_call", id: "call_1", name: "session", arguments: '{"agent":"research","wait":false}' }], created: 2 },
    { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "call_1", output: "first line\nsecond line\nlast line" }], created: 3 },
  ] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
};

describe("timeline parity", () => {
  test("child result can be expanded without opening the child session", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:child-result-toggle");
    expect(app.texts().includes("Background")).toBe(true);
    expect(app.texts().includes("“Research”")).toBe(true);
    await app.press("id:child-result-toggle");
    await app.waitFor("text:Show less");
    expect(app.texts().join("\n").includes("last line")).toBe(true);
    await app.press("id:child-result-toggle");
    await app.waitFor("text:Show result");
  });
});
