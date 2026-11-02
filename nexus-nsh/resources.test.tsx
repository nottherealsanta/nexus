import { describe, expect, open, test } from "nsh/test";

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
  "GET */api/session/ses_1/message*": { body: [{ id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "hello" }], created: 1 }] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
};

describe("kernels and shells", () => {
  test("the location strip counts live kernels and running shells, and opens their details", async () => {
    const app = await open("./App.tsx", { network, width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-resources");
    const strip = app.shows("id:location-resources");
    expect(strip).toBe(true);
    const shown = app.texts().join("\n");
    expect(shown).toContain("1 kernel · 1 shell");
    expect(shown.includes("1 kernel · 2 shells")).toBe(false);
    await app.press("id:location-resources");
    await app.waitFor("id:resources");
    await app.waitFor("id:resource-kernel-krn_1");
    await app.waitFor("id:resource-job-job_1");
    const details = app.texts().join("\n");
    expect(details).toContain("Kernels (1)");
    expect(details).toContain("Background shells (1 live, 2 kept)");
    expect(details).toContain("sleep 30");
    expect(details).toContain("printf done");
    expect(details).toContain("answer: int = 42");
    await app.keyboard.press("Escape");
    await app.waitUntil(() => !app.shows("id:resources"));
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
    expect(app.texts().join("\n").includes("kernel ·")).toBe(false);
  });
});
