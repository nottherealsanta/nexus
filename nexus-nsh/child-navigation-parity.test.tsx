// Child-session navigation parity with the TUI: subagents stay out of the root tab strip,
// a viewed child keeps its root's tab, and the subagent strip goes back to the parent and
// selects children with their own running, waiting or completed state.
import { describe, expect, open, test } from "nsh/test";

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
    "GET */api/session/ses_1/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_1": { body: parent },
    "GET */api/session/ses_child/message*": { body: [] },
    "GET */api/session/ses_child/snapshot": { body: null },
    "GET */api/session/ses_child/turn*": { body: quiet },
    "GET */api/session/ses_child/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_child/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_child": { body: children[0] },
    "GET */api/session/ses_child2/message*": { body: [] },
    "GET */api/session/ses_child2/snapshot": { body: null },
    "GET */api/session/ses_child2/turn*": { body: quiet },
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
  await app.waitFor("id:subagent-strip");
  return app;
}

describe("child-session navigation parity", () => {
  test("a viewed child keeps its root tab and the strip goes back by button and Esc", async () => {
    const app = await openChild();
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
    expect(app.shows("id:session-tab-ses_child2")).toBe(false);
    expect(app.shows("id:subagent-ses_child")).toBe(true);
    expect(app.shows("id:subagent-ses_child2")).toBe(true);
    expect(app.texts().some((t) => t.includes("completed"))).toBe(true);

    await app.press("id:subagent-back");
    await app.waitFor("id:child-card");
    expect(app.shows("id:subagent-strip")).toBe(false);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
  });

  test("Esc returns a viewed child to its parent without dropping the root tab", async () => {
    const app = await openChild();
    await app.keyboard.press("escape");
    await app.waitFor("id:child-card");
    expect(app.shows("id:subagent-strip")).toBe(false);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
  });

  test("the strip shows a running child as running", async () => {
    const app = await openChild({ childStatus: "busy", running: [{ id: "turn_child", session_id: "ses_child" }] });
    expect(app.texts().some((t) => t.includes("running"))).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
  });

  test("choosing a sibling in the strip keeps the same root tab", async () => {
    const app = await openChild();
    await app.press("id:subagent-ses_child2");
    await app.waitFor("id:subagent-current-ses_child2");
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
    expect(app.shows("id:session-tab-ses_child2")).toBe(false);
  });

  test("the strip shows a child waiting for input as waiting", async () => {
    const app = await openChild({
      childStatus: "busy",
      running: [{ id: "turn_child", session_id: "ses_child" }],
      approvals: [{ id: "ap_1", session_id: "ses_child", tool: "bash", call_id: "call_bash", args: { command: "ls" } }],
    });
    expect(app.texts().some((t) => t.includes("waiting"))).toBe(true);
    expect(app.shows("id:session-tab-ses_child")).toBe(false);
  });
});
