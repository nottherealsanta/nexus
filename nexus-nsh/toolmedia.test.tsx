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
  test("a tool row that returned an image stays folded, and shows it with its name once opened", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    expect(app.count("id:tool-media-c1")).toBe(0);
    await app.press("id:activity-group-tool-c1");
    await app.waitFor("id:tool-row-c1");
    expect(app.count("id:tool-media-c1")).toBe(0);
    expect(app.texts().some((t) => t.includes("1 image"))).toBe(true);
    await app.press("id:tool-row-c1");
    await app.waitFor("id:tool-media-c1");
    expect(app.texts().includes("shot.png")).toBe(true);
    expect(app.texts().some((t) => t.includes("1 image"))).toBe(true);
  });
});

const browserNetwork = {
  ...network,
  "GET */api/session/ses_1/message*": { body: { messages: [
    { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "check the page" }], created: 1 },
    { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "tool_call", id: "b1", name: "browser_exec", arguments: JSON.stringify({ code: "\nnew_tab('https://example.com')\nprint(page_info())" }) }], created: 2 },
    { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "b1", output: "ok" }], created: 3 },
    { id: "m4", session_id: "ses_1", seq: 4, role: "assistant", parts: [{ type: "tool_call", id: "b2", name: "browser_screenshot", arguments: JSON.stringify({ full: true }) }], created: 4 },
    { id: "m5", session_id: "ses_1", seq: 5, role: "tool", parts: [{ type: "tool_result", call_id: "b2", output: "saved", media: [{ artifact: "art_2", mime: "image/png", name: "browser-use-browser_screenshot.image" }] }], created: 5 },
  ], more: false, oldest_seq: 1 } },
};

describe("Browser tools", () => {
  test("browser rows read Browser and Screenshot with their subjects, like the TUI", async () => {
    const app = await open("./App.tsx", { network: browserNetwork, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    await app.press("id:activity-group-tool-b1");
    await app.waitFor("id:tool-row-b1");
    const texts = app.texts();
    expect(texts.includes("Browser")).toBe(true);
    expect(texts.some((t) => t.includes("new_tab('https://example.com')"))).toBe(true);
    expect(texts.includes("Screenshot")).toBe(true);
    expect(texts.some((t) => t.includes("full page"))).toBe(true);
    expect(texts.some((t) => t.includes("browser_exec") || t.includes("browser_screenshot"))).toBe(false);
  });
});
