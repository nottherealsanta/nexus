// Child-session navigation parity with the TUI: subagents stay out of the root tab strip,
// a viewed child keeps its root's tab and has one heading row (title plus metadata), and the
// full-height rail goes back to the root. Child cards remain the way to open subagents.
import { describe, expect, open, test } from "nsh/test";
import { SESSION_BACK_CELLS, headingMeta, headingMetaRoom, sessionBackRailPx } from "./src/ui/header";
import { centreContentPadding } from "./src/ui/centre";
import { textCellPoints } from "./src/ui/geometry";

const parent = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Parent", model: "scripted/m",
  permission: "ask", created: 1, updated: 3, effort: null, agent: "build", parent_id: null,
  archived: 0, revision: 1, disabled_tools: "[]", status: "idle",
};
const child = {
  ...parent, id: "ses_child", title: "Research", agent: "scout", parent_id: "ses_1",
  parent_call_id: "call_1", created: 2, updated: 3,
};
const sibling = {
  ...child, id: "ses_child2", title: "Second look", parent_call_id: "call_2", created: 3,
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const quiet = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };

function network(options: {
  childStatus?: string;
  running?: { id: string; session_id: string }[];
  approvals?: { id: string; session_id: string; tool: string; call_id: string; args: Record<string, unknown> }[];
} = {}) {
  const running = options.running ?? [];
  const approvals = options.approvals ?? [];
  const children = [
    { ...child, status: options.childStatus ?? "idle" },
    { ...sibling, status: "idle" },
  ];
  const snapshot = {
    watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
    projects: [project], sessions: [parent, ...children], running, approvals, questions: [], selected: null,
  };
  const messages = [
    { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "start" }], created: 1 },
    { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "tool_call", id: "call_1", name: "session", arguments: '{"agent":"scout","wait":false,"title":"Research"}' }], created: 2 },
    { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "call_1", output: "the child's answer" }], created: 3 },
  ];
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": { body: snapshot },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "GET */api/project/*/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_1/message*": { body: messages },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/turn*": { body: quiet },
    "GET */api/session/ses_1/input": { body: [] },
    "GET */api/session/ses_1/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_1": { body: parent },
    "GET */api/session/ses_child/message*": { body: [] },
    "GET */api/session/ses_child/snapshot": { body: null },
    "GET */api/session/ses_child/turn*": { body: quiet },
    "GET */api/session/ses_child/input": { body: [] },
    "GET */api/session/ses_child/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_child/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_child": { body: children[0] },
    "GET */api/session/ses_child2/message*": { body: [] },
    "GET */api/session/ses_child2/snapshot": { body: null },
    "GET */api/session/ses_child2/turn*": { body: quiet },
    "GET */api/session/ses_child2/input": { body: [] },
    "GET */api/session/ses_child2/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_child2/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_child2": { body: children[1] },
  };
}

async function openChild(options: Parameters<typeof network>[0] = {}) {
  const app = await open("./App.tsx", { network: network(options), width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:child-card");
  await app.press("id:child-card");
  await app.waitFor("id:session-heading-title");
  return app;
}

describe("child-session navigation parity", () => {
  test("a viewed child keeps its root tab and has one heading row, not a subagent-tab line", async () => {
    const app = await openChild();
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
    expect(app.shows("id:session-tab-ses_child2")).toBe(false);
    expect(app.shows("id:subagent-strip")).toBe(false);
    expect(app.shows("id:session-heading-title")).toBe(true);
    expect(app.texts()).toContain("Scout · m · scripted · default");
    // The child card is still the navigation path into a subagent.
    await app.press("id:session-back");
    await app.waitFor("id:child-card");
    await app.press("id:child-card");
    await app.waitFor("id:session-heading-title");
  });

  test("the heading names a viewed child and the full-height rail returns straight to the root", async () => {
    const app = await openChild();
    expect(app.shows("id:session-heading")).toBe(true);
    expect(app.shows("id:session-heading-title")).toBe(true);
    expect(app.shows("id:session-back-rail")).toBe(true);
    expect(app.shows("id:session-back-arrow")).toBe(true);
    expect(app.shows("id:session-heading-back")).toBe(false);
    await app.press("id:session-back");
    await app.waitFor("id:child-card");
    // The root keeps the blank heading row but no rail and no child name.
    expect(app.shows("id:session-heading")).toBe(true);
    expect(app.shows("id:session-heading-title")).toBe(false);
    expect(app.shows("id:session-heading-meta")).toBe(false);
    expect(app.shows("id:session-back-rail")).toBe(false);
    expect(app.shows("id:subagent-strip")).toBe(false);
  });

  test("the rail is exactly four text cells and reserves that gutter for content", () => {
    expect(SESSION_BACK_CELLS).toBe(4);
    expect(sessionBackRailPx()).toBe(textCellPoints() * 4);
    expect(centreContentPadding(true)).toEqual({ marginLeft: sessionBackRailPx() });
    expect(centreContentPadding(false)).toBe(undefined);
  });

  test("heading metadata stays in stable order and falls back compactly", () => {
    expect(headingMeta(["Scout", "GPT-6", "codex", "high"], 80)).toBe("Scout · GPT-6 · codex · high");
    expect(headingMeta(["Scout", "GPT-6", "codex", "high"], 24)).toBe("Scout·GPT-6·codex·high");
    const tight = headingMeta(["Scout", "GPT-6", "codex", "high"], 14);
    expect(tight.includes("…")).toBe(true);
    expect(headingMeta([], 20)).toBe("");
    // A readable title share is reserved before metadata is compacted.
    expect(headingMetaRoom(70)).toBe(41);
    expect(headingMetaRoom(30)).toBe(15);
    expect(headingMetaRoom(10)).toBe(0);
  });

  test("Left returns a viewed child to the root outside text fields", async () => {
    const app = await openChild();
    await app.keyboard.press("left");
    await app.waitFor("id:child-card");
    expect(app.shows("id:session-heading-title")).toBe(false);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
  });

  test("Left in the composer still edits the draft while a child is viewed", async () => {
    const app = await openChild();
    await app.type("id:composer-input", "ab");
    await app.waitUntil(() => app.value("id:composer-input") === "ab");
    await app.keyboard.press("left");
    expect(app.value("id:composer-input")).toBe("ab");
    expect(app.shows("id:session-heading-title")).toBe(true);
    expect(app.shows("id:session-back-rail")).toBe(true);
  });

  test("Esc returns a viewed child to its parent without dropping the root tab", async () => {
    const app = await openChild();
    await app.keyboard.press("escape");
    await app.waitFor("id:child-card");
    expect(app.shows("id:session-heading-title")).toBe(false);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
  });
});
