// The Void design's second pass: the composer's measured selector, the working row while
// waiting, and a session row's one-line shape.
import { describe, expect, open, test } from "nsh/test";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const base = {
  project_id: "prj_1", directory: "/work/demo", model: "", permission: "ask", effort: null, agent: "build",
  parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const first = { ...base, id: "ses_1", title: "Refactor loader", created: 1, updated: 2, model: "acmeprov/big-model-very-long-name" };
const second = { ...base, id: "ses_2", title: "Parent of two", created: 3, updated: 4 };
const child = { ...base, id: "ses_3", title: "Research", created: 5, updated: 6, parent_id: "ses_2", parent_call_id: "call_1" };
const approval = { id: "apr_1", session_id: "ses_1", tool: "bash", call_id: "c1", args: { command: "pytest -q" } };
const models = { source: "t", model: "acmeprov/big-model-very-long-name", models: [{ ref: "acmeprov/big-model-very-long-name", provider: "acmeprov", name: "Big Model Name", context: 200000 }] };

function snapshot(extra: Record<string, unknown> = {}, sessions = [first, second, child]) {
  return {
    watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
    projects: [project], sessions, running: [], approvals: [], questions: [], selected: null, ...extra,
  };
}

function network(snap: unknown) {
  const empty = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": { body: snap },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/model/last*": { body: { model: "acmeprov/big-model-very-long-name" } },
    "GET */api/model*": { body: models },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "GET */api/session/ses_1/message*": { body: [] },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/turn*": { body: empty },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_2/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_3/worktrees": { body: { worktrees: [] } },
    "GET */api/session/*": { body: first },
  };
}

const view = (snap: unknown, width = 1024) =>
  open("./App.tsx", { network: network(snap), width, height: 768, platform: "web" });

describe("Void second pass", () => {
  test("the model selector keeps the provider in a wide dock and drops it in a narrow one", async () => {
    const wide = await view(snapshot());
    await wide.waitFor("text:Refactor loader");
    await wide.press("id:session-ses_1");
    await wide.waitFor("id:composer-model");
    expect(wide.texts().includes("acmeprov")).toBe(true);
    const narrow = await view(snapshot(), 500);
    await narrow.waitFor("id:header");
    await narrow.press("id:toggle-left");
    await narrow.waitFor("id:session-ses_1");
    await narrow.press("id:session-ses_1");
    await narrow.waitFor("id:composer-model");
    expect(narrow.texts().includes("acmeprov")).toBe(false);
    expect(narrow.texts().includes("Big Model Name")).toBe(true);
  });

  test("the working row steps aside while this session waits on the user", async () => {
    const busy = [first, second, child].map((s) => (s.id === "ses_1" ? { ...s, status: "busy" } : s));
    const app = await view(snapshot({ sessions: busy, approvals: [approval] }, busy));
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:waiting-card");
    expect(app.shows("id:working-row")).toBe(false);
    expect(app.shows("id:waiting-card")).toBe(true);
  });

  test("a session row with nothing on its second line is one line", async () => {
    const app = await view(snapshot());
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:session-line-ses_1")).toBe(false);
    // The parent has a child, so it keeps its second line (the child count).
    expect(app.shows("id:session-line-ses_2")).toBe(true);
    expect(app.texts().includes("1 ↳")).toBe(true);
  });
});
