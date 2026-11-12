// Plan 09 9.7: Ctrl+R searches the project's prompt history (shared with the TUI).
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
  "GET */api/project/prj_1/prompts*": { body: { prompts: [
    { text: "run the tests", image_count: 0 },
    { text: "write the release notes\nwith two lines", image_count: 0 },
    { text: "fix the login bug", image_count: 0 },
    { text: "fix the login bug", image_count: 0 },
  ] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1": { body: session },
  "GET */api/session/*": { body: session },
};

describe("Prompt history search (Ctrl+R)", () => {
  test("lists earlier prompts newest first, once each, filters, and fills the composer", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.url.includes("/prompts")));
    await app.keyboard.press("ctrl+r");
    await app.waitFor("id:picker");
    const texts = app.texts();
    expect(texts.includes("Prompt history")).toBe(true);
    expect(texts.filter((t) => t === "fix the login bug").length).toBe(1);
    expect(texts.includes("2 lines")).toBe(true);
    expect(texts.indexOf("run the tests")).toBeLessThan(texts.indexOf("fix the login bug"));
    await app.type("id:picker-input", "release");
    await app.waitUntil(() => !app.texts().includes("run the tests"));
    await app.keyboard.press("enter");
    await app.waitUntil(() => !app.shows("id:picker"));
    await app.waitUntil(() => app.value("id:composer-input") === "write the release notes\nwith two lines");
  });
});
