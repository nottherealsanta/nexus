// Plan 45: the pointer gestures of the tab layout that only the headless input can drive (docs/layout.md):
// Esc during a drag, Alt-drag copy and Mod/Ctrl+click open-to-the-side. `--target web` refuses `dragAndHold` and
// `keyboard.down`, so this file runs headless (no `--target`) like the other input tests.
import { describe, expect, open, test } from "nsh/test";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [session], running: [], approvals: [], questions: [], selected: null,
};
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: { messages: [], more: false, oldest_seq: null } },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*/input": { body: { inputs: [], held: false } },
  "GET */api/session/ses_1": { body: session },
  "GET */api/session/*": { body: session },
  "GET */api/bash*": { body: [] },
  "GET */api/kernel*": { body: [] },
  "GET */api/project/prj_1/files*": { body: { path: "", entries: [{ name: "hello.txt", kind: "file", ignored: false }] } },
  "POST */api/project/prj_1/watch": { body: { status: "watching" } },
  "GET */api/project/prj_1/file*": { body: { path: "hello.txt", content: "one\ntwo\n", revision: "rev1", language: "text" } },
  "PUT */api/project/prj_1/file*": { body: { path: "hello.txt", revision: "rev2" } },
};
const core = { "nexus.core": "http://core.test" };
const boot = async () => {
  const app = await open("./App.tsx", { network, storage: core, width: 1500, height: 800, platform: "linux" });
  await app.waitFor("text:Refactor loader");
  await app.press("id:session-ses_1");
  await app.waitFor("id:session-tab-ses_1");
  return app;
};
const openFileTab = async (app: any) => {
  await app.press("id:tab-files");
  await app.waitFor("id:file-entry-hello.txt");
  await app.press("id:file-entry-hello.txt");
  await app.waitFor("id:file-editor");
};

describe("tab layout pointer input (plan 45)", () => {
  test("Esc cancels a drag before it is released", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.dragAndHold("id:tab-file:prj_1:hello.txt", { x: 700, y: 200 });
    await app.waitFor("id:drag-layer");
    await app.keyboard.press("escape");
    await app.waitFor("text:Drag cancelled.");
    expect(app.shows("id:drag-layer")).toBe(false);
    await app.drop();
    await app.waitFor("id:drag-layer", { state: "gone" });
    expect(app.shows("id:group-g2")).toBe(false);
  });

  test("Alt-drag copies a tab into a new group", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.keyboard.down("alt");
    await app.drag("id:tab-file:prj_1:hello.txt", { x: 700, y: 200 });
    await app.keyboard.up("alt");
    await app.waitFor("id:group-g2");
    // Both groups show the file: the copy left the source tab in place.
    expect(app.count("id:tab-file:prj_1:hello.txt")).toBe(2);
  });

  test("Ctrl+click on a file row opens it in the group to the right", async () => {
    const app = await boot();
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.keyboard.down("ctrl");
    // A button reports no press payload: the pointer's move over the row is where the keys come from.
    await app.hover("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.keyboard.up("ctrl");
    await app.waitFor("id:group-g2");
    expect(app.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:file-editor")).toBe(true);
  });

  test("Mod+click on a session row opens the session in the group to the right", async () => {
    const app = await boot();
    await app.keyboard.down("mod");
    await app.press("id:session-ses_1");
    await app.keyboard.up("mod");
    await app.waitFor("id:group-g2");
    expect(app.count("id:session-tab-ses_1")).toBe(2);
    expect(app.shows("id:session-placeholder-ses_1")).toBe(true);
  });
});
