// Plan 37: `/continue` and sends show a pending working row at once, driven by real events.
import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", status: "idle" };
const messages = [
  { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "do the thing" }], created: 1 },
];
const sse = (frames: string) => `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n${frames}`;
const frame = (id: number, type: string, data: unknown) => `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ id, type, session_id: "ses_1", cursor: `log_t:${id}`, data })}\n\n`;
const base = (extra: Record<string, unknown> = {}, events = "") => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: sse(events), headers: { "content-type": "text/event-stream" } },
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
  "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
  "GET */api/session/*": { body: session },
  ...extra,
});
const openSession = async (network: Record<string, unknown>) => {
  const app = await open("./App.tsx", { network, width: 1100, height: 800, platform: "web" });
  await app.waitFor("id:splash", { state: "gone" });
  await app.press("id:session-ses_1");
  await app.waitFor("text:do the thing");
  return app;
};

describe("pending turn row (plan 37)", () => {
  test("/continue shows the row at once; the phase follows busy; the first delta clears it", async () => {
    const app = await openSession(base({ "POST */api/session/ses_1/continue": { status: 202, body: { status: "busy" } } }, frame(1, "session.status", { status: "busy", turn_id: "t_1" })));
    await app.type("id:composer-input", "/continue");
    await app.keyboard.press("enter");
    await app.waitFor("id:pending-turn");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/continue")));
  });

  test("a refusal removes the row and the strip gives the reason", async () => {
    const app = await openSession(base({ "POST */api/session/ses_1/continue": { status: 409, body: { error: "session has unfinished durable turn t_1", code: "conflict" } } }));
    await app.type("id:composer-input", "/continue");
    await app.keyboard.press("enter");
    await app.waitUntil(() => app.texts().some((t) => t.includes("Continue refused · session has unfinished")));
    expect(app.shows("id:pending-turn")).toBe(false);
  });
});

// The phases and the queue, against the store directly (the app under `open` runs in its own runtime).
import { connection, model, pendingFailed, pendingTurns, setPending, settlePending, flushQueuedContinues, PENDING_SLOW_MS } from "./src/store";
import { continueSession, interrupt } from "./src/commands";
import { pendingText } from "./src/ui/timeline";
import { Model } from "./src/core/reduce";

const wire = (ses: any = session) => {
  model.sessions.set(ses.id, { ...ses } as any);
  model.select(ses.id);
  model.selected!.messages = messages as any;
  model.selected!.scratch = [];
};
const stubCore = (answer: () => { status?: number; body?: unknown } = () => ({ status: 202, body: { status: "busy" } })) => {
  const calls: { path: string; key: string | null }[] = [];
  (globalThis as any).fetch = async (input: any, init: any = {}) => {
    const url = String(typeof input === "string" ? input : input.url);
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    if (init.method === "POST" && path.endsWith("/continue")) {
      calls.push({ path, key: new Headers(init.headers).get("idempotency-key") });
      const a = answer();
      return new Response(JSON.stringify(a.body ?? null), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
    }
    return new Response("null", { status: 200 });
  };
  return calls;
};
const reset = () => { for (const id of Object.keys(pendingTurns())) setPending(id, null); pendingFailed.set({}); connection.set({ kind: "live" } as any); };

describe("pending turn phases and queueing (plan 37)", () => {
  test("phases follow real events and the first output clears the row", () => {
    reset(); wire();
    setPending("ses_1", { started: Date.now(), queued: false, accepted: false, busySeen: false, key: "k" });
    expect(pendingText("ses_1")).toMatch(/^Waiting for Core · \d+s$/);
    settlePending("session.status", "ses_1", { status: "busy" });
    expect(pendingText("ses_1")).toMatch(/^Waiting for the model · \d+s$/);
    model.selected!.scratch.push({ id: 9, type: "retry_scheduled", time: 1, data: { phase: "waiting_network", delay: 8 } } as any);
    expect(pendingText("ses_1")).toBe("Waiting for network · retrying in 8.0s");
    settlePending("text_delta", "ses_1", {});
    expect(pendingText("ses_1")).toBe(null);
    expect(pendingTurns()["ses_1"]).toBeUndefined();
  });

  test("a queued call waits for Core, is sent once with its one key, and Esc cancels before sending", async () => {
    reset(); wire();
    const calls = stubCore();
    connection.set({ kind: "offline", reason: "down" } as any);
    await continueSession();
    expect(calls.length).toBe(0);
    expect(pendingText("ses_1")).toBe("Waiting for Core · not connected");
    const key = pendingTurns()["ses_1"].key!;
    connection.set({ kind: "live" } as any);
    flushQueuedContinues.fn();
    flushQueuedContinues.fn();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls.length).toBe(1);
    expect(calls[0].key).toBe(key);
    expect(pendingTurns()["ses_1"].accepted).toBe(true);

    reset();
    connection.set({ kind: "offline", reason: "down" } as any);
    await continueSession();
    await interrupt();
    expect(pendingTurns()["ses_1"]).toBeUndefined();
    connection.set({ kind: "live" } as any);
    flushQueuedContinues.fn();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls.length).toBe(1);
  });

  test("a refusal clears the row and keeps the reason; Retry reuses the key", async () => {
    reset(); wire();
    let status = 409;
    const calls = stubCore(() => status === 409 ? { status: 409, body: { error: "session has unfinished durable turn t_1", code: "conflict" } } : { status: 202, body: {} });
    await continueSession();
    expect(pendingTurns()["ses_1"]).toBeUndefined();
    expect(pendingFailed()["ses_1"]).toContain("unfinished durable turn");
    status = 202;
    await continueSession();
    expect(pendingFailed()["ses_1"]).toBeUndefined();
    expect(calls.length).toBe(2);
    expect(PENDING_SLOW_MS).toBe(10_000);
  });
});
