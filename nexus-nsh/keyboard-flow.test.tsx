// PLAN 15.4: the keyboard flow from an empty composer, and the projects overview it ends in.
import { describe, expect, open, test } from "nsh/test";
import { overviewRows, sessionStateWord } from "./src/project-rail";
import type { Project, Session } from "./src/core/api";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const project = (name: string, created: number, extra: Partial<Project> = {}): Project =>
  ({ id: `prj_${name}`, path: `/work/${name}`, name: name[0].toUpperCase() + name.slice(1), created, updated: created, revision: 1, ...extra });
const session = (id: string, projectName: string, title: string, updated: number, extra: Record<string, unknown> = {}) => ({
  id, project_id: `prj_${projectName}`, directory: `/work/${projectName}`, title, model: "", permission: "ask", created: updated, updated,
  effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", ...extra,
});
const projects = [project("alpha", 1, { position: 0 }), project("bravo", 2, { position: 1, status: "passive" }), project("charlie", 3, { position: 2, status: "inactive" })];
const sessions = [
  session("ses_a1", "alpha", "Alpha chat", 5), session("ses_a0", "alpha", "Alpha old chat", 1, { archived: 1 }),
  session("ses_b1", "bravo", "Bravo notes", 4), session("ses_c1", "charlie", "Charlie draft", 3),
];
const network = (extra: Record<string, unknown> = {}) => ({
  ...extra,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects, sessions, running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/project/*/worktrees": { body: { worktrees: [] } },
});
const launch = (extra: Record<string, unknown> = {}) => open("./App.tsx", { network: network(extra), width: 1100, height: 800, platform: "web", colorScheme: "dark" });

describe("projects overview rows (logic)", () => {
  test("saved order, active sessions as a tree, archived behind a toggle, inactive last", () => {
    const rows = overviewRows(projects, sessions as unknown as Session[], new Set());
    expect(rows.map((r) => r.kind === "session" ? `${r.last ? "└" : "├"}${r.id}` : r.kind === "project" ? r.id : r.kind === "toggle" ? `toggle:${r.count}:${r.expanded}` : r.kind === "group" ? r.title : r.kind))
      .toEqual(["prj_alpha", "├ses_a1", "toggle:1:false", "prj_bravo", "└ses_b1", "Inactive", "prj_charlie", "└ses_c1"]);
    const open = overviewRows(projects, sessions as unknown as Session[], new Set(["prj_alpha"]));
    expect(open.filter((r) => r.kind === "session").map((r) => (r as any).id)).toEqual(["ses_a1", "ses_a0", "ses_b1", "ses_c1"]);
  });
  test("a project without active sessions says so, and state words follow the TUI's precedence", () => {
    expect(overviewRows([project("z", 1)], [], new Set()).map((r) => r.kind)).toEqual(["project", "empty"]);
    expect(sessionStateWord({ archived: false, status: "idle" }, true, true, true)).toBe("needs input");
    expect(sessionStateWord({ archived: false, status: "busy" }, false, false, false)).toBe("running");
    expect(sessionStateWord({ archived: true, status: "idle" }, false, false, true)).toBe("archived");
    expect(sessionStateWord({ archived: false, status: "idle" }, false, false, true)).toBe("finished");
    expect(sessionStateWord({ archived: false, status: "idle" }, false, false, false)).toBe("idle");
  });
});

describe("keyboard flow from an empty composer (app)", () => {
  test("Left opens the session sidebar, again the project list (Up/Down select), again the overview", async () => {
    const app = await launch();
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "");
    await app.keyboard.press("left");
    await app.waitFor("id:left-sidebar");
    expect(app.shows("id:session-ses_b1")).toBe(true);
    await app.keyboard.press("left");
    await app.waitFor("id:project-cursor-prj_bravo");
    await app.keyboard.press("down");
    await app.waitFor("id:project-cursor-prj_charlie");
    await app.keyboard.press("up");
    await app.waitFor("id:project-cursor-prj_bravo");
    await app.keyboard.press("left");
    await app.waitFor("id:projects-overview");
    expect(app.shows("id:overview-row-0")).toBe(true);
  });

  test("Esc and Right step back out in reverse order", async () => {
    for (const out of ["escape", "right"]) {
      const app = await launch();
      await app.waitFor("id:composer-input");
      await app.type("id:composer-input", "");
      await app.keyboard.press("left"); await app.keyboard.press("left"); await app.keyboard.press("left");
      await app.waitFor("id:projects-overview");
      await app.keyboard.press(out);
      await app.waitUntil(() => !app.shows("id:projects-overview"));
      expect(app.shows("id:project-cursor-prj_bravo")).toBe(true);
      await app.keyboard.press(out);
      await app.waitUntil(() => !app.shows("id:project-cursor-prj_bravo"));
      expect(app.shows("id:session-ses_b1")).toBe(true);
      await app.keyboard.press(out);
      await app.keyboard.press("x");
      await app.waitUntil(() => app.value("id:composer-input") === "x");
    }
  });

  test("Left with text in the composer still moves the cursor", async () => {
    const app = await launch();
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "hello");
    await app.keyboard.press("left");
    await app.keyboard.press("x");
    await app.waitUntil(() => app.value("id:composer-input") === "hellxo");
    expect(app.shows("id:projects-overview")).toBe(false);
  });

  test("the overview shows the tree, passive dimmed, inactive last and archived behind a toggle", async () => {
    const app = await launch();
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "");
    for (let i = 0; i < 3; i++) await app.keyboard.press("left");
    await app.waitFor("id:projects-overview");
    const text = app.texts().join("\n").replace(/\u00a0/g, " ");
    const at = (needle: string) => { const i = text.indexOf(needle); expect(i, needle).toBeGreaterThanOrEqual(0); return i; };
    expect(at("Alpha chat")).toBeLessThan(at("Bravo notes"));
    expect(at("Bravo notes")).toBeLessThan(at("Inactive"));
    expect(at("Inactive")).toBeLessThan(at("Charlie draft"));
    expect(text).toContain("· Passive");
    expect(text).toContain("· r restore");
    expect(text).toContain("▸ 1 archived");
    expect(text).not.toContain("Alpha old chat");
    await app.screenshot("/private/tmp/nexus-nsh-overview.png");
    await app.press("id:overview-row-2"); // Alpha's archived toggle
    await app.waitUntil(() => app.texts().join("\n").replace(/\u00a0/g, " ").includes("Alpha old chat"));
    expect(app.texts().join("\n").replace(/\u00a0/g, " ")).toContain("▾ 1 archived");
    await app.screenshot("/private/tmp/nexus-nsh-overview-archived.png");
    await app.press("id:overview-row-2");
    await app.waitUntil(() => !app.texts().join("\n").replace(/\u00a0/g, " ").includes("Alpha old chat"));
  });

  test("r restores an inactive project in one action", async () => {
    const app = await launch({ "PATCH */api/project/prj_charlie": { body: { ...projects[2], status: "current", revision: 2 } } });
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "");
    for (let i = 0; i < 3; i++) await app.keyboard.press("left");
    await app.waitFor("id:projects-overview");
    expect(app.texts().join("\n").replace(/\u00a0/g, " ")).toContain("· Inactive");
    await app.keyboard.press("end"); // Charlie's session row: restore acts on its project
    await app.keyboard.press("r");
    await app.waitUntil(() => app.requests().some((r) => r.method === "PATCH" && r.url.endsWith("/api/project/prj_charlie")));
    expect(JSON.parse((app.requests().find((r) => r.method === "PATCH") as any).body)).toEqual({ status: "current" });
  });

  test("the palette command opens the overview", async () => {
    const app = await launch();
    await app.waitFor("id:composer-input");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "Projects overview");
    await app.keyboard.press("enter");
    await app.waitFor("id:projects-overview");
  });

  test("the project picker lists inactive projects and choosing one restores it", async () => {
    const app = await launch({ "PATCH */api/project/prj_charlie": { body: { ...projects[2], status: "current", revision: 2 } } });
    await app.waitFor("id:composer-input");
    await app.keyboard.press("mod+shift+k");
    await app.waitFor("id:picker");
    expect(app.texts().some((t) => t.includes("inactive · /work/charlie"))).toBe(true);
    await app.press("id:pick-2");
    await app.waitUntil(() => app.requests().some((r) => r.method === "PATCH" && r.url.endsWith("/api/project/prj_charlie")));
    expect(JSON.parse((app.requests().find((r) => r.method === "PATCH") as any).body)).toEqual({ status: "current" });
  });
});
