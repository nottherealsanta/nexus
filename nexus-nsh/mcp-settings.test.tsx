// Settings › MCP (remote servers): the page, adding one from its address, the sign-in link and the
// detail actions. The TUI's tests/shell.rs `the_mcp_page_…` tests check the same behaviours.
import { describe, expect, test } from "nsh/test";
import { open } from "./open-bundle";
import { mcpSummary, stateLabel } from "./src/ui/mcp-settings";

const sse = (frames: string) => `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n${frames}`;
const frame = (id: number, type: string, data: unknown) => `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ id, type, cursor: `log_t:${id}`, data })}\n\n`;
type Network = Exclude<NonNullable<Parameters<typeof open>[1]>["network"], "live" | null | undefined>;

const linear = { server: "linear", state: "failed", auth: "required", transport: "http", url: "https://mcp.linear.app/mcp", oauth: true, enabled: true, tools: 0, error: "sign-in required", header_names: ["X-Team"] };
const docs = { server: "docs", state: "connected", auth: "signed_in", transport: "http", url: "https://docs.example.com/mcp", oauth: true, enabled: true, tools: 3, header_names: [] };

const core = (extra: Network = {}, events = "") => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: sse(events), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "r1", values: { defaults: { effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false }, mcp: { full_definitions: false, max_listed_tools: 25 } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/project/prj_1/mcp*": { body: { servers: [], error: null } },
  "GET */api/mcp": { body: { servers: [linear, docs] } },
  ...extra,
});

const openMcp = async (network: Network) => {
  const app = await open("./App.tsx", { network, width: 1100, height: 800, platform: "web" });
  await app.waitFor("id:rail-project-prj_1");
  await app.press("id:rail-settings");
  await app.press("id:section-mcp");
  await app.waitFor("id:mcp-page");
  return app;
};
const sent = (app: any, method: string, tail: string) => app.requests().filter((r: any) => r.method === method && r.url.endsWith(tail));

describe("Settings › MCP", () => {
  test("labels say what the TUI says", () => {
    expect(stateLabel({ ...linear, scope: "global" } as any)).toBe("sign-in required");
    expect(stateLabel({ ...docs, scope: "global" } as any)).toBe("connected · 3 tools");
    expect(stateLabel({ ...docs, scope: "global", tools: 1 } as any)).toBe("connected · 1 tool");
    expect(stateLabel({ ...docs, scope: "global", state: "idle", auth: "none" } as any)).toBe("not signed in");
    expect(mcpSummary()).toBe("loading…");
  });

  test("the page lists servers with their state and the new-session settings", async () => {
    const app = await openMcp(core());
    await app.waitFor("id:mcp-open-global-linear");
    const text = app.texts().join("\n");
    for (const needle of ["Add a remote server", "Add to this project", "Every project", "linear", "sign-in required", "docs", "connected · 3 tools", "New sessions", "Full tool definitions in context"]) {
      expect(text).toContain(needle);
    }
  });

  test("adding refuses an address that is not http(s) and sends nothing", async () => {
    const app = await openMcp(core());
    await app.press("id:mcp-add");
    await app.waitFor("id:mcp-address");
    await app.type("id:mcp-address", "mcp.example.com");
    await app.press("id:mcp-add-confirm");
    await app.wait(150);
    expect(sent(app, "POST", "/api/mcp").length).toBe(0);
    expect(app.shows("id:mcp-address")).toBe(true);
  });

  test("adding takes only an address; Core names the server", async () => {
    const app = await openMcp(core({ "POST */api/mcp": { status: 201, body: { ...linear, server: "example" } } }));
    await app.press("id:mcp-add");
    await app.waitFor("id:mcp-address");
    await app.type("id:mcp-address", "https://mcp.example.com/mcp");
    await app.press("id:mcp-add-confirm");
    await app.waitUntil(() => sent(app, "POST", "/api/mcp").length === 1);
    expect(JSON.parse(sent(app, "POST", "/api/mcp")[0].body)).toEqual({ entry: { url: "https://mcp.example.com/mcp" } });
  });

  test("a server that needs a sign-in shows its actions; the link Core sends is opened once and shown with Cancel", async () => {
    const started = frame(1, "mcp.login", { server: "linear", status: "pending", verification_uri: "https://auth.linear.app/authorize?x=1" });
    const app = await openMcp(core({ "POST */api/mcp/linear/login": { status: 202, body: { server: "linear", status: "pending", message: null } } }, started));
    await app.press("id:mcp-open-global-linear");
    await app.waitFor("id:mcp-detail");
    const text = () => app.texts().join("\n");
    // The scripted event already carries the link, so the detail shows the pending sign-in's link and Cancel
    // (a live Core sends it after the POST); the always-present actions are there either way.
    for (const needle of ["Add a header", "Switch off", "Restart", "Remove", "headers X-Team (values never shown)", "sign-in required"]) {
      expect(text()).toContain(needle);
    }
    await app.waitFor("id:mcp-detail-link");
    expect(text().includes("https://auth.linear.app/authorize")).toBe(true);
    expect(app.shows("id:mcp-detail-cancel")).toBe(true);
    expect(app.shows("id:mcp-detail-sign-in")).toBe(false);
  });

  test("a server that needs a sign-in offers Sign in, and pressing it asks Core to start one", async () => {
    const app = await openMcp(core({ "POST */api/mcp/linear/login": { status: 202, body: { server: "linear", status: "pending", message: null } } }));
    await app.press("id:mcp-open-global-linear");
    await app.waitFor("id:mcp-detail-sign-in");
    await app.press("id:mcp-detail-sign-in");
    await app.waitUntil(() => sent(app, "POST", "/api/mcp/linear/login").length === 1);
    await app.waitFor("id:mcp-detail-cancel");
  });

  test("a header written without a name sends nothing", async () => {
    const app = await openMcp(core());
    await app.press("id:mcp-open-global-docs");
    await app.waitFor("id:mcp-detail");
    await app.press("id:mcp-detail-header");
    await app.type("id:mcp-header-input", "just-a-token");
    await app.press("id:mcp-header-save");
    await app.wait(150);
    expect(sent(app, "PATCH", "/api/mcp/docs").length).toBe(0);
  });

  test("a header is typed as Name: value; removal asks first", async () => {
    const app = await openMcp(core({
      "PATCH */api/mcp/docs": { body: { ...docs, header_names: ["Authorization"] } },
      "DELETE */api/mcp/docs": { body: { server: "docs", removed: true } },
    }));
    await app.press("id:mcp-open-global-docs");
    await app.waitFor("id:mcp-detail");
    await app.press("id:mcp-detail-header");
    await app.type("id:mcp-header-input", "Authorization: Bearer abc");
    await app.press("id:mcp-header-save");
    await app.waitUntil(() => sent(app, "PATCH", "/api/mcp/docs").length === 1);
    expect(JSON.parse(sent(app, "PATCH", "/api/mcp/docs")[0].body)).toEqual({ entry: { headers: { Authorization: "Bearer abc" } } });
    // Remove: a confirmation first; nothing is sent until it is confirmed.
    await app.press("id:mcp-detail-remove");
    await app.wait(150);
    expect(sent(app, "DELETE", "/api/mcp/docs").length).toBe(0);
  });
});
