// Timeline visual parity with the TUI: monospace content, delivery tags,
// completed-thought timings and the parent's child card. Existing parity.test.tsx is untouched.
import { describe, expect, open, test } from "nsh/test";

const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Parent", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const emptyTurnPage = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';

function network(body: {
  messages?: unknown[]; turns?: unknown[]; sessions?: unknown[]; running?: unknown[];
}) {
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": {
      body: {
        watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
        projects: [project], sessions: body.sessions ?? [session], running: body.running ?? [],
        approvals: [], questions: [], selected: null,
      },
    },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "GET */api/session/ses_1/message*": { body: body.messages ?? [] },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/turn*": {
      body: body.turns?.length ? { ...emptyTurnPage, turns: body.turns } : emptyTurnPage,
    },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/*": { body: session },
  };
}

async function openSession(body: Parameters<typeof network>[0]) {
  const app = await open("./App.tsx", { network: network(body), width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  return app;
}

const user = (id: string, seq: number, text: string, delivery?: string) => ({
  id, session_id: "ses_1", seq, role: "user", parts: [{ type: "text", text }], created: seq,
  ...(delivery ? { delivery } : {}),
});
const assistant = (id: string, seq: number, text: string, parts: unknown[] = []) => ({
  id, session_id: "ses_1", seq, role: "assistant",
  parts: [...parts, ...(text ? [{ type: "text", text }] : [])], created: seq,
});
const request = (id: string, message_id: string, reasoning_seconds: number, status = "completed") => ({
  id, step: 1, status, model: "scripted/m", effort: null, started: 1, finished: 3, stop: "end", error: null,
  message_id, usage: null, reasoning_seconds, attempts: [],
});
const turn = (requests: unknown[]) => ({
  id: "trn_1", status: "completed", started: 1, finished: 3, start_seq: 0, steps: requests.length,
  stop: "end", ended: { stop: "end", steps: requests.length }, error: null, recovery: "none", requests, tools: [],
});

describe("timeline visual parity", () => {
  test("long user messages offer their complete sent text", async () => {
    const message = Array.from({ length: 45 }, (_, i) => `line ${i + 1}`).join("\n");
    const app = await openSession({ messages: [user("m1", 1, message)] });
    await app.waitFor("id:user-full-1");
    expect(app.texts().join("\n")).toContain("show full message (sent whole)");
    expect(app.texts().join("\n")).not.toContain("line 45");
    await app.press("id:user-full-1");
    await app.waitFor("text:▾ collapse message");
    expect(app.texts().join("\n")).toContain("line 45");
  });

  test("user delivery reads as steer and queued with the turn number, not the raw enum", async () => {
    const app = await openSession({
      messages: [
        user("m1", 1, "first prompt"),
        assistant("m2", 2, "first reply"),
        user("m3", 3, "steer this", "steer"),
        assistant("m4", 4, "steered reply"),
        user("m5", 5, "queued prompt", "queued"),
        assistant("m6", 6, "queued reply"),
      ],
    });
    await app.waitFor("id:msg-user");
    const shown = app.texts().join("\n");
    expect(shown).toContain("first prompt");
    expect(shown).toContain("steer · #1");
    expect(shown).toContain("queued · #2");
  });

  test("completed thought duration comes from its request, and is not attributed across several parts", async () => {
    const app = await openSession({
      messages: [
        user("m1", 1, "inspect the loader"),
        assistant("m2", 2, "done", [{ type: "reasoning", summary: "Looking for the manifest" }]),
        assistant("m3", 3, "more", [
          { type: "reasoning", summary: "First thought" },
          { type: "reasoning", summary: "Second thought" },
        ]),
      ],
      turns: [turn([request("req_1", "m2", 2.1), request("req_2", "m3", 9)])],
    });
    await app.waitFor("text:Thought for 2.1s");
    const shown = app.texts().join("\n");
    expect(shown).toContain("Looking for the manifest");
    expect(shown.includes("Thought for 9.0s")).toBe(false);
  });

  test("an idle background child does not mistake its acknowledgement for completion", async () => {
    const child = {
      ...session, id: "ses_child", title: "Research", parent_id: "ses_1", parent_call_id: "call_1",
      model: "scripted/m", effort: "high", tokens: 1234, created: 10, updated: 65,
    };
    const app = await openSession({
      sessions: [session, child],
      messages: [
        user("m1", 1, "delegate"),
        assistant("m2", 2, "", [{ type: "tool_call", id: "call_1", name: "session", arguments: '{"agent":"research","wait":false}' }]),
        { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "call_1", output: "first line\nsecond line\nlast line" }], created: 3 },
      ],
    });
    await app.waitFor("id:child-meta");
    const shown = app.texts().join("\n");
    expect(shown).toContain("Background");
    expect(shown).toContain("scripted/m/high");
    expect(shown).toContain("started");
    expect(shown).toContain("Show start");
    expect(shown.includes("55s")).toBe(false);
    expect(shown.includes("1.2K tok")).toBe(false);
    await app.press("id:child-result-toggle");
    await app.waitFor("text:Show less");
    expect(app.texts().join("\n")).toContain("last line");
    await app.press("id:child-result-toggle");
    await app.waitFor("text:Show start");
  });

  test("a running background child keeps its start acknowledgement apart from completion", async () => {
    const child = {
      ...session, id: "ses_child", title: "Research", parent_id: "ses_1", parent_call_id: "call_1",
      model: "scripted/m", effort: "high", tokens: 1234, created: 10, updated: 20,
    };
    const app = await openSession({
      sessions: [session, child],
      running: [{ id: "turn_1", session_id: "ses_child" }],
      messages: [
        user("m1", 1, "delegate"),
        assistant("m2", 2, "", [{ type: "tool_call", id: "call_1", name: "session", arguments: '{"agent":"research","wait":false}' }]),
        { id: "m3", session_id: "ses_1", seq: 3, role: "tool", parts: [{ type: "tool_result", call_id: "call_1", output: "started in the background" }], created: 3 },
      ],
    });
    await app.waitFor("text:Show start");
    const shown = app.texts().join("\n");
    expect(shown).toContain("running");
    expect(shown).toContain("scripted/m/high");
    expect(shown.includes("tok")).toBe(false);
    expect(shown.includes("Show result")).toBe(false);
    await app.press("id:child-result-toggle");
    await app.waitFor("text:Show less");
    expect(app.texts().join("\n")).toContain("started in the background");
  });
});
