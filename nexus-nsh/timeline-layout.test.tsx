// Regression coverage for the shared timeline reading column: the new-session
// starting context and the started session use the same column, card and inset.
import { describe, expect, open, test } from "nsh/test";
import { ACTIVITY_CONTROL_INSET, ACTIVITY_INDENT, CARD_INSET, TEXT_INDENT, timelineColumnClass, timelineColumnPx } from "./src/ui/reading-column";

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
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/*": { body: session },
  };
}

describe("timeline reading column", () => {
  test("horizontal insets grow with the window and keep a narrow screen readable", () => {
    expect(timelineColumnClass(390)).toBe("px-6");
    expect(timelineColumnClass(819)).toBe("px-6");
    expect(timelineColumnClass(820)).toBe("px-12");
    expect(timelineColumnClass(1024)).toBe("px-12");
    expect(timelineColumnClass(1200)).toBe("px-16");
    expect(timelineColumnClass(1599)).toBe("px-16");
    expect(timelineColumnClass(1600)).toBe("px-20");
    // The old fixed 40 px gutter is gone at every desktop width.
    expect(timelineColumnPx(390)).toBe(24);
    expect(timelineColumnPx(1024)).toBe(48);
    expect(timelineColumnPx(1280)).toBe(64);
    expect(timelineColumnPx(1600)).toBe(80);
  });

  test("cards, prose and activity rows share one text edge", () => {
    expect(CARD_INSET).toBe("px-4");
    expect(TEXT_INDENT).toBe("pl-4");
    expect(ACTIVITY_INDENT).toBe("pl-3");
    expect(ACTIVITY_CONTROL_INSET).toBe("px-1");
  });

  test("the new-session starting context renders in the timeline's shared column and card", async () => {
    const app = await open("./App.tsx", { network: network([], null), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:new-session-column");
    await app.waitFor("id:context-section");
    expect(app.shows("id:context-sections")).toBe(true);
    expect(app.shows("id:new-session")).toBe(true);
    expect(app.shows("id:new-session-column")).toBe(true);
  });

  test("the started-session context uses the same column and card as the draft", async () => {
    const app = await open("./App.tsx", { network: network([session], starting), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:context-section");
    expect(app.shows("id:context-sections")).toBe(true);
    expect(app.count("id:timeline-row")).toBe(1);
    expect(app.shows("id:new-session-column")).toBe(false);
  });
});
