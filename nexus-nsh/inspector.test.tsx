import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/inspector.json";

const network = { "GET */api/session/ses_inspector/inspect*": { body: scenario } };
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const session = { id: "ses_inspector", project_id: "prj_1", directory: "/work/demo", title: "Review", model: "scripted/m", permission: "allow", created: 1, updated: 2, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const appNetwork = {
  ...network,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "t", values: { defaults: {}, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};
describe("native inspector", () => {
  test("the app palette opens the native inspector with Core data", async () => {
    const app = await open("./App.tsx", { width: 1280, height: 900, network: appNetwork });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+p");
    await app.waitFor("id:palette-input");
    await app.type("id:palette-input", "Open inspector");
    await app.press("id:cmd-app.open_inspector");
    await app.waitFor("id:inspector");
    await app.waitFor("text:Timing · Input 1,000 · Cached 750 · Uncached 250 · Hit 75% · Writes unknown");
    await app.keyboard.press("Escape");
    expect(app.shows("id:inspector")).toBe(false);
  });
  test("shared cache metrics, parallel lanes, details, zoom and close", async () => {
    const app = await open("./InspectorFixture.tsx", { width: 1280, height: 900, colorScheme: "dark", network });
    await app.waitFor("id:inspector");
    const text = app.texts().join("\n");
    for (const fact of ["Session inspector", "Cached 750", "Hit 75%", "Writes unknown", "Scout", "bash"]) expect(text).toContain(fact);
    await app.press("text:Cache info");
    expect(app.texts().join("\n")).toContain("not a prediction");
    await app.press("text:Cache info");
    await app.press("id:lane-root-model");
    await app.waitFor("text:Model request: 1000 input, 750 cached");
    await app.screenshot("/tmp/nexus-inspector-nsh.png");
    await app.press("text:Zoom in");
    expect(app.texts().join("\n")).toContain("2×");
    await app.keyboard.press("Escape");
    expect(app.texts().join("\n").includes("Session inspector")).toBe(false);
  });
  test("tabs and older pages load inside the inspector", async () => {
    const app = await open("./InspectorFixture.tsx", { width: 1280, height: 900, network });
    await app.waitFor("id:inspector");
    await app.press("id:inspect-Raw");
    await app.waitFor("text:Retained request details");
    await app.press("id:inspect-older");
    await app.waitFor("text:Page 2");
    expect(app.texts().join("\n")).toContain("Retained request details");
  });
  test("narrow inspector keeps navigation and refresh reachable", async () => {
    const app = await open("./InspectorFixture.tsx", { width: 390, height: 844, colorScheme: "dark", network });
    await app.waitFor("id:inspector");
    expect(app.shows("id:inspect-refresh")).toBe(true);
    expect(app.shows("id:inspect-Raw")).toBe(true);
    await app.screenshot("/tmp/nexus-inspector-nsh-narrow.png");
    await app.keyboard.press("Escape");
    expect(app.shows("id:inspector")).toBe(false);
  });
});
