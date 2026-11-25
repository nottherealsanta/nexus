import { expect, open, test } from "nsh/test";

test("the new-session view has no separate Project MCP section", async () => {
  const app = await open("./App.tsx", {
    network: {
      "GET */__nexus/host": { body: { host: "test", canStart: false } },
      "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
      "GET */api/snapshot*": { body: {
        watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
        projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
        sessions: [], running: [], approvals: [], questions: [], selected: null,
      } },
      "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
      "GET */api/provider": { body: [] },
      "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
      "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
      "GET */api/agent*": { body: { agents: [], problems: [] } },
      "GET */api/skill*": { body: { skills: [], problems: [] } },
      "GET */api/mcp*": { body: { servers: [] } },
    },
    width: 1024, height: 768, platform: "web",
  });
  await app.waitFor("id:context-sections");
  expect(app.shows("id:project-mcp")).toBe(false);
  expect(app.texts().join("\n").includes("Project MCP")).toBe(false);
});
