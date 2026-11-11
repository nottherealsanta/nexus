import { describe, expect, open, test } from "nsh/test";
import terminalDock from "../interface/scenarios/terminal-dock.json";

const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Resources", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const kernels = [{
  id: "krn_1", created_by: "ses_1", created: 9, cell_count: 2, busy: true, users: ["ses_1"],
  variables: [{ name: "answer", type: "int", repr: "42" }], cells_clipped: 0,
}];
const jobs = [
  { id: "job_1", session_id: "ses_1", root: "ses_1", command: "sleep 30", directory: "/work/demo", started: 9, finished: null, duration_s: 3, status: "running", exit_code: null, timeout: null, output_chars: 0, dropped_chars: 0 },
  { id: "job_2", session_id: "ses_1", root: "ses_1", command: "printf done", directory: "/work/demo", started: 5, finished: 6, duration_s: 1, status: "done", exit_code: 0, timeout: null, output_chars: 4, dropped_chars: 0 },
];
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/kernel*": { body: kernels },
  "GET */api/bash*": { body: jobs },
  "GET */api/terminal*": { body: [] },
  "GET */api/session/ses_1/message*": { body: [{ id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "hello" }], created: 1 }] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
};

describe("kernels and shells", () => {
  test("the location strip separates kernel and terminal dock indicators", async () => {
    const app = await open("./App.tsx", { network, width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-resources");
    const strip = app.shows("id:location-resources");
    expect(strip).toBe(true);
    const shown = app.texts().join("\n");
    expect(shown).toContain("kernel");
    expect(shown).toContain("1 shell");
    expect(app.shows("id:location-background-shell")).toBe(true);
    expect(shown.includes("0 shell")).toBe(false);
    expect(app.shows("id:location-shell")).toBe(false);
    expect(shown.includes("1 kernel")).toBe(false);
    await app.press("id:location-resources");
    await app.waitFor("id:resources");
    await app.waitFor("id:resource-kernel-krn_1");
    expect(app.shows("id:resource-job-job_1")).toBe(false);
    const details = app.texts().join("\n");
    expect(details).toContain("Kernels (1)");
    expect(details.includes("Background shells (1 live, 2 kept)")).toBe(false);
    expect(details).toContain("answer: int = 42");
    await app.keyboard.press("Escape");
    await app.waitUntil(() => !app.shows("id:resources"));
    await app.press("id:location-background-shell");
    await app.waitFor("id:resource-job-job_1");
    expect(app.shows("id:resource-kernel-krn_1")).toBe(false);
    expect(app.texts().join("\n")).toContain("Background shells (1 live, 2 kept)");
  });

  test("a running shell can be stopped from the modal", async () => {
    const deleted: string[] = [];
    const app = await open("./App.tsx", {
      network: { ...network, "DELETE */api/bash/*": (req: any) => { deleted.push(String(req.url)); return { body: { ...jobs[0], status: "killed" } }; } },
      width: 1280, height: 900, platform: "web",
    });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-background-shell");
    await app.press("id:location-background-shell");
    await app.waitFor("id:resource-job-job_1");
    expect(app.shows("id:resource-job-stop-job_1")).toBe(true);
    expect(app.shows("id:resource-job-stop-job_2")).toBe(false);
    expect(app.texts().join("\n")).toContain("s stop");
    await app.keyboard.press("ArrowDown");
    await app.keyboard.press("s");
    expect(deleted.length).toBe(0);
    await app.keyboard.press("ArrowUp");
    await app.keyboard.press("s");
    await app.waitUntil(() => deleted.length === 1);
    expect(deleted[0]).toContain("/api/bash/job_1");
    await app.press("id:resource-job-stop-job_1");
    await app.waitUntil(() => deleted.length === 2);
  });

  test("the count is absent when no kernel or running shell exists", async () => {
    const app = await open("./App.tsx", {
      network: { ...network, "GET */api/kernel*": { body: [] }, "GET */api/bash*": { body: [] } },
      width: 1280, height: 900, platform: "web",
    });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-strip");
    expect(app.shows("id:location-resources")).toBe(false);
    expect(app.shows("id:location-background-shell")).toBe(false);
    expect(app.shows("id:location-shell")).toBe(false);
    expect(app.texts().join("\n").includes("kernel ·")).toBe(false);
  });

  test("background and terminal shells have separate indicators", async () => {
    const app = await open("./App.tsx", {
      network: { ...network, "GET */api/terminal*": { body: [{ id: "t1", name: "Shell", project_id: "prj_1", workspace: "/work/demo", status: "running", controller: null, rows: 24, cols: 80 }] } },
      width: 1280, height: 900, platform: "web",
    });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-background-shell");
    await app.waitFor("id:location-shell");
    expect(app.texts().join("\n")).toContain("1 shell");
    expect(app.texts().join("\n")).toContain("1 terminal shell");
    await app.press("id:location-background-shell");
    await app.waitFor("id:resource-job-job_1");
  });

  test("a running job never appears in the dock; the shell indicator opens the jobs list", async () => {
    const app = await open("./App.tsx", {
      network: { ...network,
        "GET */api/bash*": { body: [{...terminalDock.background_job, session_id: session.id, root: session.id}] },
        "GET */api/terminal*": { body: terminalDock.terminals.map(t => ({...t, project_id: project.id, session_id: session.id})) },
      }, width: 1280, height: 900, platform: "web",
    });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-shell");
    expect(app.shows("id:location-background-shell")).toBe(true);
    expect(app.texts().join("\n")).toContain("1 shell");
    expect(app.texts().join("\n")).toContain("1 terminal shell");
    await app.press("id:location-shell");
    await app.waitFor("id:terminal-panel");
    for (const fact of terminalDock.expect) expect(app.texts().join("\n")).toContain(fact);
    for (const fact of terminalDock.absent) expect(app.texts().join("\n").includes(fact)).toBe(false);
  });
});
