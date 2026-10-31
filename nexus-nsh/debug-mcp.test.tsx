import { describe, test, open } from "nsh/test";

const sse = (frames: string) => frames;
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [], running: [], approvals: [], questions: [], selected: null,
};
const core = () => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
});
const row = {
  server: "docs", state: "idle", error: null, tools: 0, enabled: true, source: "project",
  project_id: "prj_1", file: "/work/demo/.mcp.json",
  transport: "stdio", command: "fake-server", arg_count: 4, cwd: "/work/demo", env_names: ["TOKEN"],
  warnings: [],
};
const summary = { project_id: "prj_1", file: "/work/demo/.mcp.json", exists: true, revision: "rev1", error: null, warnings: [], servers: [row] };

describe("debug", () => {
  test("texts", async () => {
    const app = await open("./App.tsx", {
      network: { ...core(), "GET */api/project/prj_1/mcp": { body: summary } },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:tab-projects");
    await app.waitFor("id:project-mcp-server-docs");
    await app.wait(200);
    console.log("TEXTS", JSON.stringify(app.texts()));
  });
});
