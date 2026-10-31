// Project `.mcp.json` in the Projects view: enabled entries start automatically with a
// new session, so the panel shows current status and non-secret configuration facts only.
import { describe, expect, open, test } from "nsh/test";
import { configMeta, configShape, stateLabel } from "./src/project-mcp";

const sse = (frames: string) => frames;
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [],
  running: [], approvals: [], questions: [], selected: null,
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

function row(overrides: Record<string, unknown> = {}) {
  return {
    server: "docs", state: "idle", error: null, tools: 0, enabled: true, source: "project",
    project_id: "prj_1", file: "/work/demo/.mcp.json",
    transport: "stdio", command: "fake-server", arg_count: 4, cwd: "/work/demo", env_names: ["TOKEN"],
    warnings: [],
    ...overrides,
  };
}

function summary(entries: ReturnType<typeof row>[], extra: Record<string, unknown> = {}) {
  return {
    project_id: "prj_1", file: "/work/demo/.mcp.json", exists: true, revision: "rev1",
    error: null, warnings: [], servers: entries,
    ...extra,
  };
}

function mcpPosts(app: any) {
  return app.requests().filter((request: any) => request.method === "POST" && request.url.includes("/api/project/prj_1/mcp"));
}

describe("project MCP configuration facts", () => {
  test("the shape and meta lines report only what Core describes, never secret values", () => {
    const stdio = row();
    expect(configShape(stdio)).toBe("stdio · fake-server · 4 arguments");
    expect(configMeta(stdio)).toBe("/work/demo · env TOKEN (values never shown)");

    const http = row({
      server: "web", transport: "http", command: undefined, arg_count: undefined,
      cwd: undefined, env_names: undefined, url: "https://example.test/mcp", header_names: ["Authorization"],
    });
    expect(configShape(http)).toBe("http · https://example.test/mcp");
    expect(configMeta(http)).toBe("headers Authorization (values never shown)");

    expect(configShape(row({ transport: undefined }))).toBe("");
    expect(configMeta(row({ transport: undefined }))).toBe("");
  });

  test("status labels describe the current connection state", () => {
    expect(stateLabel(row({ state: "connected", tools: 3 }))).toBe("connected · 3 tools");
    expect(stateLabel(row({ state: "connected", tools: 1 }))).toBe("connected · 1 tool");
    expect(stateLabel(row({ state: "connecting" }))).toBe("connecting");
    expect(stateLabel(row({ state: "idle", enabled: false }))).toBe("disabled");
    expect(stateLabel(row({ state: "disabled" }))).toBe("disabled");
    expect(stateLabel(row({ state: "failed" }))).toBe("failed");
    expect(stateLabel(row({ state: "invalid" }))).toBe("invalid");
  });
});

describe("project MCP in the Projects view", () => {
  test("the panel shows status, configuration and the auto-run warning without approval controls", async () => {
    const web = row({
      server: "web", transport: "http", command: undefined, arg_count: undefined,
      cwd: undefined, env_names: undefined, url: "https://example.test/mcp",
      header_names: ["Authorization"], state: "connected", tools: 2,
    });
    const app = await open("./App.tsx", {
      network: { ...core(), "GET */api/project/prj_1/mcp": { body: summary([row(), web]) } },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:tab-projects");
    await app.waitFor("id:project-mcp-server-docs");

    const panel = app.texts().join("\n");
    expect(panel.includes("Project MCP")).toBe(true);
    expect(panel.includes("idle")).toBe(true);
    expect(panel.includes("stdio · fake-server · 4 arguments")).toBe(true);
    expect(panel.includes("/work/demo · env TOKEN (values never shown)")).toBe(true);
    expect(panel.includes("http · https://example.test/mcp")).toBe(true);
    expect(panel.includes("headers Authorization (values never shown)")).toBe(true);
    expect(panel.includes("connected · 2 tools")).toBe(true);
    expect(panel.includes("start automatically with a new session")).toBe(true);
    expect(panel.includes("commands run then, without asking")).toBe(true);

    // Nothing here approves or revokes anything: reading and refreshing only issue GETs.
    expect(app.shows("id:project-mcp-review-docs")).toBe(false);
    expect(app.shows("id:project-mcp-revoke-docs")).toBe(false);
    const visible = app.texts().join("\n").toLowerCase();
    expect(visible.includes("approv")).toBe(false);
    expect(visible.includes("revoke")).toBe(false);
    expect(visible.includes("pending")).toBe(false);
    await app.press("id:project-mcp-refresh");
    await app.wait(100);
    expect(mcpPosts(app).length).toBe(0);
  });

  test("disabled and invalid entries are shown with their state and reason", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        "GET */api/project/prj_1/mcp": {
          body: summary([
            row({ server: "scan", state: "disabled", enabled: false }),
            row({ server: "broken", state: "invalid", enabled: false, transport: undefined, error: "command is empty" }),
          ]),
        },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:tab-projects");
    await app.waitFor("id:project-mcp-server-scan");

    const panel = app.texts().join("\n");
    expect(panel.includes("disabled")).toBe(true);
    expect(panel.includes("invalid")).toBe(true);
    expect(panel.includes("command is empty")).toBe(true);
    // No enabled entry can start, so the auto-run warning is absent.
    expect(panel.includes("start automatically with a new session")).toBe(false);
  });
});
