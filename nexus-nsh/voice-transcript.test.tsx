import { expect, open, test } from "nsh/test";
import { appendTranscript } from "./src/dictation-logic";
import scenario from "../interface/scenarios/voice-transcript.json";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Voice transcript", model: "scripted/m", permission: "allow", created: 1, updated: 2, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
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
  "GET */api/session/ses_1/message*": { body: [{ id: "voice_message", session_id: "ses_1", seq: 1, created: 1, role: "user", parts: [{ type: "text", text: scenario.message }] }] },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/*": { body: session },
  "POST */api/session/ses_1/prompt": { body: {} },
};

test("the composer keeps the visible voice notice in the submitted context", async () => {
  const app = await open("./App.tsx", { network, width: 1280, height: 768, platform: "web", colorScheme: "dark" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  // Feed the same completed draft produced by the composer's dictation callback.
  await app.paste("id:composer-input", { text: appendTranscript(scenario.typed, scenario.transcript) });
  await app.waitUntil(() => app.value("id:composer-input") === scenario.message);
  await app.waitUntil(() => app.texts().join(" ").includes(scenario.notice));
  await app.screenshot("/tmp/nexus-voice-transcript-nsh.png");
  await app.press("id:send");
  await app.waitUntil(() => app.requests().some((r) => r.url.endsWith("/prompt")));
  const sent = app.requests().find((r) => r.url.endsWith("/prompt"))!;
  const body = typeof sent.body === "string" ? JSON.parse(sent.body) : sent.body;
  expect(body.text).toBe(scenario.message);
});
