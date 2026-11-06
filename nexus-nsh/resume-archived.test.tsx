import { expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/resume-archived.json";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Archived work", model: "scripted/m", permission: "allow", created: 1, updated: 2, agent: "build", parent_id: null, archived: 1, revision: 1, disabled_tools: "[]" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/*": { body: session },
  "POST */api/session/ses_1/unarchive": { body: {} },
  "POST */api/session/ses_1/prompt": { body: {} },
};

test("archived send warns, cancel keeps draft, resume unarchives before sending", async () => {
  const app = await open("./App.tsx", { network, width: 1280, height: 768, platform: "web", colorScheme: "dark" });
  await app.waitFor("id:chip-archived");
  await app.press("id:chip-archived");
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.type("id:composer-input", scenario.prompt);
  await app.press("id:send");
  await app.waitUntil(() => app.texts().includes(scenario.title));
  expect(app.texts().join(" ").includes(scenario.body)).toBe(true);
  expect(app.requests().some((r) => r.method === "POST" && /\/(unarchive|prompt)$/.test(r.url))).toBe(false);
  await app.keyboard.press("escape");
  await app.press("id:send");
  await app.waitUntil(() => app.texts().includes(scenario.title));
  await app.press("text:Resume");
  await app.waitUntil(() => app.requests().some((r) => r.url.endsWith("/prompt")));
  const posts = app.requests().filter((r) => r.method === "POST" && /\/(unarchive|prompt)$/.test(r.url));
  expect(posts[0].url.endsWith("/unarchive")).toBe(true);
  expect(posts[1].url.endsWith("/prompt")).toBe(true);
});

test("failed unarchive leaves the draft and does not send a prompt", async () => {
  const app = await open("./App.tsx", {
    network: { ...network, "POST */api/session/ses_1/unarchive": { status: 409, body: { error: "Tree is busy" } } },
    width: 1280, height: 768, platform: "web", colorScheme: "dark",
  });
  await app.waitFor("id:chip-archived");
  await app.press("id:chip-archived");
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.type("id:composer-input", scenario.prompt);
  await app.press("id:send");
  await app.waitUntil(() => app.texts().includes(scenario.title));
  await app.press("text:Resume");
  await app.waitUntil(() => app.texts().join(" ").includes("Tree is busy"));
  expect(app.requests().some((r) => r.url.endsWith("/prompt"))).toBe(false);
  await app.keyboard.press("escape");
  await app.press("id:send");
  await app.waitUntil(() => app.texts().includes(scenario.title));
});
