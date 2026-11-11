// Fork subagents (PLAN.md section 10), as in the TUI: the parent's `fork` call is a tool row (a minor task run
// like a tool call), not a child card; a fork child reads `fork` in its heading metadata, and its
// timeline folds the messages copied from the parent into one
// `Forked from ‹parent› · N messages` row, collapsed by default and opened like an activity group. The
// `[Nexus fork]` task message after it is an ordinary user message.
import { describe, expect, open, test } from "nsh/test";
import { buildRows, forkSummary } from "./src/ui/timeline";
import { model, timelineOpen } from "./src/store";
import { agentLabel } from "./src/agent-color";

const parent = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Parent work", model: "scripted/m",
  permission: "ask", created: 1, updated: 3, effort: null, agent: "build", parent_id: null,
  archived: 0, revision: 1, disabled_tools: "[]", status: "idle", fork: null,
};
const fork = {
  ...parent, id: "ses_fork", title: "Check tests", agent: "build", parent_id: "ses_1",
  parent_call_id: "call_1", created: 2, updated: 3, fork: { parent_id: "ses_1", inherited: 2 },
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const quiet = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };

const parentMessages = [
  { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "inherited question" }], created: 1 },
  { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "inherited answer" }], created: 2 },
  { id: "m3", session_id: "ses_1", seq: 3, role: "assistant", parts: [{ type: "tool_call", id: "call_1", name: "fork", arguments: '{"wait":false,"title":"Check tests","prompt":"Check the tests"}' }], created: 3 },
];
const forkMessages = [
  { ...parentMessages[0], id: "f1", session_id: "ses_fork" },
  { ...parentMessages[1], id: "f2", session_id: "ses_fork" },
  { id: "f3", session_id: "ses_fork", seq: 3, role: "user", parts: [{ type: "text", text: "[Nexus fork] Check the tests" }], created: 4 },
  { id: "f4", session_id: "ses_fork", seq: 4, role: "assistant", parts: [{ type: "text", text: "fork report" }], created: 5 },
];

function sessionRoutes(id: string, body: unknown, messages: unknown[]) {
  return {
    [`GET */api/session/${id}/message*`]: { body: messages },
    [`GET */api/session/${id}/snapshot`]: { body: null },
    [`GET */api/session/${id}/turn*`]: { body: quiet },
    [`GET */api/session/${id}/input`]: { body: [] },
    [`GET */api/session/${id}/context`]: { body: { used: 0, window: null, exact: false } },
    [`GET */api/session/${id}/worktrees`]: { body: { worktrees: [] } },
    [`GET */api/session/${id}`]: { body },
  };
}

function network(sessions: unknown[] = [parent, fork]) {
  const snapshot = {
    watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
    projects: [project], sessions, running: [], approvals: [], questions: [], selected: null,
  };
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
    ...sessionRoutes("ses_1", parent, parentMessages),
    ...sessionRoutes("ses_fork", fork, forkMessages),
  };
}

async function openParent() {
  const app = await open("./App.tsx", { network: network(), width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("text:1 fork");
  return app;
}

async function openFork() {
  const app = await openParent();
  await app.press("text:1 fork");
  await app.press("id:tool-row-call_1");
  await app.press("id:tool-open-fork-call_1");
  await app.waitFor("text:fork report");
  return app;
}

describe("fork subagent", () => {
  test("the parent's fork call is a tool row, not a child card", async () => {
    const app = await openParent();
    expect(app.count("id:child-card")).toBe(0);
    // Opened, the group shows `Fork ‹title›`; opened again, the row shows the task, then what the parent received.
    await app.press("text:1 fork");
    await app.waitFor("text:Fork");
    await app.press("text:Check tests");
    await app.waitFor("text:  Check the tests");
    await app.waitFor("id:tool-open-fork-call_1");
  });

  test("the heading metadata labels a fork `fork`, not its inherited agent", async () => {
    const app = await openFork();
    await app.waitFor("id:session-heading-title");
    expect(app.texts()).toContain("Fork · m · scripted · default");
    expect(app.shows("id:subagent-strip")).toBe(false);
  });

  test("a fork's inherited messages fold into one row, collapsed by default, opened and closed by its chevron row", async () => {
    const app = await openFork();
    expect(app.texts()).toContain("Forked from Parent work · 2 messages");
    expect(app.texts().some((s) => s.includes("inherited question"))).toBe(false);
    expect(app.texts().some((s) => s.includes("inherited answer"))).toBe(false);
    // The task message is what the model received: shown as a normal user message, then the fork's own work.
    expect(app.texts().some((s) => s.includes("[Nexus fork] Check the tests"))).toBe(true);
    expect(app.texts().some((s) => s.includes("fork report"))).toBe(true);

    await app.press("id:fork-inherited");
    await app.waitFor("text:inherited answer");
    expect(app.texts().some((s) => s.includes("inherited question"))).toBe(true);
    // Indented under the chevron: the user band, the reply and its footer.
    expect(app.count("id:fork-inherited-row")).toBe(3);

    await app.press("id:fork-inherited");
    await app.waitUntil(() => !app.texts().some((s) => s.includes("inherited answer")));
    expect(app.texts()).toContain("Forked from Parent work · 2 messages");
  });

  test("rows: the block replaces the inherited rows (and their footers) at the top, then opens them in order", () => {
    model.sessions.set("ses_1", parent as any);
    model.sessions.set("ses_fork", fork as any);
    model.selected = {
      sessionId: "ses_fork", messages: forkMessages as any, messagesMore: false, turns: [], floor: null, usage: null,
      usageMark: 0, scratch: [], loaded: true, starting: null, turnsGap: null,
    };
    try {
      const closed = buildRows();
      expect(closed.map((r) => r.key)).toEqual(["fork:ses_fork", "f3", "f4-0", "footer:ses_fork:1"]);
      const block = closed[0];
      expect(block.kind === "fork" && block.parent === "Parent work" && block.count === 2 && !block.open).toBe(true);
      // The task message is an ordinary user message, not a notice.
      expect(closed[1].kind).toBe("user");
      timelineOpen.update((o) => ({ ...o, "fork:ses_fork": true }));
      const opened = buildRows();
      expect(opened.map((r) => r.key)).toEqual(["fork:ses_fork", "f1", "f2-0", "footer:ses_fork:0", "f3", "f4-0", "footer:ses_fork:1"]);
      // The copied rows are indented under the block, as a group's members are; the fork's own work is not.
      expect(opened.filter((r) => r.inherited).map((r) => r.key)).toEqual(["f1", "f2-0", "footer:ses_fork:0"]);
      // With older pages unloaded, inherited messages are known by their sequence; an unknown parent shows its id.
      timelineOpen.update((o) => ({ ...o, "fork:ses_fork": false }));
      model.sessions.delete("ses_1");
      model.selected = { ...model.selected!, messages: forkMessages.slice(1) as any, messagesMore: true };
      const paged = buildRows();
      expect(paged.map((r) => r.key)).toEqual(["older-messages:ses_fork", "fork:ses_fork", "f3", "f4-0", "footer:ses_fork:0"]);
      expect(paged[1].kind === "fork" && paged[1].parent === "ses_1").toBe(true);
    } finally {
      model.selected = null;
      model.sessions.delete("ses_1");
      model.sessions.delete("ses_fork");
      timelineOpen.update((o) => { const { ["fork:ses_fork"]: _, ...rest } = o; return rest; });
    }
  });

  test("wording: one message is singular and an unknown parent shows its id", () => {
    expect(forkSummary("Parent work", 1)).toBe("Forked from Parent work · 1 message");
    expect(forkSummary("ses_gone", 12)).toBe("Forked from ses_gone · 12 messages");
  });

  test("the label uses `fork`", () => {
    expect(agentLabel(fork)).toBe("fork");
    expect(agentLabel(parent)).toBe("build");
    expect(agentLabel({ agent: "scout" })).toBe("scout");
  });
});
