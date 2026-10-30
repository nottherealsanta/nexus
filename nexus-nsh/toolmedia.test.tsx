// PLAN K8: an image a tool returned shows in the open tool row.
import { describe, expect, open, test } from "nsh/test";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const original = { id: "ses_0", project_id: "prj_1", directory: "/work/demo", title: "Original work", model: "", permission: "ask", created: 3, updated: 3, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", continued_to: "ses_1" };
const session = { continued_from: "ses_0", id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [session], running: [], approvals: [], questions: [], selected: null,
};
const summary = "## Goal\nFix the login bug.\n## State\nHalf done.\n## Decisions\nNone.\n## Next steps\nRun the tests.";
const messages = [
  { id: "m1", session_id: "ses_1", seq: 1, role: "user", delivery: "handoff", parts: [{ type: "text", text: `[Handoff from session ses_abcdef123456]\n\n${summary}` }], created: 1 },
  { id: "m2", session_id: "ses_1", seq: 2, role: "user", parts: [{ type: "text", text: "carry on" }], created: 2 },
];
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: { messages: [
    { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "screenshot please" }], created: 1 },
    { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "tool_call", id: "c1", name: "mcp_shot", arguments: "{}" }], created: 2 },
    { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "c1", output: "saved", media: [{ artifact: "art_1", mime: "image/png", name: "shot.png" }] }], created: 3 },
  ], more: false, oldest_seq: 1 } },
  "GET */api/session/ses_0/message*": { body: [] },
  "GET */api/session/ses_0/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_0/snapshot": { body: null },
  "GET */api/session/ses_0/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_0": { body: original },
  "GET */api/session/ses_1": { body: session },
  "GET */api/session/*": { body: session },
};



describe("Tool images (K8)", () => {
  test("a tool row that returned an image is open and shows it with its name", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:tool-media-c1");
    expect(app.texts().includes("shot.png")).toBe(true);
    expect(app.texts().some((t) => t.includes("1 image"))).toBe(true);
  });
});
