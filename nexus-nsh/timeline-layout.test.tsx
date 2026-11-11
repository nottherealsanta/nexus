// Regression coverage for the shared timeline reading column: the new-session
// starting context and the started session use the same column, card and inset.
import { describe, expect, open, test } from "nsh/test";
import { ACTIVITY_CHEVRON, CARD_INSET, TEXT_INDENT, TEXT_INSET, TURN_INSET, USER_BAND_INSET, timelineColumnClass, timelineColumnPx } from "./src/ui/reading-column";
import { iconGlyphs } from "./src/ui/kit";
import { sidebarGlyphs, verbGlyphs, structure } from "./src/gen/theme";
import { contextGrid, defaultLeftSidebarWidth, rightSidebarWidth, workspaceTabCells } from "./src/ui/geometry";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Parent", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const quietTurns = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
const starting = {
  origin: "created", system_prompt: "You are coding in Nexus.", tools: ["read", "bash"], tool_definitions: { read: { name: "read" } },
  instruction_file: { path: "/work/demo/AGENTS.md", status: "loaded", bytes: 20, content: "Project instructions" },
};

function network(sessions: unknown[], snapshot: unknown | null) {
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": {
      body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions, running: [], approvals: [], questions: [], selected: null },
    },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent/build": { body: { prompt: "Build prompt" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/project/prj_1/file*": { body: { content: "Project instructions" } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "POST */api/draft/context": { body: { used: 1200, parts: { instructions: 1000, tools: 200, instruction_file: 20 }, tools: ["read", "bash"], skills: [], mcp: [] } },
    "GET */api/session/ses_1/message*": { body: [] },
    "GET */api/session/ses_1/snapshot": { body: snapshot },
    "GET */api/session/ses_1/turn*": { body: quietTurns },
    "GET */api/session/ses_1/input": { body: [] },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/*": { body: sessions[0] ?? session },
  };
}

describe("timeline reading column", () => {
  test("context columns follow the longest name instead of stretching to fill the page", () => {
    const names = ["read", "bash", "edit", "write"];
    expect(contextGrid(names, 800, 4).columns).toBe(4);
    expect(contextGrid(names, 120, 4).columns).toBe(1);
    expect(contextGrid(names, 800, 4).cell < 200).toBe(true);
    expect(contextGrid(["a very long server name"], 120, 2).cell <= 88).toBe(true);
  });

  test("workspace tabs share the longest title's width, with room for status and close", () => {
    expect(workspaceTabCells([{ title: "New session", session: false }], 160)).toBe(16);
    expect(workspaceTabCells([{ title: "A", session: true }, { title: "New session", session: false }], 160)).toBe(16);
    expect(workspaceTabCells([{ title: "x".repeat(60), session: true }], 20)).toBe(15);
  });

  test("fixed compact gutters leave the available width to the conversation", () => {
    for (const width of [390, 640, 819, 820, 1024, 1200, 1599, 1600, 1920]) {
      expect(timelineColumnClass(width)).toBe("px-3");
      expect(timelineColumnPx(width)).toBe(12);
    }
  });

  test("sidebar defaults match the Rust TUI's 28/30 and 34/36 cell widths", () => {
    expect(defaultLeftSidebarWidth(960)).toBe(28 * 8);
    expect(defaultLeftSidebarWidth(1280)).toBe(34 * 8);
    expect(rightSidebarWidth(960)).toBe(30 * 8);
    expect(rightSidebarWidth(1280)).toBe(36 * 8);
  });

  test("controls and tool verbs use the Rust TUI's shared text glyphs", () => {
    expect(iconGlyphs["panel-left"]).toBe(sidebarGlyphs.left_open.unicode);
    expect(iconGlyphs.sessions).toBe(sidebarGlyphs.tab_sessions.unicode);
    expect(iconGlyphs.read).toBe(verbGlyphs.read.unicode);
    expect(iconGlyphs.shell).toBe(verbGlyphs.shell.unicode);
    expect(iconGlyphs.x).toBe(structure.close.unicode);
    for (const glyph of Object.values(iconGlyphs)) expect(glyph.length > 0).toBe(true);
  });

  test("cards, prose and activity rows share one text edge", () => {
    expect(CARD_INSET).toBe("px-2");
    expect(TEXT_INDENT).toBe("pl-2");
    expect(TEXT_INSET).toBe("px-2");
    expect(TURN_INSET).toBe("pl-4");
    expect(USER_BAND_INSET).toBe("pl-4 pr-2");
    expect(ACTIVITY_CHEVRON).toBe("w-4 shrink-0 -ml-1");
  });

  test("the new-session starting context renders in the timeline's shared column and card", async () => {
    const app = await open("./App.tsx", { network: network([], null), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:new-session-column");
    await app.waitFor("id:context-section");
    expect(app.shows("id:context-sections")).toBe(true);
    expect(app.shows("id:new-session")).toBe(true);
    expect(app.shows("id:new-session-column")).toBe(true);
    expect(app.shows("id:toggle-left")).toBe(true);
    expect(app.shows("id:send")).toBe(true);
    expect(app.texts().includes("Send message")).toBe(false);
    expect(app.shows("id:tab-sessions")).toBe(true);
    expect(app.shows("id:tab-files")).toBe(true);
    expect(app.shows("id:right-rail")).toBe(false);
    expect(app.shows("id:composer-bottom-edge")).toBe(true);
    expect(app.shows("id:activity-bar")).toBe(true);
  });

  test("the started-session context uses the same column and card as the draft", async () => {
    const app = await open("./App.tsx", { network: network([session], starting), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:context-section");
    expect(app.shows("id:context-sections")).toBe(true);
    expect(app.count("id:timeline-row")).toBe(1);
    expect(app.shows("id:new-session-column")).toBe(false);
    // The root session keeps the blank heading row; it does not repeat the tab title.
    expect(app.shows("id:session-heading")).toBe(true);
    expect(app.shows("id:session-heading-title")).toBe(false);
    expect(app.shows("id:session-heading-meta")).toBe(false);
    expect(app.shows("id:session-back-rail")).toBe(false);
    expect(app.shows("id:subagent-strip")).toBe(false);
  });

  test("a scrolling tab title stays in its own region and leaves the close slot fixed", async () => {
    const long = { ...session, title: "A deliberately long session title that keeps scrolling past its tab and close" };
    const app = await open("./App.tsx", { network: network([long], null), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor(`id:tab-title-clip-${session.id}`);
    await app.hover(`id:session-tab-${session.id}`);
    await app.wait(900);
    expect(app.shows(`id:tab-title-clip-${session.id}`)).toBe(true);
    expect(app.shows(`id:tab-close-${session.id}`)).toBe(true);
    expect(app.texts().some((t) => t.includes(long.title))).toBe(true);
  });

  test("a narrow header keeps a title instead of squeezing fixed-width tabs", async () => {
    const app = await open("./App.tsx", { network: network([], null), width: 640, height: 384, platform: "web" });
    await app.waitFor("id:narrow-workspace-title");
    expect(app.shows("id:new-session-tab-add")).toBe(false);
    expect(app.shows("id:send")).toBe(true);
  });
});
