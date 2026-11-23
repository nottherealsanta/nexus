// First-run onboarding (plans/52): opens once per Core home, walks the manifest's steps and writes the choices to config.
import { describe, expect, open, test } from "nsh/test";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const providers = [
  { id: "codex", label: "Codex", connected: true, verified: true, methods: ["browser"] },
  { id: "claude", label: "Claude", connected: false, verified: true, methods: ["cli"] },
];
const modelList = [{ ref: "codex/gpt-6-luna", name: "GPT-6 Luna" }, { ref: "codex/gpt-6", name: "GPT-6" }];
const base = (ui: object, extra: object = {}) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: providers },
  "GET */api/config": { body: { revision: "t", values: { defaults: {}, agents: { models: {}, fallbacks: {}, efforts: {} }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true, ...ui } }, defaults: {}, schema: {}, errors: [] } },
  "PATCH */api/config": { body: { revision: "t2", values: { defaults: {}, agents: { models: {}, fallbacks: {}, efforts: {} }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true, onboarded: true } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: modelList, model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  ...extra,
});
const patches = (app: any) => app.requests().filter((r: any) => r.method === "PATCH" && r.url.includes("/api/config")).map((r: any) => JSON.parse(r.body ?? "{}"));

describe("Onboarding", () => {
  test("opens when the home has not finished it, and not when it has", async () => {
    const fresh = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await fresh.waitFor("id:onboarding");
    expect(fresh.texts().join("\n")).toContain("Welcome to Nexus");
    const done = await open("./App.tsx", { network: base({ onboarded: true }), width: 1280, height: 768 });
    await done.waitFor("id:header");
    expect(done.shows("id:onboarding")).toBe(false);
  });

  test("walks the steps with Next and Back, filling command keys", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-provider");
    expect(app.texts().join("\n")).toContain("Codex");
    await app.press("id:onboarding-back");
    await app.waitFor("id:onboarding-step-welcome");
    expect(app.texts().join("\n")).not.toContain("{key:");
  });

  test("Enter takes the focused Next button, and Esc goes back", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.keyboard.press("enter");
    await app.waitFor("id:onboarding-step-provider");
    await app.keyboard.press("escape");
    await app.waitFor("id:onboarding-step-welcome");
  });

  test("on the agents step Enter opens the focused row's picker and ↓ moves down", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-agents");
    await app.keyboard.press("down");
    await app.keyboard.press("enter");
    await app.waitFor("id:picker");
    expect(app.texts().join("\n")).toContain("Model for scout");
  });

  test("the default agent button cycles and writes defaults.agent", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-root-agent");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["defaults.agent"] === "orchestrate"));
  });

  test("an agent row writes its model, and Skip marks the tour done", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: false }), width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-step-agents");
    expect(app.texts().join("\n")).toContain("luna, haiku or hydrogen");
    await app.press("id:onboarding-model-scout");
    await app.waitFor("id:picker");
    await app.press("text:codex/gpt-6-luna");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["agents.models"]?.scout?.[0] === "codex/gpt-6-luna"));
    await app.press("id:onboarding-skip");
    await app.waitUntil(() => patches(app).some((p: any) => p.set?.["ui.onboarded"] === true));
    await app.waitUntil(() => !app.shows("id:onboarding"));
  });

  test("with no provider connected the model rows say why", async () => {
    const none = base({ onboarded: false }, { "GET */api/provider": { body: providers.map((p) => ({ ...p, connected: false })) } });
    const app = await open("./App.tsx", { network: none, width: 1280, height: 768 });
    await app.waitFor("id:onboarding");
    await app.press("id:onboarding-next");
    await app.press("id:onboarding-next");
    await app.waitFor("id:onboarding-no-provider");
  });

  test("Replay onboarding in Settings opens it again", async () => {
    const app = await open("./App.tsx", { network: base({ onboarded: true }), width: 1280, height: 768 });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-about");
    await app.press("id:setting-about-onboarding");
    await app.waitFor("id:onboarding");
  });
});
