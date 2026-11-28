import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/claude-direct.json";
import { startingSections } from "./src/ui/newsession";

const project = { id: "prj_direct", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_direct", head: 0, floor: 0, head_cursor: "log_direct:0" }, projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_direct","head":0,"head_cursor":"log_direct:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [scenario.provider] },
  "GET */api/config": { body: { revision: "t", values: { defaults: { permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: scenario.model.ref } },
  "GET */api/model*": { body: { source: "fixture", models: [scenario.model] } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/usage*": { body: { fetched: 1, providers: [scenario.usage] } },
};

describe("Claude direct", () => {
  test("compatibility instruction is visible in the system prompt detail", () => {
    const system = startingSections(scenario.starting, []).sections[0];
    expect(system.detail).toContain("You are Claude Code, Anthropic's official CLI for Claude.");
    expect(system.detail).toContain("You are an AI agent running in Nexus.");
  });
  test("shared provider row and connection note remain distinct from SDK mode", async () => {
    const app = await open("./App.tsx", { network, width: 1280, height: 768 });
    await app.waitFor("id:header");
    await app.waitFor("id:composer-model");
    expect(app.texts().join("\n")).toContain("Claude Haiku 4.5");
    expect(app.texts().join("\n")).not.toContain("Direct · Claude Haiku");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-providers");
    await app.waitFor("id:provider-open-claude-direct");
    expect(app.texts().join("\n")).toContain(scenario.provider.label);
    await app.press("id:provider-open-claude-direct");
    await app.waitFor("id:provider-detail");
    expect(app.texts().join("\n")).toContain(scenario.provider.note);
    expect(app.shows("id:provider-detail-disconnect")).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:provider-detail")).toBe(false);
  });
  test("direct-only sign-in choices are available when disconnected", async () => {
    const app = await open("./App.tsx", { network: { ...network,
      "GET */api/provider": { body: [{ ...scenario.provider, connected: false }] },
    }, width: 1280, height: 768 });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-providers");
    await app.waitFor("id:provider-open-claude-direct");
    await app.press("id:provider-open-claude-direct");
    await app.waitFor("id:provider-detail");
    expect(app.texts().join("\n")).toContain("Sign in with browser (1-year token)");
    expect(app.texts().join("\n")).toContain("Use signed-in Claude Code profile (read-only)");
  });
});
