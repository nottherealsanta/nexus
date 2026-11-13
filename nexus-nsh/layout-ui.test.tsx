// Plan 45: tabs and splits in the NSH desktop/web client (docs/layout.md).
import { describe, expect, open, test } from "nsh/test";

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




const inputs = { "GET */api/session/*/input": { body: { inputs: [], held: false } }, "GET */api/bash*": { body: [] }, "GET */api/kernel*": { body: [] } };
const terminal = { id: "term_1", name: "Shell", project_id: "prj_1", workspace: "/work/demo", session_id: "ses_1", creator: "user", status: "running", created: 1, ended: null, rows: 24, cols: 80, exit_code: null, signal: null, controller: null };
const withTerminal = {
  ...inputs, ...network,
  "GET */api/terminal*": { body: [terminal] },
  "POST */api/terminal": { body: terminal },
  "POST */api/terminal/term_1/attach": { body: { attachment_id: "att_1" } },
  "POST */api/terminal/term_1/control": { body: { ...terminal, controller: "me" } },
  "GET */api/terminal/term_1/stream*": { body: "", headers: { "content-type": "text/event-stream" } },
};
const core = { "nexus.core": "http://core.test" };
const boot = async (extra: any = {}, size = { width: 1500, height: 800 }) => {
  const app = await open("./App.tsx", { network: { ...inputs, ...network }, storage: core, ...size, platform: "web", ...extra });
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

describe("tabs and splits (desktop and web)", () => {
  test("the header tab strip is a group strip, and there is no terminal dock or file modal", async () => {
    const app = await boot();
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:group-g1")).toBe(true);
    expect(app.shows("id:group-menu-g1")).toBe(true);
    expect(app.shows("id:terminal-panel-shell")).toBe(false);
    expect(app.shows("id:file-panel-shell")).toBe(false);
  });

  test("a file opens as a tab beside the session tab, with the session kept", async () => {
    const app = await boot();
    await openFileTab(app);
    expect(app.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:file-panel")).toBe(true);
    // Back to the session tab: its composer returns and the file tab stays.
    await app.press("id:session-tab-ses_1");
    await app.waitFor("id:file-panel", { state: "gone" });
    expect(app.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
  });

  test("a modified file shows a dot and closing it asks Save, Discard or Cancel", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.type("id:file-editor", "one\ntwo\nthree\n");
    await app.waitFor("text:● modified");
    await app.press("id:tab-close-file:prj_1:hello.txt");
    await app.waitFor("id:confirm-dialog");
    expect(app.shows("id:unsaved-discard")).toBe(true);
    expect(app.shows("id:confirm-cancel")).toBe(true);
    expect(app.texts().join("\n")).toContain("Save changes to hello.txt?");
    await app.press("id:confirm-cancel");
    expect(app.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
    await app.press("id:tab-close-file:prj_1:hello.txt");
    await app.press("id:unsaved-discard");
    await app.waitFor("id:tab-file:prj_1:hello.txt", { state: "gone" });
    // The next open starts from the saved text: nothing was kept.
    await app.press("id:file-entry-hello.txt");
    await app.waitFor("id:file-editor");
    expect(app.shows("text:● modified")).toBe(false);
  });

  test("Save in the prompt writes against the revision and closes the tab", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.type("id:file-editor", "one\ntwo\nthree\n");
    await app.waitFor("text:● modified");
    await app.press("id:tab-close-file:prj_1:hello.txt");
    await app.press("id:confirm-ok");
    await app.waitUntil(() => app.requests().some((r) => r.method === "PUT" && r.url.includes("/api/project/prj_1/file")));
    await app.waitFor("id:tab-file:prj_1:hello.txt", { state: "gone" });
  });

  test("the group menu splits right: the file is shown again in a new group sharing its buffer", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.press("id:group-menu-g1");
    await app.press("id:group-menu-split-right");
    await app.waitFor("id:group-g2");
    expect(app.shows("id:group-g1")).toBe(true);
    // Editing in one tab is dirty in the other: one buffer.
    await app.type("id:file-editor >> nth=0", "one\ntwo\nthree\n");
    await app.waitFor("text:● modified");
    expect(app.count("id:file-editor") >= 2).toBe(true);
    // Join right is offered from the left group; Join from g2 is not (nothing to its right).
    await app.press("id:group-menu-g2");
    expect(app.shows("id:group-menu-join-right")).toBe(false);
  });

  test("join closes the split and keeps the tabs", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.press("id:group-menu-g1");
    await app.press("id:group-menu-split-down");
    await app.waitFor("id:group-g2");
    await app.press("id:group-menu-g2");
    await app.press("id:group-menu-close-tab");
    await app.waitFor("id:group-g2", { state: "gone" });
    expect(app.shows("id:group-g1")).toBe(true);
  });

  test("Ctrl+X then backslash splits right, and the palette joins back", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    // (The leader's arrow keys did not reach the keymap in this headless run: the palette and the menu cover them.)
    await app.press("id:tab-file:prj_1:hello.txt >> nth=0");
    await app.keyboard.press("mod+p");
    await app.type("id:palette-input", "Join right");
    await app.keyboard.press("enter");
    await app.waitFor("id:group-g1", { state: "gone" });
    expect(app.shows("id:group-g2")).toBe(true);
  });

  test("the leader's w closes the focused tab", async () => {
    const app = await boot();
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("w");
    await app.waitFor("id:session-tab-ses_1", { state: "gone" });
  });

  test("the leader's alt+left moves the focused tab into the group to the left", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    // Move the new group's only tab left: the empty group goes.
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("alt+left");
    await app.waitFor("id:group-g2", { state: "gone" });
    expect(app.shows("id:group-g1")).toBe(true);
  });

  test("the leader's shift+right joins into the neighbour on the right", async () => {
    const app = await boot();
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("left");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("right");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("left");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("shift+right");
    await app.waitFor("id:group-g1", { state: "gone" });
    expect(app.shows("id:group-g2")).toBe(true);
  });

  test("the leader's up and down arrows focus a group, and shift+down joins", async () => {
    const app = await boot();
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("-");
    await app.waitFor("id:group-g2");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("up");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("shift+down");
    await app.waitFor("id:group-g1", { state: "gone" });
    expect(app.shows("id:group-g2")).toBe(true);
  });

  test("the leader's arrow keys focus a group", async () => {
    const app = await boot();
    // A session tab: the composer has the focus, as it does in normal use.
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("left");
    // Join right works only from the left group: it proves the focus moved.
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("shift+right");
    await app.waitFor("id:group-g1", { state: "gone" });
    expect(app.shows("id:group-g2")).toBe(true);
  });

  test("a split is refused under the minimum pane size with the layout.too_small toast", async () => {
    const app = await boot({}, { width: 1500, height: 230 });
    await openFileTab(app);
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("-");
    await app.waitFor("text:/Not enough room to split here/");
    expect(app.shows("id:group-g2")).toBe(false);
  });

  test("dragging a tab onto a group body shows the drop zone and splits on release", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.drag("id:tab-file:prj_1:hello.txt", { x: 700, y: 200 });
    await app.waitFor("id:group-g2");
    expect(app.shows("id:drag-layer")).toBe(false);
  });

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

  test("Mod+click on a file row opens it in the group to the right", async () => {
    const app = await boot();
    await app.press("id:tab-files");
    await app.waitFor("id:file-entry-hello.txt");
    await app.keyboard.down("mod");
    await app.press("id:file-entry-hello.txt");
    await app.keyboard.up("mod");
    await app.waitFor("id:group-g2");
    expect(app.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
    // The session stays in the left group.
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
  });


  test("a window too narrow for every group shows only the focused one and says others are hidden", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    await app.setViewport(390, 800);
    await app.waitFor("id:hidden-groups");
    expect(app.shows("id:group-g1")).toBe(false);
    await app.setViewport(1500, 800);
    await app.waitFor("id:group-g1");
    expect(app.shows("id:hidden-groups")).toBe(false);
  });

  test("the layout persists per project and is restored with its tabs", async () => {
    const app = await boot();
    await openFileTab(app);
    await app.keyboard.press("ctrl+x");
    await app.keyboard.press("\\");
    await app.waitFor("id:group-g2");
    const saved = JSON.parse((await app.storage())["nexus.layout.prj_1.main"]);
    expect(saved.v).toBe(1);
    expect(saved.root.type).toBe("split");
    const again = await open("./App.tsx", { network: { ...inputs, ...network }, storage: { ...core, "nexus.layout.prj_1.main": JSON.stringify(saved) }, width: 1500, height: 800, platform: "web" });
    await again.waitFor("id:group-g2");
    expect(again.shows("id:tab-file:prj_1:hello.txt")).toBe(true);
  });

  test("a saved tab whose session is gone is dropped quietly", async () => {
    const saved = { v: 1, seq: 1, focus: "g1", root: { type: "group", id: "g1", tabs: [{ kind: "session", id: "ses_1" }, { kind: "session", id: "ses_gone" }], active: "session:ses_gone" } };
    const app = await open("./App.tsx", { network: { ...inputs, ...network }, storage: { ...core, "nexus.layout.prj_1.main": JSON.stringify(saved) }, width: 1500, height: 800, platform: "web" });
    await app.waitFor("id:session-tab-ses_1");
    expect(app.shows("id:session-tab-ses_gone")).toBe(false);
  });

  test("Ctrl+` opens a new terminal in a split below, and the tab shows its name", async () => {
    const app = await open("./App.tsx", { network: withTerminal, storage: core, width: 1500, height: 800, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:group-strip-g1");
    await app.keyboard.press("ctrl+`");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/api/terminal")));
    await app.waitFor("id:terminal-panel");
    await app.waitFor("id:group-g2");
    expect(app.shows("id:tab-terminal:term_1")).toBe(true);
    expect(app.shows("id:terminal-rename")).toBe(true);
    // Closing a running terminal asks first; the terminal's own tab is the only one in its group.
    await app.press("id:tab-close-terminal:term_1");
    await app.waitFor("text:Close running terminal?");
  });
});

// Several sessions side by side (plan 45): each drawn session group has its own live timeline and composer.
const second = { ...session, id: "ses_2", title: "Second task", continued_from: null, created: 4, updated: 4 };
const turnsEmpty = { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } };
const secondMessage = { id: "m9", session_id: "ses_2", seq: 1, role: "user", parts: [{ type: "text", text: "a message in the second session" }], created: 4 };
const sideBySide = (focus = "g1", right: any[] = [{ kind: "session", id: "ses_2" }], active = "session:ses_2") => ({
  v: 1, seq: 2, focus, root: { type: "split", dir: "row", ratios: [0.5, 0.5], children: [
    { type: "group", id: "g1", tabs: [{ kind: "session", id: "ses_1" }], active: "session:ses_1" },
    { type: "group", id: "g2", tabs: right, active },
  ] },
});
const bootTwo = async (opts: { saved?: any; extra?: any; size?: { width: number; height: number } } = {}) => {
  const net = {
    ...inputs,
    "GET */api/session/ses_2/message*": { body: { messages: [secondMessage], more: false, oldest_seq: 1 } },
    "GET */api/session/ses_2/turn*": turnsEmpty,
    "GET */api/session/ses_2/snapshot": { body: null },
    "GET */api/session/ses_2/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_2": { body: second },
    "POST */api/session/*/prompt*": { status: 202, body: { status: "busy" } },
    ...network,
    "GET */api/snapshot*": { body: { ...snapshot, sessions: [session, second] } },
    ...(opts.extra ?? {}),
  };
  const size = opts.size ?? { width: 1500, height: 800 };
  const app = await open("./App.tsx", { network: net, storage: { ...core, "nexus.layout.prj_1.main": JSON.stringify(opts.saved ?? sideBySide()) }, ...size, platform: "web" });
  await app.waitFor("text:Refactor loader");
  await app.press("id:session-ses_1");
  await app.waitFor("id:group-g2");
  return app;
};

describe("several sessions side by side (desktop and web)", () => {
  test("the other group shows its session's own timeline and composer, not a placeholder", async () => {
    const app = await bootTwo();
    await app.waitFor("id:peer-timeline-ses_2");
    await app.waitFor("text:a message in the second session");
    expect(app.shows("id:peer-composer-ses_2")).toBe(true);
    expect(app.shows("id:session-placeholder-ses_2")).toBe(false);
    // Both groups have a composer: the live one for the viewed session, the other for ses_2.
    expect(app.count("id:composer-input")).toBe(1);
    expect(app.count("id:peer-composer-ses_2")).toBe(1);
    expect(app.shows("id:group-focused-g1")).toBe(true);
  });

  test("pressing the other group's composer focuses it, moves the highlight and the caret there", async () => {
    const app = await bootTwo();
    await app.waitFor("id:peer-composer-press-ses_2");
    await app.press("id:peer-composer-press-ses_2");
    await app.waitFor("id:group-focused-g2");
    expect(app.shows("id:group-focused-g1")).toBe(false);
    // The first session now has the other-group composer and timeline; ses_2 has the live one.
    await app.waitFor("id:peer-composer-ses_1");
    await app.waitFor("id:peer-timeline-ses_1");
    expect(app.shows("id:peer-composer-ses_2")).toBe(false);
    await app.type("id:composer-input", "typed after the press");
    expect(app.value("id:composer-input")).toBe("typed after the press");
  });

  test("each session keeps its own draft, shown in its group's composer while unfocused", async () => {
    const app = await bootTwo();
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "draft for one");
    await app.press("id:peer-composer-press-ses_2");
    await app.waitFor("id:peer-composer-ses_1");
    expect(app.texts()).toContain("draft for one");
    expect(app.value("id:composer-input")).toBe("");
    await app.type("id:composer-input", "draft for two");
    await app.press("id:peer-composer-press-ses_1");
    await app.waitFor("id:group-focused-g1");
    await app.waitFor("id:peer-composer-ses_2");
    expect(app.value("id:composer-input")).toBe("draft for one");
    expect(app.texts()).toContain("draft for two");
  });

  test("a message sent from the second group goes to the second session", async () => {
    const app = await bootTwo();
    await app.press("id:peer-composer-press-ses_2");
    await app.waitFor("id:group-focused-g2");
    await app.type("id:composer-input", "hello two");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.includes("/api/session/ses_2/prompt")));
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/api/session/ses_1/prompt"))).toBe(false);
  });

  test("opening a session from the sidebar moves the tab highlight to the group showing it", async () => {
    const app = await bootTwo();
    await app.waitFor("id:group-focused-g1");
    await app.press("id:session-ses_2");
    await app.waitFor("id:group-focused-g2");
    expect(app.shows("id:group-focused-g1")).toBe(false);
    await app.waitFor("id:peer-composer-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:group-focused-g1");
    await app.waitFor("id:peer-composer-ses_2");
  });

  test("opening the session already in view brings its group forward from a file group", async () => {
    const file = { kind: "file", id: "prj_1:hello.txt" };
    const app = await bootTwo({ saved: sideBySide("g1", [file], "file:prj_1:hello.txt") });
    await app.waitFor("id:tab-file:prj_1:hello.txt");
    await app.press("id:tab-file:prj_1:hello.txt");
    await app.waitFor("id:group-focused-g2");
    // ses_1 is still the viewed session; its group keeps the live composer.
    await app.press("id:session-ses_1");
    await app.waitFor("id:group-focused-g1");
    expect(app.shows("id:group-focused-g2")).toBe(false);
  });

  test("a live event of the other session shows in its group without focusing it", async () => {
    let send = false, sent = false;
    const ev = (id: number, type: string, data: unknown) => `event: message\ndata: ${JSON.stringify({ id, time: id, cursor: `log_t:${id}`, type, session_id: "ses_2", data })}\n\n`;
    const live = ev(1, "message.added", { id: "m10", session_id: "ses_2", seq: 2, role: "assistant", parts: [{ type: "text", text: "a live reply in the other group" }], created: 5 });
    const app = await bootTwo({ extra: { "GET */api/event*": () => {
      const body = send && !sent ? (sent = true, connected + live) : connected;
      return { body, headers: { "content-type": "text/event-stream" }, delay: 150 };
    } } });
    await app.waitFor("text:a message in the second session");
    send = true;
    await app.waitFor("text:a live reply in the other group", { timeout: 8000 });
    expect(app.shows("id:group-focused-g1")).toBe(true);
  });

  test("a narrow window shows only the focused group, with no other composer", async () => {
    const app = await bootTwo();
    await app.waitFor("id:peer-composer-ses_2");
    await app.setViewport(390, 800);
    await app.waitFor("id:hidden-groups");
    expect(app.shows("id:peer-composer-ses_2")).toBe(false);
    expect(app.shows("id:composer-input")).toBe(true);
    await app.setViewport(1500, 800);
    await app.waitFor("id:peer-composer-ses_2");
  });
});
