import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const queued = { id: "in_1", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: "follow up" }] };
const steered = { id: "in_2", session_id: "ses_1", mode: "steer", parts: [{ type: "text", text: "second thought" }] };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [{ id: "trn_1", session_id: "ses_1" }], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: true, inputs: [queued, steered] } },
  "POST */api/session/ses_1/prompt": { status: 202, body: { status: "busy", turn_id: "trn_2", mode: "interrupt", interrupted: true } },
  "DELETE */api/session/ses_1/input/in_1": { body: queued },
  "DELETE */api/session/ses_1/input/in_2": { body: steered },
  "GET */api/session/*": { body: session },
};

describe("interrupt and send (plan 25)", () => {
  test("Alt+Enter while busy posts mode interrupt and clears the draft", async () => {
    const app = await open("./ComposerFixture.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "stop and do this");
    await app.keyboard.press("alt+enter");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/api/session/ses_1/prompt")));
    const post = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/api/session/ses_1/prompt")) as any;
    expect(JSON.parse(post.body)).toMatchObject({ text: "stop and do this", mode: "interrupt" });
    await app.waitUntil(() => app.value("id:composer-input") === "");
  });
});
