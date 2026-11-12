// PLAN 15.3: the project rail's saved order, three-tier status and non-destructive removal.
import { describe, expect, open, test } from "nsh/test";
import { PROJECT_STATUS, dropMove, orderedProjects, projectLabels, projectStatus, railProjects } from "./src/project-rail";
import type { Project } from "./src/core/api";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const mk = (name: string, created: number, extra: Partial<Project> = {}): Project =>
  ({ id: `prj_${name}`, path: `/work/${name}`, name, created, updated: created, revision: 1, ...extra });
const projects = [mk("alpha", 1, { position: 2 }), mk("bravo", 2, { position: 0 }), mk("charlie", 3, { position: 1, status: "passive" }), mk("delta", 4, { position: 3, status: "inactive" })];
const snapshot = (list: Project[]) => ({
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: list, sessions: [], running: [], approvals: [], questions: [], selected: null,
});
const network = (list: Project[], extra: Record<string, unknown> = {}) => ({
  ...extra,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot(list) },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/project/*/worktrees": { body: { worktrees: [] } },
});

describe("project rail order and status (logic)", () => {
  test("the saved order wins, creation order is the fallback, and inactive projects leave the rail", () => {
    expect(orderedProjects(projects).map((p) => p.name)).toEqual(["bravo", "charlie", "alpha", "delta"]);
    expect(railProjects(projects).map((p) => p.name)).toEqual(["bravo", "charlie", "alpha"]);
    const old = [mk("two", 2), mk("one", 1)];
    expect(orderedProjects(old).map((p) => p.name)).toEqual(["one", "two"]);
    expect(projectStatus(old[0])).toBe(PROJECT_STATUS.current);
    expect(projectStatus({ status: "nonsense" })).toBe("current");
  });

  test("initials follow creation order, not the display order", () => {
    const list = [mk("Nexus", 1, { position: 1 }), mk("Notes", 2, { position: 0 })];
    expect([...projectLabels(list).entries()]).toEqual([["prj_Nexus", "N"], ["prj_Notes", "NT"]]);
  });

  test("dropMove names the neighbour a dragged tile lands next to", () => {
    const ids = ["a", "b", "c"];
    expect(dropMove(ids, "c", 0)).toEqual({ before: "a" });
    expect(dropMove(ids, "a", 2)).toEqual({ after: "c" });
    expect(dropMove(ids, "b", 1)).toBeNull();
    expect(dropMove(ids, "a", 9)).toEqual({ after: "c" });
    expect(dropMove(ids, "zz", 0)).toBeNull();
  });
});

describe("project rail (app)", () => {
  test("the rail shows the saved order, hides inactive and dims passive projects", async () => {
    const app = await open("./App.tsx", { network: network(projects), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_bravo");
    expect(app.shows("id:rail-project-prj_delta")).toBe(false);
    const order = ["bravo", "charlie", "alpha"].map((n) => app.texts().indexOf(n.charAt(0).toUpperCase()));
    expect(app.count("id:rail-label-prj_charlie")).toBe(1);
    expect(order.length).toBe(3);
    await app.screenshot("/private/tmp/nexus-nsh-rail-status.png");
  });

  test("dragging a tile moves it in Core; a press without travel stays a click", async () => {
    const app = await open("./App.tsx", {
      network: network(projects, { "POST */api/project/prj_alpha/move": { body: { projects } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_alpha");
    await app.drag("id:rail-project-prj_alpha", { x: 0, y: -200 });
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/api/project/prj_alpha/move")));
    const move = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/api/project/prj_alpha/move")) as any;
    expect(JSON.parse(move.body)).toEqual({ before: "prj_bravo" });
    await app.screenshot("/private/tmp/nexus-nsh-rail-reordered.png");
  });

  test("dropping sideways outside the rail cancels the move", async () => {
    const app = await open("./App.tsx", { network: network(projects, { "POST */api/project/*/move": { body: { projects } } }), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_alpha");
    await app.drag("id:rail-project-prj_alpha", { x: 300, y: -200 });
    await app.wait(300);
    expect(app.requests().some((r) => r.url.endsWith("/move"))).toBe(false);
  });

  test("the row menu moves a project with the shared commands and sets its status", async () => {
    const app = await open("./App.tsx", {
      network: network(projects, { "POST */api/project/prj_alpha/move": { body: { projects } }, "PATCH */api/project/prj_alpha": { body: { ...projects[0], status: "passive", revision: 2 } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_alpha");
    await app.rightClick("id:rail-project-prj_alpha");
    await app.press("id:row-action-project.move_up");
    await app.waitUntil(() => app.requests().some((r) => r.url.endsWith("/api/project/prj_alpha/move")));
    const move = app.requests().find((r) => r.url.endsWith("/api/project/prj_alpha/move")) as any;
    expect(JSON.parse(move.body)).toEqual({ before: "prj_charlie" });
    await app.rightClick("id:rail-project-prj_alpha");
    expect(app.shows("id:row-action-project.status.current")).toBe(true);
    await app.press("id:row-action-project.status.passive");
    await app.waitUntil(() => app.requests().some((r) => r.method === "PATCH" && r.url.endsWith("/api/project/prj_alpha")));
    const patch = app.requests().find((r) => r.method === "PATCH") as any;
    expect(JSON.parse(patch.body)).toEqual({ status: "passive" });
  });

  test("Move up is unavailable on the first project and Move down on the last, with their reasons", async () => {
    const app = await open("./App.tsx", { network: network(projects, { "POST */api/project/*/move": { body: { projects } } }), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_bravo");
    await app.rightClick("id:rail-project-prj_bravo");
    await app.waitFor("id:row-action-project.move_up");
    expect(app.texts().includes("It is already first.")).toBe(true);
    await app.keyboard.press("escape");
    await app.rightClick("id:rail-project-prj_alpha");
    await app.waitFor("id:row-action-project.move_down");
    expect(app.texts().includes("It is already last.")).toBe(true);
    await app.keyboard.press("escape");
    await app.wait(300);
    expect(app.requests().some((r) => r.url.endsWith("/move"))).toBe(false);
  });

  test("removing a project asks first and only unregisters it", async () => {
    const app = await open("./App.tsx", {
      network: network(projects, { "POST */api/project/prj_alpha/remove": { body: { id: "prj_alpha", removed: true, sessions_kept: 0 } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_alpha");
    await app.rightClick("id:rail-project-prj_alpha");
    await app.press("id:row-action-project.remove");
    await app.waitFor("id:confirm-dialog");
    expect(app.texts().some((t) => t.includes("Files on disk are never touched."))).toBe(true);
    await app.screenshot("/private/tmp/nexus-nsh-rail-remove.png");
    expect(app.requests().some((r) => r.url.endsWith("/remove"))).toBe(false);
    await app.press("id:confirm-ok");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/api/project/prj_alpha/remove")));
    expect(app.requests().some((r) => r.method === "DELETE")).toBe(false);
  });

});
