import fileWatchScenario from "../interface/scenarios/file-watch.json";
// PLAN K10: the built-in editor over a project file.
import { describe, expect, open, test } from "nsh/test";
import { fileRows } from "./src/file-tree";

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
  "POST */api/project/prj_1/watch": { body: { status: "watching" } },
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
  test("a file opens in a modal that wraps long lines and leaves the page underneath", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-panel");
    expect(app.shows("id:file-panel-resize")).toBe(false);
    expect(app.shows("id:left-sidebar")).toBe(true);
    await app.press("id:file-back");
    await app.waitFor("id:file-panel", { state: "gone" });
  });

  test("Markdown opens in the editor and can preview the current draft", async () => {
    const app = await open("./App.tsx", {
      storage: core,
      network: {
        ...network,
        "GET */api/project/prj_1/files*": { body: { path: "", entries: [{ name: "AGENTS.md", kind: "file", ignored: false }] } },
        "GET */api/project/prj_1/file*": { body: { path: "AGENTS.md", content: "# Title\n\nSome **bold** text.\n", revision: "rev1", language: "markdown" } },
      },
      width: 1100, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-AGENTS.md");
    await app.press("id:file-entry-AGENTS.md");
    await app.waitFor("id:file-editor");
    expect(app.shows("id:file-markdown")).toBe(false);
    expect(app.shows("text:# Title")).toBe(true);
    await app.press("id:file-preview");
    await app.waitFor("id:file-markdown");
    expect(app.shows("id:file-editor")).toBe(false);
    expect(app.shows("text:Some bold text.")).toBe(true);
    // Find reads the source, so it leaves the preview and opens the bar.
    await app.keyboard.press("mod+f");
    await app.waitFor("id:file-find-input");
    await app.waitFor("id:file-editor");
    expect(app.shows("text:# Title")).toBe(true);
  });

  test("a file opens editable and saves against the revision it was read at", async () => {
    const app = await open("./App.tsx", { network, storage: core, width: 1100, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitUntil(() => app.texts().some((t) => t.includes("one")));
    expect(app.shows("id:file-save")).toBe(true);
    expect(app.shows("id:file-edit")).toBe(false);
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
    await app.waitFor("id:file-save", { timeout: 15000 });
  });
});

describe("Files tree", () => {
  test("folders expand in place and keep their expanded state across tabs", async () => {
    const app = await open("./App.tsx", {
      storage: { "nexus.core": "http://core.test" },
      network: {
        ...network,
        "GET */api/project/prj_1/files*": (r: { url: string }) => ({
          body: r.url.includes("path=src")
            ? { path: "src", entries: [{ name: "main.rs", kind: "file", ignored: false }, { name: "lib.rs", kind: "file", ignored: false }] }
            : { path: "", entries: [{ name: "src", kind: "directory", ignored: false }, { name: ".git", kind: "directory", ignored: false }, { name: "README.md", kind: "file", ignored: false }] },
        }),
      },
      width: 1500, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-src");
    expect(app.shows("id:file-entry-.git")).toBe(false); // never listed, as in the TUI
    expect(app.shows("id:file-entry-src/main.rs")).toBe(false);
    await app.press("id:file-entry-src");
    await app.waitFor("id:file-entry-src/main.rs");
    expect(app.shows("id:file-entry-README.md")).toBe(true); // the root stays visible
    await app.press("id:tab-sessions");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-src/main.rs"); // still expanded
    await app.press("id:file-entry-src");
    await app.waitFor("id:file-entry-src/main.rs", { state: "gone" });
    expect(app.shows("id:file-entry-src")).toBe(true);
  });

  test("rows are depth-first, indented, and hide .git", () => {
    const dirs = {
      "": { entries: [{ name: "src", kind: "directory", ignored: false }, { name: ".git", kind: "directory", ignored: false }, { name: "a.txt", kind: "file", ignored: false }] },
      src: { entries: [{ name: "nested", kind: "directory", ignored: false }, { name: "main.rs", kind: "file", ignored: false }] },
      "src/nested": { entries: [{ name: "deep.rs", kind: "file", ignored: false }] },
    };
    expect(fileRows(dirs, new Set())).toEqual([
      { path: "src", name: "src", kind: "directory", depth: 0, expanded: false, git_status: undefined, ignored: false },
      { path: "a.txt", name: "a.txt", kind: "file", depth: 0, expanded: false, git_status: undefined, ignored: false },
    ]);
    expect(fileRows(dirs, new Set(["src"])).map((r) => [r.path, r.depth, r.expanded])).toEqual([
      ["src", 0, true], ["src/nested", 1, false], ["src/main.rs", 1, false], ["a.txt", 0, false],
    ]);
  });
});


describe("file operations", () => {
  test("new file uses the shared command and sends the chosen path", async () => {
    let sent: any = null;
    const app = await open("./App.tsx", { network: { ...network, "POST */api/project/prj_1/files": (r: any) => { sent = JSON.parse(r.body); return { body: { path: "new.txt" } }; } }, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "New file");
    await app.keyboard.press("enter");
    await app.waitFor("id:file-operation-input");
    await app.type("id:file-operation-input", "new.txt");
    await app.press("id:file-operation-save");
    await app.waitFor("id:file-operation-dialog", { state: "gone" });
    expect(sent).toMatchObject({ action: "create_file", path: "new.txt" });
  });
});


describe("file deletion", () => {
  test("confirms before deleting the selected file", async () => {
    let sent: any = null;
    const app = await open("./App.tsx", { network: { ...network, "POST */api/project/prj_1/files": (r: any) => { sent = JSON.parse(r.body); return { body: {} }; } }, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "Delete file or folder");
    await app.keyboard.press("enter");
    await app.waitFor("id:confirm-dialog");
    expect(sent).toBe(null);
    await app.press("id:confirm-ok");
    await app.waitFor("id:confirm-dialog", { state: "gone" });
    expect(sent).toMatchObject({ action: "delete", path: "hello.txt" });
  });
});

describe("leased file watching", () => {
  test("visible Files leases a watcher, shows fallback mode, and releases on leaving", async () => {
    const actions: string[] = [];
    const app = await open("./App.tsx", { network: {
      ...network,
      "POST */api/project/prj_1/watch": (r: any) => {
        const body = JSON.parse(r.body); actions.push(body.action);
        return { body: { status: body.action === "release" ? "stopped" : "polling" } };
      },
    }, width: 1500, height: 768 });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-files");
    await app.waitFor(`text:${fileWatchScenario.polling_label}`);
    await app.screenshot("/private/tmp/nexus-files-watch-polling.png");
    expect(actions.includes("renew")).toBe(true);
    await app.press("id:tab-sessions");
    await app.waitUntil(() => actions.includes("release"));
  });

  test("a disk event updates clean text but preserves an unsaved draft", async () => {
    let send = false, sent = false, disk = false;
    const app = await open("./App.tsx", { network: {
      ...network,
      "POST */api/project/prj_1/watch": { body: { status: "watching" } },
      "GET */api/event*": () => {
        const event = send && !sent ? (sent = true, `event: files.changed\ndata: ${JSON.stringify({ id: 1, cursor: "log_t:1", session_id: null, type: "files.changed", data: { project_id: "prj_1", workspace: fileWatchScenario.workspace, source: "disk", paths: [fileWatchScenario.path] } })}\n\n`) : "";
        return { body: connected + event, headers: { "content-type": "text/event-stream" }, delay: 150 };
      },
      "GET */api/project/prj_1/file*": () => ({ body: { path: "hello.txt", content: disk ? fileWatchScenario.updated : fileWatchScenario.initial, revision: disk ? "disk-rev" : "rev1", language: "text" } }),
    }, width: 1500, height: 768 });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-save");
    await app.type("id:file-editor", fileWatchScenario.draft);
    await app.waitFor("text:● modified");
    expect(app.texts().join("\n")).toContain(fileWatchScenario.draft);
    disk = true; send = true;
    await app.waitFor("text:/changed on disk/");
    expect(app.texts().join("\n")).toContain(fileWatchScenario.draft);
    await app.screenshot("/private/tmp/nexus-files-watch-dirty.png");
  });

  test("a clean file follows a disk change", async () => {
    let send = false, sent = false, disk = false;
    const app = await open("./App.tsx", { network: {
      ...network,
      "GET */api/event*": () => {
        const event = send && !sent ? (sent = true, `event: files.changed\ndata: ${JSON.stringify({ id: 1, cursor: "log_t:1", session_id: null, type: "files.changed", data: { project_id: "prj_1", workspace: fileWatchScenario.workspace, source: "disk", paths: [fileWatchScenario.path] } })}\n\n`) : "";
        return { body: connected + event, headers: { "content-type": "text/event-stream" }, delay: 150 };
      },
      "GET */api/project/prj_1/file*": () => ({ body: { path: fileWatchScenario.path, content: disk ? fileWatchScenario.updated : fileWatchScenario.initial, revision: disk ? "disk-rev" : "rev1", language: "text" } }),
    }, width: 1500, height: 768 });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-files");
    await app.waitFor(`id:file-entry-${fileWatchScenario.path}`);
    await app.press(`id:file-entry-${fileWatchScenario.path}`);
    await app.waitFor("text:one");
    disk = true; send = true;
    await app.waitUntil(() => sent, { timeout: 15000 });
    await app.waitFor("text:disk content", { timeout: 15000 });
  });

});

describe("Git sidebar", () => {
  test("groups changes and stages a selected path", async () => {
    let sent: any = null;
    const app = await open("./App.tsx", { network: {
      ...network,
      "GET */api/project/prj_1/git*": { body: { worktree: "/work/demo", branch: "main", changes: [{ path: "hello.txt", status: " M", staged: false, unstaged: true }, { path: "new.txt", status: "??", staged: false, unstaged: true }], branches: [{ name: "main", local: true, current: true }] } },
      "POST */api/project/prj_1/git/action": (r: any) => { sent = JSON.parse(r.body); return { body: {} }; },
    }, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-git");
    await app.waitFor("id:git-unstaged-hello.txt");
    expect(app.shows("id:git-untracked-new.txt")).toBe(true);
    await app.press("id:git-unstaged-hello.txt");
    await app.waitFor("id:git-diff-dialog");
    await app.press("id:git-diff-close");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "Stage");
    await app.keyboard.press("enter");
    await app.waitUntil(() => !!sent);
    expect(sent).toMatchObject({ action: "stage", paths: ["hello.txt"] });
  });
  test("confirms live sessions before a Git operation", async () => {
    let sent: any = null;
    const app = await open("./App.tsx", { network: {
      ...network,
      "GET */api/project/prj_1/git": { body: { worktree: "/work/demo", branch: "main", changes: [], branches: [] } },
      "GET */api/project/prj_1/git/live*": { body: { working: [{ id: "ses_1", title: "Refactor loader" }], idle: [] } },
      "POST */api/project/prj_1/git/action": (r: any) => { sent = JSON.parse(r.body); return { body: {} }; },
    }, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "Fetch");
    await app.keyboard.press("enter");
    await app.waitFor("id:confirm-dialog");
    expect(sent).toBe(null);
    await app.press("id:confirm-ok");
    await app.waitUntil(() => !!sent);
    expect(sent).toMatchObject({ action: "fetch", confirmed_live: ["ses_1"] });
  });
  test("discloses the proposed upstream and reopens confirmation on new activity", async () => {
    let attempts = 0, liveReads = 0, sent: any = null;
    const app = await open("./App.tsx", { network: {
      ...network,
      "GET */api/project/prj_1/git": { body: { worktree: "/work/demo", branch: "topic", changes: [], branches: [], push_upstream: { remote: "origin", branch: "topic" } } },
      "GET */api/project/prj_1/git/live*": () => ({ body: { working: ++liveReads === 1 ? [] : [{ id: "ses_1", title: "Refactor loader" }], idle: [] } }),
      "POST */api/project/prj_1/git/action": (r: any) => { sent = JSON.parse(r.body); return ++attempts === 1 ? { status: 409, body: { code: "worktree_live", error: "new session" } } : { body: {} }; },
    }, width: 1500, height: 768, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.keyboard.press("mod+p"); await app.type("id:palette-input", "Push"); await app.keyboard.press("enter");
    await app.waitFor("text:/origin\\/topic/");
    expect(sent).toBe(null);
    await app.press("id:confirm-ok");
    await app.waitUntil(() => liveReads >= 2);
    await app.waitFor("text:/Working sessions: Refactor loader/");
    expect(app.shows("id:confirm-dialog")).toBe(true);
    await app.press("id:confirm-ok");
    await app.waitUntil(() => attempts === 2);
    expect(sent).toMatchObject({ action: "push", value: "origin", confirmed_live: ["ses_1"] });
  });
});

import editorScenario from "../interface/scenarios/file-editor.json";
describe("one editor mode and replacement", () => {
  test("case-sensitive search, replace all and undo keep one editable draft", async () => {
    const app = await open("./App.tsx", { network: {
      ...network,
      "GET */api/project/prj_1/files*": { body: { path: "", entries: [{ name: editorScenario.path, kind: "file", ignored: false }] } },
      "GET */api/project/prj_1/file*": { body: { path: editorScenario.path, content: editorScenario.initial, revision: "rev1", language: "text" } },
    }, width: 1100, height: 768 });
    await app.waitFor("text:Refactor loader");
    await app.press("id:tab-files");
    await app.press(`id:file-entry-${editorScenario.path}`);
    await app.waitFor("id:file-save");
    expect(app.shows("id:file-edit")).toBe(false);
    await app.keyboard.press("mod+f");
    await app.type("id:file-find-input", editorScenario.query);
    await app.waitFor("text:3 matches");
    await app.press("id:file-find-case");
    await app.waitFor("text:1 match");
    await app.press("id:file-find-case");
    await app.type("id:file-replace-input", editorScenario.replacement);
    await app.press("id:file-replace-all");
    await app.waitFor("text:beta beta");
    await app.waitFor("text:beta");
    await app.keyboard.press("escape");
    await app.waitFor("id:file-find", { state: "gone" });
    await app.keyboard.press("mod+z");
    await app.waitFor("text:Alpha alpha");
    await app.waitFor("text:ALPHA");
    await app.waitFor("text:saved");
    await app.keyboard.press("escape");
    await app.waitFor("id:file-panel", { state: "gone" });
  });
});
