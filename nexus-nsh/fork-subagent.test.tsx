// Fork subagents (PLAN.md section 10), as in the TUI: a fork child reads `fork` wherever its agent name shows
// (the child card, the subagent strip), and its timeline folds the messages copied from the parent into one
// `Forked from ‹parent› · N messages` row, collapsed by default and opened like an activity group. The
// `[Nexus fork]` task message after it is an ordinary user message.
import { describe, expect, open, test } from "nsh/test";
import { stripLayout } from "./src/strip-layout";
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

async function openFork() {
  const app = await open("./App.tsx", { network: network(), width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:child-card");
  return app;
}

describe("fork subagent", () => {
  test("the child card and the subagent strip label a fork `fork`, not its inherited agent", async () => {
    const app = await openFork();
    // The card: `Fork` then the title (the composer below still names the parent's own agent).
    const card = app.texts();
    expect(card[card.indexOf("Check tests") - 1]).toBe("Fork");
    await app.press("id:child-card");
    await app.waitFor("id:subagent-strip");
    await app.waitFor("text:fork report");
    const t = app.texts();
    expect(t[t.lastIndexOf("Check tests") - 1]).toBe("Fork");
  });

  test("a fork's inherited messages fold into one row, collapsed by default, opened and closed by its chevron row", async () => {
    const app = await openFork();
    await app.press("id:child-card");
    await app.waitFor("text:fork report");
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

  test("the label and the strip's width use `fork`", () => {
    expect(agentLabel(fork)).toBe("fork");
    expect(agentLabel(parent)).toBe("build");
    expect(agentLabel({ agent: "scout" })).toBe("scout");
    const wide = stripLayout({ ancestors: [{ id: "r", title: "P" }], children: [{ id: "c", title: "T", agent: "averyverylongagentname", fork: { parent_id: "r", inherited: 1 } }], viewed: "c", room: 400 });
    // `fork` (4) + 4 columns + the title: 12, not the long agent name's 30.
    expect(wide.cell).toBe(12);
  });
});
