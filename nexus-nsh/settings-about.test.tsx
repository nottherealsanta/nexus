// The About section names the NSH runtime the app ships against; it must track the pinned CLI.
import { describe, expect, open, test } from "nsh/test";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1, home: "/work/home" } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "t", values: { defaults: {}, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

describe("About", () => {
  test("names the NSH runtime the app ships against", async () => {
    const app = await open("./App.tsx", { network, width: 1280, height: 768 });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-about");
    await app.waitFor("id:settings-page-about");
    const shown = app.texts().join("\n");
    expect(shown).toContain("NSH 0.4.0");
    expect(shown).toContain("Client, Core and API versions");
  });
});
