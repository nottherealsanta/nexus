import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Example session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/project/prj_1/worktrees": { body: { worktrees: [{ path: "/work/demo", branch: "feature/sidebar", main: true }] } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

describe("session sidebar location", () => {
  test("desktop sidebar tabs are icon-only with accessible names and hover labels", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:tab-sessions");
    for (const [id, label] of [["sessions", "Sessions"], ["files", "Files"], ["git", "Git"]]) {
      expect(app.shows(`label:${label}`)).toBe(true);
      expect(app.texts().includes(label)).toBe(false);
      await app.hover(`id:tab-${id}`);
      await app.waitUntil(() => app.texts().some((text) => text.startsWith(label)));
    }
    await app.press("id:tab-files");
    await app.waitFor("id:files-header");
  });

  test("shows the branch, with the project left to the rail", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:⌥ feature/sidebar");
    expect(app.shows("id:session-place-ses_1")).toBe(true);
    expect(app.shows("id:session-group")).toBe(false);
    expect(app.texts().includes("demo")).toBe(false);
    expect(app.shows("id:new-session-row")).toBe(true);
    expect(app.shows("id:chip-archived")).toBe(true);
    const labels = app.texts();
    expect(labels.filter((text) => text === "Show archive").length).toBe(1);
    expect(labels.indexOf("New session")).toBeLessThan(labels.indexOf("Show archive"));
    expect(app.shows("id:session-search")).toBe(true);
  });
});

describe("sidebar selection highlight", () => {
  test("the selected session row and project tile share the active block and left bar", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:session-active-ses_1")).toBe(false);
    await app.press("id:session-ses_1");
    await app.waitFor("id:session-active-ses_1");
    expect(app.shows("id:rail-active-prj_1")).toBe(true);
  });
});
