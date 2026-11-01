// PLAN K10: the built-in editor over a project file.
import { describe, expect, open, test } from "nsh/test";
import { FILE_PANEL_MIN, panelDefaultFor, panelMaxFor, panelWidthFor } from "./src/ui/editor";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const original = { id: "ses_0", project_id: "prj_1", directory: "/work/demo", title: "Original work", model: "", permission: "ask", created: 3, updated: 3, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", continued_to: "ses_1" };
const session = { continued_from: "ses_0", id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [session], running: [], approvals: [], questions: [], selected: null,
};
const summary = "## Goal\nFix the login bug.\n## State\nHalf done.\n## Decisions\nNone.\n## Next steps\nRun the tests.";
const messages = [
  { id: "m1", session_id: "ses_1", seq: 1, role: "user", delivery: "handoff", parts: [{ type: "text", text: `[Handoff from session ses_abcdef123456]\n\n${summary}` }], created: 1 },
  { id: "m2", session_id: "ses_1", seq: 2, role: "user", parts: [{ type: "text", text: "carry on" }], created: 2 },
];
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: { messages: [], more: false, oldest_seq: null } },
  "GET */api/project/prj_1/files*": { body: { path: "", entries: [{ name: "hello.txt", kind: "file", ignored: false }] } },
  "GET */api/project/prj_1/file*": { body: { path: "hello.txt", content: "one\ntwo\n", revision: "rev1", language: "text" } },
  "PUT */api/project/prj_1/file*": { body: { path: "hello.txt", revision: "rev2" } },
  "GET */api/session/ses_0/message*": { body: [] },
  "GET */api/session/ses_0/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_0/snapshot": { body: null },
  "GET */api/session/ses_0/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_0": { body: original },
  "GET */api/session/ses_1": { body: session },
  "GET */api/session/*": { body: session },
};



describe("Built-in editor (K10)", () => {
  // Native builds (the macOS run) start at the Connect screen without a saved
  // address; the web build ignores this key and uses its same-origin host.
  const core = { "nexus.core": "http://core.test" };
  test("the file panel width can be dragged from 360 px up to the space minus 480 px", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-panel-resize");
    // 1500 px window, 264 px sidebar: the default stays 55% capped at 760; a drag may pass it.
    const space = 1500 - 264;
    const def = panelDefaultFor(space);
    expect(panelWidthFor(space, null)).toBe(def);
    expect(def).toBe(679);
    expect(panelWidthFor(space, def + 50)).toBe(def + 50);
    expect(panelWidthFor(space, 5000)).toBe(space - 480);
    expect(panelMaxFor(1800)).toBeGreaterThan(760);
    expect(panelWidthFor(1800, 1200)).toBe(1200);
    expect(panelWidthFor(space, 100)).toBe(FILE_PANEL_MIN);
    expect(panelMaxFor(700)).toBe(FILE_PANEL_MIN);
  });

  test("a file opens read-only, edits, and saves against the revision it was read at", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1100, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitUntil(() => app.texts().some((t) => t.includes("one")));
    expect(app.shows("id:file-save")).toBe(false);
    await app.press("id:file-edit");
    await app.waitFor("id:file-editor");
    expect(app.shows("id:file-save")).toBe(true);
    await app.type("id:file-editor", "one\ntwo\nthree\n");
    await app.waitFor("text:● modified");
    await app.press("id:file-diff");
    await app.waitFor("id:file-diff-view");
    expect(app.shows("id:file-editor")).toBe(false);
    await app.press("id:file-diff");
    await app.waitFor("id:file-editor");
  });

  test("find in file opens a bar with a case toggle and closes on Esc", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1100, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-editor");
    await app.keyboard.press("mod+f");
    await app.waitFor("id:file-find-input");
    await app.type("id:file-find-input", "one");
    await app.waitFor("text:1 match");
    await app.press("id:file-find-next");
    await app.waitFor("text:1 of 1");
    expect(app.shows("id:file-find-case")).toBe(true);
    await app.keyboard.press("escape");
    await app.waitFor("id:file-find", { state: "gone" });
    expect(app.shows("id:file-panel")).toBe(true);
  });

  test("the panel keeps its editor and text across the layout breakpoint", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1200, height: 800, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-panel");
    await app.press("id:file-edit");
    await app.waitFor("id:file-editor");
    await app.type("id:file-editor", "one\ntwo\nthree\n");
    await app.waitFor("text:● modified");
    // Below 1100 the panel covers the body; above it, it sits beside the timeline.
    await app.setViewport(900, 800);
    await app.waitFor("id:file-editor");
    expect(app.shows("id:file-panel")).toBe(true);
    expect(app.shows("text:● modified")).toBe(true);
    await app.setViewport(1200, 800);
    await app.waitFor("id:file-editor");
    expect(app.shows("text:● modified")).toBe(true);
  });

  test("Back closes a clean file panel", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1100, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-panel");
    await app.press("id:file-back");
    await app.waitFor("id:file-panel", { state: "gone" });
  });

  test("the editor opens a 1 MiB file", async () => {
    const line = "let value = compute(alpha, beta, gamma); // a line of source\n";
    const content = line.repeat(Math.floor((1024 * 1024) / line.length));
    const app = await open("./App.tsx", {
      storage: core,
      network: {
        ...network,
        "GET */api/project/prj_1/files*": { body: { path: "", entries: [{ name: "big.rs", kind: "file", ignored: false }] } },
        "GET */api/project/prj_1/file*": { body: { path: "big.rs", content, revision: "rev1", language: "rust" } },
      },
      width: 1100,
      height: 768,
      platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-big.rs");
    await app.press("id:file-entry-big.rs");
    await app.waitFor("id:file-editor", { timeout: 15000 });
    expect(app.shows("id:file-editor")).toBe(true);
    await app.press("id:file-edit");
    await app.waitFor("id:file-save", { timeout: 15000 });
  });
});
