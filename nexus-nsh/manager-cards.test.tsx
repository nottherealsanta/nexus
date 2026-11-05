// The Manager's cards (docs/manager-design.md 10.4): the shared scenarios in interface/scenarios/manager-cards.json,
// drawn by the real app. A card is shown through the dev preview setting (inert unless it is set), so no
// Core or microphone is involved; then keys, clicks and the zero-idle-cost rule are checked on the same app.
import { describe, expect, open, test } from "nsh/test";
import scenarios from "../interface/scenarios/manager-cards.json";
import dropdownScenario from "../interface/scenarios/manager-dropdown.json";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Scenario", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0,"stream_id":"stream_t"}\n\n';
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false }, manager: { enabled: true, mode: "pipeline", model: "mock/manager_demo", proactive: "attention" } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/manager/context": { body: { title: "Manager context", document: "Episodes\n─────\ncurrent  ses_1\n" } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/context": { body: { used: 0, window: null, exact: false } },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
  "POST */api/manager/*": { status: 204, body: null },
  "GET */api/session/*": { body: session },
};

const withCard = (card: unknown, extra: Record<string, unknown> = {}, size = { width: 1280, height: 800 }) =>
  open("./ComposerFixture.tsx", { network, platform: "web", colorScheme: "dark", ...size, storage: { "nexus.manager.dev": JSON.stringify({ card, level: 0.5, ...extra }) } });
const shown = (app: Awaited<ReturnType<typeof open>>) => app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
const manager = (app: Awaited<ReturnType<typeof open>>) => app.requests().filter((r) => r.url.includes("/api/manager"));

describe("manager cards", () => {
  for (const scenario of (scenarios as { cards: { name: string; card: Record<string, unknown>; expect: string[]; absent: string[] }[] }).cards) {
    test(scenario.name, async () => {
      const app = await withCard(scenario.card);
      await app.waitFor("id:manager-card");
      const text = shown(app);
      // A flowchart is the shared raster in NSH (as in the timeline): its node names are in the picture, not in
      // the texts, so the card is recognised by the diagram's copy-source row instead.
      const facts = scenario.name === "diagram" ? ["◇ mermaid · copy source"] : scenario.expect;
      for (const fact of facts) expect(text).toContain(fact);
      for (const fact of scenario.absent) expect(text.includes(fact)).toBe(false);
    });
  }

  test("narrow: the board stacks its three sections", async () => {
    const scenario = (scenarios as any).cards.find((c: any) => c.name === "board");
    const app = await withCard(scenario.card, {}, { width: 390, height: 844 });
    await app.waitFor("id:manager-card");
    const text = shown(app);
    for (const fact of scenario.expect) expect(text).toContain(fact);
  });
});

describe("manager keys and clicks", () => {
  const board = (scenarios as any).cards.find((c: any) => c.name === "board").card;
  const approval = (scenarios as any).cards.find((c: any) => c.name === "approval").card;

  test("cards appear over a dimmed app and Escape closes only the dropdown", async () => {
    const app = await withCard(board);
    await app.waitFor("id:manager-card");
    expect(dropdownScenario.panel_dim).toBe(true);
    expect(app.shows("id:manager-backdrop")).toBe(true);
    expect(shown(app)).toContain("Open Manager session");
    await app.keyboard.press("escape");
    await app.waitFor("id:manager-dropdown", { state: "gone" });
    expect(app.shows("id:manager-backdrop")).toBe(false);
    expect(app.shows("id:manager-indicator")).toBe(true);
    expect(manager(app).some(r=>r.url.endsWith("/api/manager/stop"))).toBe(false);
  });

  test("the live voice's indicator offers Stop on hover and stops the voice on click", async () => {
    const app = await open("./ComposerFixture.tsx", { network, platform: "web", colorScheme: "dark", width: 1280, height: 800,
      storage: { "nexus.manager.dev": JSON.stringify({ level: 0.5, live: true }) } });
    await app.waitFor("id:manager-indicator");
    await app.hover("id:manager-indicator");
    await app.waitFor("id:manager-indicator-stop");
    expect(shown(app)).toContain(dropdownScenario.indicator_stop);
    await app.press("id:manager-indicator");
    await app.wait(100);
    expect(manager(app).some(r=>r.url.endsWith("/api/manager/live/stop"))).toBe(true);
    expect(app.shows("id:manager-indicator")).toBe(true);
  });

  test("header toggles typed input without starting audio", async () => {
    const app = await open("./ComposerFixture.tsx", { network, platform: "web", colorScheme: "dark", width: 1280, height: 800 });
    await app.waitFor("id:open-manager-context");
    await app.press("id:open-manager-context"); await app.waitFor("id:manager-field");
    expect(shown(app)).toContain("Open Manager session");
    expect(manager(app).some(r=>r.url.endsWith("/api/manager/start"))).toBe(false);
    await app.keyboard.press("escape"); await app.waitFor("id:manager-dropdown",{state:"gone"});
  });

  test("input stays in the dropdown and a typed line is sent without opening voice", async () => {
    const app = await withCard(approval); await app.waitFor("id:manager-field");
    await app.type("id:manager-field", "y"); await app.keyboard.press("enter");
    expect(manager(app).some(r=>r.url.endsWith("/api/manager/say"))).toBe(true);
    expect(manager(app).some(r=>r.url.endsWith("/api/manager/live/start"))).toBe(false);
    expect(app.shows("id:manager-field")).toBe(true);
  });

  test("esc and letters at rest belong to the app", async () => {
    const app = await open("./ComposerFixture.tsx", { network, platform: "web", colorScheme: "dark", width: 1280, height: 800 });
    await app.waitFor("id:session-ses_1");
    await app.keyboard.press(`mod+p`);
    await app.waitFor("id:palette");
    await app.keyboard.press("escape");
    await app.waitFor("id:palette", { state: "gone" });
    await app.keyboard.press("1");
    await app.keyboard.press("space");
    expect(app.shows("id:manager-stage")).toBe(false);
    expect(manager(app).length).toBe(0);
  });

  test("at rest nothing of the Manager is drawn, requested or scheduled", async () => {
    const app = await open("./ComposerFixture.tsx", { network, platform: "web", colorScheme: "dark", width: 1280, height: 800 });
    await app.waitFor("id:session-ses_1");
    const before = app.requests().length;
    await app.wait(2500);
    expect(app.count("id:manager-stage")).toBe(0);
    expect(app.count("id:manager-indicator")).toBe(0);
    expect(app.requests().slice(before).filter((r) => r.url.includes("/api/manager")).length).toBe(0);
  });
});
