// The timeline follows the end as the TUI does, and keeps measured rows across rebuilds.
import { describe, expect, open, test } from "nsh/test";
import { keepIdentity } from "./src/ui/keep-identity";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Long session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const messages = Array.from({ length: 60 }, (_, i) => ({
  id: `m${i}`, session_id: "ses_1", seq: i + 1, role: i % 2 ? "assistant" : "user", parts: [{ type: "text", text: `Message ${i}\n\nSome text to give the row a height.` }], created: i + 1,
}));
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": {
    body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null },
  },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: messages },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
};

describe("timeline scrolling", () => {
  test("a session opens at its end, scrolling up leaves it, and the jump returns to it", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768 });
    await app.waitFor("text:Long session");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    expect(app.shows("id:jump-latest")).toBe(false);
    // Only a list that opened at its end has anywhere to go up to.
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:jump-latest");
    await app.press("id:jump-latest");
    await app.waitUntil(() => !app.shows("id:jump-latest"));
    // Upward again from the end: the jump really moved the list there.
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:jump-latest");
  });

  test("unchanged rows keep their objects, so the list keeps their measured heights", () => {
    const a = { key: "a", kind: "notice", text: "same" } as any;
    const b = { key: "b", kind: "activity", items: [{ key: "t", text: "x" }] } as any;
    const c = { key: "c", kind: "notice", text: "old" } as any;
    const next = keepIdentity([a, b, c], [{ ...a }, { ...b, items: [{ key: "t", text: "x" }] }, { ...c, text: "new" }, { key: "d" } as any]);
    expect(next[0]).toBe(a);
    expect(next[1]).toBe(b);
    expect(next[2]).not.toBe(c);
    expect((next[2] as any).text).toBe("new");
    expect(next[3].key).toBe("d");
  });
});
