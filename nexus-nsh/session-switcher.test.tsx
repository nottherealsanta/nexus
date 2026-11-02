import { describe, expect, open, test } from "nsh/test";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = (id: string, project: string, title: string, updated: number, extra: Record<string, unknown> = {}) => ({
  id, project_id: project, directory: project === "prj_1" ? "/work/demo" : "/work/other",
  title, model: "", permission: "ask", created: updated, updated, effort: null, agent: "build",
  parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", ...extra,
});
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [
    { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 },
    { id: "prj_2", path: "/work/other", name: "other", created: 2, updated: 3, revision: 1 },
  ],
  sessions: [
    session("ses_1", "prj_1", "Refactor loader", 2, { status: "busy" }),
    session("ses_2", "prj_2", "Other project task", 3),
  ],
  running: [], approvals: [], questions: [], selected: null,
};
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/project/prj_1/worktrees": { body: { worktrees: [{ path: "/work/demo", branch: "trunk", main: true }] } },
  "GET */api/project/prj_2/worktrees": { body: { worktrees: [{ path: "/work/other", branch: "trunk", main: true }] } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

describe("session switcher", () => {
  test("pressing the open project in the rail opens it in project scope, with the state glyph first", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_1");
    // The first press opens the project; pressing the one already open asks for a session in it.
    await app.press("id:rail-project-prj_1");
    await app.waitFor("id:session-ses_1");
    await app.press("id:rail-project-prj_1");
    await app.waitFor("id:session-switcher");
    expect(app.texts().includes("Search sessions · Project")).toBe(true);
    expect(app.shows("id:switch-session-ses_1")).toBe(true);
    expect(app.shows("id:switch-session-ses_2")).toBe(false);
    expect(app.shows("id:switch-glyph-ses_1")).toBe(true);

    await app.keyboard.press("tab");
    await app.waitFor("text:Search sessions · Global");
    expect(app.shows("id:switch-session-ses_2")).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:session-switcher")).toBe(false);
  });
});
