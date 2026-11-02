import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const queued = { id: "in_1", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: "follow up" }] };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
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
  "GET */api/session/ses_1/context": { body: { used: 0, window: null, exact: false } },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: true, inputs: [queued] } },
  "DELETE */api/session/ses_1/input/in_1": { body: queued },
  "GET */api/session/*": { body: session },
};

async function openQueue() {
  const app = await open("./ComposerFixture.tsx", { network, width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:pending-in_1");
  await app.hover("id:pending-in_1");
  await app.waitFor("id:pending-remove-in_1");
  return app;
}

const deletes = (app: { requests: () => { method: string; url: string }[] }) =>
  app.requests().filter((r) => r.method === "DELETE" && r.url.includes("/api/session/ses_1/input/"));

describe("pending queue", () => {
  test("Remove asks first and then deletes the message instead of restoring it", async () => {
    const app = await openQueue();
    await app.press("id:pending-remove-in_1");
    await app.waitFor("id:confirm-dialog");
    const dialog = app.texts().join("\n");
    expect(dialog.includes('Remove the queued message "follow up"?')).toBe(true);
    expect(dialog.includes("This cannot be undone.")).toBe(true);
    expect(deletes(app).length).toBe(0);

    await app.press("id:confirm-cancel");
    await app.waitUntil(() => !app.shows("id:confirm-dialog"));
    expect(deletes(app).length).toBe(0);

    await app.press("id:pending-remove-in_1");
    await app.waitFor("id:confirm-dialog");
    await app.press("id:confirm-ok");
    await app.waitUntil(() => deletes(app).length === 1);
    expect(deletes(app)[0].url.endsWith("/input/in_1")).toBe(true);
    expect(app.value("id:composer-input")).toBe("");
  });

  test("Edit still puts the message back in the draft", async () => {
    const app = await openQueue();
    await app.press("id:pending-edit-in_1");
    await app.waitUntil(() => app.value("id:composer-input") === "follow up");
    expect(app.shows("id:confirm-dialog")).toBe(false);
  });
});
