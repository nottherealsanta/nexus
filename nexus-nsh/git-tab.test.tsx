import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Example session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const change = (path: string, status: string, staged: boolean, unstaged: boolean, added = 0, removed = 0) => ({ path, status, staged, unstaged, added, removed });
const git = (changes: unknown[]) => ({ worktree: "/work/demo", branch: "feature/git", changes, branches: [{ name: "feature/git", local: true, current: true }, { name: "other-branch", local: true, current: false }] });
const network = (changes: unknown[]) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/project/prj_1/worktrees": { body: { worktrees: [{ path: "/work/demo", branch: "feature/git", main: true }] } },
  "GET */api/project/prj_1/git*": { body: git(changes) },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
});

describe("Git tab", () => {
  test("says No changes and never lists branches", async () => {
    const app = await open("./App.tsx", { network: network([]), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:tab-git");
    await app.press("id:tab-git");
    await app.waitFor("id:git-empty");
    expect(app.texts().includes("demo · feature/git")).toBe(true);
    expect(app.texts().includes("Branches")).toBe(false);
    expect(app.texts().includes("other-branch")).toBe(false);
  });

  test("summarises changes by top-level folder and expands a folder to its files", async () => {
    const changes = [change("plans/a.md", "M ", true, false, 10, 2), change("src/x.ts", "MM", true, true, 5, 1), change("src/y.ts", " M", false, true, 1, 0), change("README.md", "??", false, true)];
    const app = await open("./App.tsx", { network: network(changes), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:tab-git");
    await app.press("id:tab-git");
    await app.waitFor("id:git-summary");
    expect(app.texts().includes("4 files changed · +16 −3")).toBe(true);
    expect(app.texts().includes("2 staged · 2 unstaged · 1 untracked")).toBe(true);
    expect(app.shows("id:git-folder-src/")).toBe(true);
    expect(app.shows("id:git-staged-src/x.ts")).toBe(false);
    await app.press("id:git-folder-src/");
    await app.waitFor("id:git-staged-src/x.ts");
    expect(app.shows("id:git-unstaged-src/y.ts")).toBe(true);
    await app.press("id:git-folder-src/");
    expect(app.shows("id:git-staged-src/x.ts")).toBe(false);
  });
});
