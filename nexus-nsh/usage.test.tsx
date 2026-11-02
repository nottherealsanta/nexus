import { describe, expect, open, test } from "nsh/test";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "",
  permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0,
  revision: 1, disabled_tools: "[]",
};
const network = (extra: Record<string, unknown> = {}) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/*": { body: session },
  ...extra,
});

describe("provider usage", () => {
  test("lists every connected provider, including those without plan limits", async () => {
    const app = await open("./App.tsx", {
      network: network({
        "GET */api/usage": { body: { fetched: 1, providers: [
          { id: "codex", label: "ChatGPT (Codex)", plan: "Plus", notes: [], error: null,
            windows: [{ label: "5-hour", used_percent: 42, resets_at: null }] },
          { id: "openai", label: "OpenAI (API key)", plan: "", notes: ["No plan limits; token usage is in the session totals."], error: null, windows: [] },
        ] } },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.keyboard.press("mod+shift+u");
    await app.waitFor("id:usage");
    const shown = app.texts().join("\n");
    expect(shown.includes("Provider usage")).toBe(true);
    expect(shown.includes("ChatGPT (Codex)") && shown.includes("42%")).toBe(true);
    expect(shown.includes("OpenAI (API key)")).toBe(true);
    expect(shown.includes("No plan limits; token usage is in the session totals.")).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:usage")).toBe(false);
  });

  test("names the empty state when no provider is connected", async () => {
    const app = await open("./App.tsx", {
      network: network({ "GET */api/usage": { body: { fetched: 1, providers: [] } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.keyboard.press("mod+shift+u");
    await app.waitFor("id:usage");
    expect(app.texts().join("\n").includes("No provider is connected.")).toBe(true);
  });
});
