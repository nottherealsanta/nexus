// Plan 39: a linked worktree's header shows only when it holds two or more listed sessions.
import { describe, expect, open, test } from "nsh/test";

const base = { model: "", permission: "ask", created: 1, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const mk = (id: string, directory: string, updated: number, extra: object = {}) => ({ ...base, id, project_id: "prj_1", directory, title: `Title ${id}`, updated, ...extra });
const network = (sessions: any[]) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions, running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/project/prj_1/worktrees": { body: { worktrees: [
    { path: "/work/demo", branch: "main", main: true },
    { path: "/work/demo-a", branch: "feature-a", main: false },
    { path: "/work/demo-b", branch: "feature-b", main: false },
  ] } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
});

describe("worktree header in the sessions list (plan 39)", () => {
  test("one session in a linked worktree has no header; two have one", async () => {
    const sessions = [mk("s_main", "/work/demo", 9), mk("s_a1", "/work/demo-a", 8), mk("s_a2", "/work/demo-a", 7), mk("s_b1", "/work/demo-b", 6)];
    const app = await open("./App.tsx", { network: network(sessions), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-s_b1");
    await app.waitFor("id:session-worktree-group");
    const texts = app.texts();
    expect(texts.includes("/work/demo-a")).toBe(true);
    // feature-b holds one session: no header, and its row's second line still names the branch.
    expect(texts.includes("/work/demo-b")).toBe(false);
    expect(texts.filter((t) => t === "⌥ feature-b").length).toBe(1);
    expect(app.shows("id:session-place-s_b1")).toBe(true);
  });

  test("the archived filter recounts the group", async () => {
    const sessions = [mk("s_main", "/work/demo", 9), mk("s_a1", "/work/demo-a", 8), mk("s_a2", "/work/demo-a", 7, { archived: 1 })];
    const app = await open("./App.tsx", { network: network(sessions), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-s_a1");
    expect(app.shows("id:session-worktree-group")).toBe(false);
    await app.press("id:chip-archived");
    await app.waitFor("id:session-s_a2");
    expect(app.shows("id:session-worktree-group")).toBe(true);
  });
});
