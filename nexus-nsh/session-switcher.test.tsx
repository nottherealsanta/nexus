import { describe, expect, open, test } from "nsh/test";
import { spinner as spinnerFrames } from "./src/gen/theme";

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
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
};

describe("session switcher", () => {
  test("pressing the open project in the rail toggles the sidebar", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_1");
    await app.press("id:rail-project-prj_1");
    await app.waitFor("id:session-ses_1");
    await app.press("id:rail-project-prj_1");
    expect(app.shows("id:left-sidebar")).toBe(false);
    expect(app.shows("id:session-switcher")).toBe(false);
    await app.press("id:rail-project-prj_1");
    await app.waitFor("id:left-sidebar");
  });

  test("a running mark uses the animated spinner, and reduced motion keeps the static glyph", async () => {
    const animated = await open("./App.tsx", { network, width: 1024, height: 768, platform: "web" });
    await animated.press("id:rail-project-prj_1");
    await animated.waitFor("id:session-ses_1");
    expect(animated.texts().some((t) => spinnerFrames.includes(t))).toBe(true);
    expect(animated.texts().includes("◐")).toBe(false);

    const reducedNetwork = { ...network, "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } } };
    const still = await open("./App.tsx", { network: reducedNetwork, width: 1024, height: 768, platform: "web" });
    await still.press("id:rail-project-prj_1");
    await still.waitUntil(() => still.texts().includes("◐"));
    expect(still.texts().some((t) => spinnerFrames.includes(t))).toBe(false);
  });
});
