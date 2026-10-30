// DESIGN_PLAN I7 (D13): one modal recipe for the palette, pickers, Settings and confirms.
import { describe, expect, open, test } from "nsh/test";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [project], sessions: [], running: [], approvals: [], questions: [], selected: null,
};
const network = (extra: Record<string, unknown> = {}) => ({
  ...extra,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/default": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [{ ref: "codex/gpt-5", name: "GPT-5", context: 200_000 }], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [{ name: "build", source: "builtin" }], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/project/*/worktrees": { body: { worktrees: [] } },
});
const mod = "mod";

describe("modal recipe (I7)", () => {
  test("the palette is a modal with a title, a count and a key-hint footer; Esc closes it", async () => {
    const app = await open("./App.tsx", { network: network(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    await app.keyboard.press(`${mod}+p`);
    await app.waitFor("id:palette");
    expect(app.texts().includes("Commands")).toBe(true);
    expect(app.texts().includes("↑↓ move · enter run · esc close")).toBe(true);
    expect(app.texts().some((t) => /^\d+ commands$/.test(t))).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:palette")).toBe(false);
  });

  test("Settings opens as a modal over the view, which keeps its composer; Esc returns", async () => {
    const app = await open("./App.tsx", { network: network(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    await app.keyboard.press(`${mod}+,`);
    await app.waitFor("id:settings");
    expect(app.count("id:composer-input")).toBe(1); // still mounted under the scrim
    expect(app.texts().includes("esc close")).toBe(true);
    await app.press("id:section-providers");
    await app.waitFor("id:providers-page");
    await app.keyboard.press("escape");
    expect(app.shows("id:settings")).toBe(false);
    expect(app.shows("id:composer-input")).toBe(true);
  });

  test("a picker lists its rows with the choose/close footer and a count", async () => {
    const app = await open("./App.tsx", { network: network(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    await app.keyboard.press(`${mod}+p`);
    await app.type("id:palette-input", "model");
    await app.waitFor("id:cmd-model.change");
    await app.press("id:cmd-model.change");
    await app.waitFor("id:picker");
    expect(app.texts().includes("enter choose · esc close")).toBe(true);
    await app.waitFor("id:pick-0");
  });

  test("a destructive confirm has Cancel as its default: Enter cancels, nothing is deleted", async () => {
    const child = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "T", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
    const app = await open("./App.tsx", {
      network: network({ "GET */api/snapshot*": { body: { ...snapshot, sessions: [child] } }, "DELETE */api/project/prj_1": { status: 204, body: null } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:tab-projects");
    await app.waitFor("id:project-prj_1");
    await app.rightClick("id:project-prj_1");
    await app.press("id:row-action-project.remove");
    await app.waitFor("id:confirm-dialog");
    expect(app.shows("id:confirm-cancel")).toBe(true);
    expect(app.texts().some((t) => t.startsWith("enter cancel"))).toBe(true);
    await app.wait(500); // Enter within 400 ms of opening is ignored
    await app.keyboard.press("enter");
    expect(app.shows("id:confirm-dialog")).toBe(false);
    expect(app.requests().some((r) => r.method === "DELETE")).toBe(false);
  });
});
