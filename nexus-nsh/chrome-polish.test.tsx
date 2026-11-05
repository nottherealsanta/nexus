import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Example session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, api_version: "1.0" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/project/prj_1/worktrees": { body: { worktrees: [{ path: "/work/demo", branch: "main", main: true }] } },
  "GET */api/session/ses_1/git": { body: { branch: "main", ahead: 0, behind: 0, changes: Array.from({ length: 17 }, (_, i) => ({ path: `f${i}.txt`, status: " M", staged: false, unstaged: true, added: 1, removed: 0 })) } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

describe("chrome polish", () => {
  test("a Core outage is said once: the banner, not the header", async () => {
    const app = await open("./App.tsx", { network: { ...network, "GET */api/info": { status: 502, body: { error: "Nexus Core is not running (no daemon.json)", code: "no_daemon" } } }, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:banner");
    expect(app.shows("id:header-status")).toBe(false);
  });

  test("one details toggle opens Session and Git without a separate right rail", async () => {
    const app = await open("./App.tsx", { network, width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    expect(app.shows("id:right-rail")).toBe(false);
    await app.press("id:toggle-right");
    await app.waitFor("id:right-sidebar");
    expect(app.shows("id:right-section-git-status")).toBe(true);
    expect(app.texts().includes("17")).toBe(true);
  });
});
