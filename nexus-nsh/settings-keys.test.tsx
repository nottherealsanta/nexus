// Settings keys: navigation never changes a value; Enter or Space on a row does (a switch flips, an enum edits in place).
import { describe, expect, open, test } from "nsh/test";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const config = { revision: "t", values: { defaults: { permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false }, browser: { use_my_chrome: false } }, defaults: {}, schema: {}, errors: [] };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: config },
  "PATCH */api/config": { body: config },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

async function boot() {
  const app = await open("./App.tsx", { network, width: 1280, height: 768 });
  await app.waitFor("id:header");
  await app.keyboard.press("mod+,");
  await app.waitFor("id:settings");
  return app;
}
const patches = (app: Awaited<ReturnType<typeof boot>>) => app.requests().filter((r) => r.method === "PATCH");
const shown = (app: Awaited<ReturnType<typeof boot>>) => app.texts().join("\n");

describe("Settings keys", () => {
  test("Right on a section enters its settings and changes nothing; Left goes back", async () => {
    const app = await boot();
    await app.waitFor("id:settings-section-focus");
    expect(shown(app)).toContain("enter open");
    await app.keyboard.press("right");
    await app.waitFor("id:settings-row-focus");
    expect(app.shows("id:settings-section-focus")).toBe(false);
    expect(shown(app)).toContain("enter change");
    expect(patches(app).length).toBe(0);
    await app.keyboard.press("right");
    expect(patches(app).length).toBe(0);
    await app.keyboard.press("left");
    await app.waitFor("id:settings-section-focus");
    expect(patches(app).length).toBe(0);
  });

  test("Enter and Space flip a switch; arrows on its row do not", async () => {
    const app = await boot();
    await app.press("id:setting-appearance.animations");
    await app.keyboard.press("right");
    expect(patches(app).length).toBe(0);
    await app.press("id:setting-appearance.animations");
    await app.keyboard.press("enter");
    await app.waitUntil(() => patches(app).length === 1);
    expect(String(patches(app)[0].body)).toContain('"ui.reduced_motion":true');
    await app.keyboard.press("space");
    await app.waitUntil(() => patches(app).length === 2);
  });

  test("an enum edits in place: arrows choose, Enter sets, Esc cancels", async () => {
    const app = await boot();
    await app.press("id:section-tools");
    await app.press("id:setting-tools.default_permission");
    await app.keyboard.press("enter");
    await app.waitUntil(() => shown(app).includes("esc cancel"));
    expect(shown(app)).toContain("enter set");
    await app.keyboard.press("right");
    expect(patches(app).length).toBe(0);
    await app.keyboard.press("escape");
    await app.waitUntil(() => !shown(app).includes("esc cancel"));
    expect(patches(app).length).toBe(0);
    expect(app.shows("id:settings")).toBe(true);
    await app.keyboard.press("enter");
    await app.waitUntil(() => shown(app).includes("esc cancel"));
    await app.keyboard.press("right");
    await app.keyboard.press("enter");
    await app.waitUntil(() => patches(app).length === 1);
    expect(String(patches(app)[0].body)).toContain('"defaults.permission":"ask"');
  });
});
