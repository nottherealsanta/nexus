// Per-agent, per-model effort pickers (docs/config.md): Settings rows and the onboarding rows share the same
// default label, and clearing the default removes only that model's override.
import { describe, expect, open, test } from "nsh/test";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const providers = [{ id: "codex", label: "Codex", connected: true, verified: true, methods: ["browser"] }];
const models = [{ ref: "codex/gpt-5", name: "GPT-5", efforts: ["low", "medium", "high"] }];
const agents = [{ name: "plan", mode: "subagent", source: "builtin", description: "Plans", effort: "low", tools: [], subagent: { pool: [], fallbacks: [], efforts: {}, effective: { model: "", source: "parent" } } }];
const settings = { defaults: {}, agents: { models: { plan: ["codex/gpt-5"] }, fallbacks: {}, efforts: {} }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true, onboarded: true } };
const config = (values: object) => ({ revision: "t", values, defaults: {}, schema: {}, errors: [] });

const base = (values: object, extra: object = {}) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: providers },
  "GET */api/config": { body: config(values) },
  "PATCH */api/config": { body: { ...config(values), revision: "t2" } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models, model: "scripted/m" } },
  "GET */api/agent*": { body: { agents, problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  ...extra,
});
const patches = (app: any) => app.requests().filter((r: any) => r.method === "PATCH" && r.url.includes("/api/config")).map((r: any) => JSON.parse(r.body ?? "{}"));

describe("reasoning effort pickers", () => {
  test("Settings opens the model's effort picker and its default clears the override", async () => {
    const app = await open("./App.tsx", {
      network: base({ ...settings, agents: { ...settings.agents, efforts: { plan: { "codex/gpt-5": "high" } } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:header");
    await app.press("id:rail-settings");
    await app.waitFor("id:settings");
    await app.press("id:section-agents");
    await app.waitFor("id:agent-row-plan");
    await app.press("id:agent-row-plan");
    await app.waitFor("id:agent-models-plan");
    expect(app.texts().join("\n")).toContain("effort high");
    expect(app.shows("id:agent-effort-up-plan-codex/gpt-5")).toBe(true);
    await app.press("id:agent-effort-pick-plan-codex/gpt-5");
    await app.waitFor("id:picker");
    const shown = app.texts().join("\n");
    expect(shown).toContain("Agent default: low");
    expect(shown).toContain("medium");
    await app.press("id:pick-0");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["agents.efforts"] && Object.keys(p.set["agents.efforts"]).length === 0));
  });

  test("a Settings row with no override names the agent's default, not the provider's", async () => {
    const app = await open("./App.tsx", { network: base(settings), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:header");
    await app.press("id:rail-settings");
    await app.waitFor("id:settings");
    await app.press("id:section-agents");
    await app.waitFor("id:agent-row-plan");
    await app.press("id:agent-row-plan");
    await app.waitFor("id:agent-models-plan");
    expect(app.texts().join("\n")).toContain("effort Agent default: low");
    await app.press("id:agent-effort-pick-plan-codex/gpt-5");
    await app.waitFor("id:picker");
    expect(app.texts().join("\n")).toContain("Agent default: low");
  });

  test("a stale override on a model with no catalog levels can still be cleared", async () => {
    const stale = { ...settings, agents: { models: { plan: ["codex/unknown"] }, fallbacks: {}, efforts: { plan: { "codex/unknown": "high" } } } };
    const app = await open("./App.tsx", { network: base(stale), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:header");
    await app.press("id:rail-settings");
    await app.waitFor("id:settings");
    await app.press("id:section-agents");
    await app.waitFor("id:agent-row-plan");
    await app.press("id:agent-row-plan");
    await app.waitFor("id:agent-models-plan");
    expect(app.texts().join("\n")).toContain("effort high");
    await app.press("id:agent-effort-pick-plan-codex/unknown");
    await app.waitFor("id:picker");
    expect(app.texts().join("\n")).toContain("Agent default: low");
    await app.press("id:pick-0");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["agents.efforts"] && Object.keys(p.set["agents.efforts"]).length === 0));
  });

  test("an offline Settings effort control cannot open the picker", async () => {
    const values = { ...settings, agents: { ...settings.agents, efforts: { plan: { "codex/gpt-5": "high" } } } };
    let info = 0;
    const app = await open("./App.tsx", {
      network: {
        ...base(values),
        "GET */api/info": () => info++ === 0 ? { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } } : { status: 502, body: { error: "Core went away" } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:header");
    await app.waitUntil(() => app.texts().join(" ").includes("Disconnected from Core"));
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-agents");
    await app.waitFor("id:agent-row-plan");
    await app.press("id:agent-row-plan");
    await app.waitFor("id:agent-models-plan");
    let refused = false;
    try { await app.press("id:agent-effort-pick-plan-codex/gpt-5"); } catch { refused = true; }
    expect(refused).toBe(true);
    await app.wait(100);
    expect(app.shows("id:picker")).toBe(false);
  });

  test("an onboarding row names its effort and e opens the picker", async () => {
    const tour = { ...settings, agents: { models: { scout: ["codex/gpt-5"] }, fallbacks: {}, efforts: {} }, ui: { ...settings.ui, onboarded: false } };
    const scout = { ...agents[0], name: "scout", mode: "subagent", description: "Scouts", effort: "low" };
    const app = await open("./App.tsx", { network: base(tour, { "GET */api/agent*": { body: { agents: [scout], problems: [] } } }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-provider");
    expect(app.texts().join("\n")).not.toContain("e effort");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-agents");
    expect(app.texts().join("\n")).toContain("e effort");
    expect(app.texts().join("\n")).toContain("effort Agent default: low");
    await app.keyboard.press("down"); // the root row, then scout
    await app.keyboard.press("e");
    await app.waitFor("id:picker");
    expect(app.texts().join("\n")).toContain("Agent default: low");
    await app.press("id:pick-2"); // default, low, medium
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["agents.efforts"]?.scout?.["codex/gpt-5"] === "medium"));
  });

  test("choosing an onboarding model with levels opens its effort picker, and Escape keeps the default", async () => {
    const tour = { ...settings, agents: { models: {}, fallbacks: {}, efforts: {} }, ui: { ...settings.ui, onboarded: false } };
    const scout = { ...agents[0], name: "scout", mode: "subagent", description: "Scouts", effort: "" };
    const app = await open("./App.tsx", { network: base(tour, { "GET */api/agent*": { body: { agents: [scout], problems: [] } } }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-agents");
    await app.press("id:onboarding-model-scout");
    await app.waitFor("id:picker");
    await app.press("text:codex/gpt-5");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["agents.models"]?.scout?.[0] === "codex/gpt-5"));
    await app.waitUntil(() => app.texts().join("\n").includes("Model default"));
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:picker"));
    expect(patches(app).some((p: any) => "agents.efforts" in (p.set ?? {}))).toBe(false);
  });

  test("e on a model with no levels and no override warns instead of opening a default-only picker", async () => {
    const tour = { ...settings, agents: { models: { scout: ["codex/gpt-5"] }, fallbacks: {}, efforts: {} }, ui: { ...settings.ui, onboarded: false } };
    const scout = { ...agents[0], name: "scout", mode: "subagent", description: "Scouts", effort: "" };
    const app = await open("./App.tsx", {
      network: base(tour, {
        "GET */api/model*": { body: { source: "t", models: [{ ref: "codex/gpt-5", name: "GPT-5" }] } },
        "GET */api/agent*": { body: { agents: [scout], problems: [] } },
      }),
      width: 1280, height: 768,
    });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-agents");
    await app.keyboard.press("down");
    await app.keyboard.press("e");
    await app.waitUntil(() => app.texts().join("\n").includes("codex/gpt-5 lists no effort levels"));
    expect(app.shows("id:picker")).toBe(false);
  });
});
