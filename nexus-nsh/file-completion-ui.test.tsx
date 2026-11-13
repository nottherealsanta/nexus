import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/project/prj_1/files/search*": { body: { paths: ["plans/45-tabs-and-splits.md", "plans/folder/"] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
  "GET */api/session/*": { body: session },
};

async function composer() {
  const app = await open("./ComposerFixture.tsx", { network, width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:composer-input");
  return app;
}

describe("file completion", () => {
  test("slash completion still accepts with Tab", async () => {
    const app = await composer();
    await app.type("id:composer-input", "/arch");
    await app.waitFor("id:completion-session.archive");
    await app.keyboard.press("tab");
    await app.waitUntil(() => app.value("id:composer-input") === "/archive ");
  });

  test("searches recursively and Enter selects a file instead of sending", async () => {
    const app = await composer();
    await app.type("id:composer-input", "@45");
    await app.waitUntil(() => app.value("id:composer-input") === "@45");
    await app.waitFor("id:completion-plans/45-tabs-and-splits.md");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.value("id:composer-input") === "@plans/45-tabs-and-splits.md ");
    expect(app.requests().some((r) => r.method === "GET" && r.url.includes("/files/search") && r.url.includes("q=45"))).toBe(true);
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/prompt"))).toBe(false);
  });

  test("Enter on a folder continues completion without sending", async () => {
    const app = await composer();
    await app.type("id:composer-input", "@");
    await app.waitFor("id:completion-plans/folder/");
    await app.keyboard.press("down");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.value("id:composer-input") === "@plans/folder/");
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/prompt"))).toBe(false);
  });

  test("accepts a file at the caret without replacing the rest of the draft", async () => {
    const app = await composer();
    await app.type("id:composer-input", "see @45 afterwards");
    await app.waitUntil(() => app.value("id:composer-input") === "see @45 afterwards");
    await app.waitUntil(() => !app.shows("id:completion"));
    for (let i = 0; i < " afterwards".length; i++) await app.keyboard.press("left");
    await app.waitFor("id:completion-plans/45-tabs-and-splits.md");
    await app.keyboard.press("enter");
    await app.waitUntil(() => app.value("id:composer-input") === "see @plans/45-tabs-and-splits.md  afterwards");
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/prompt"))).toBe(false);
  });
});
