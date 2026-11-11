// The phone client's routes for plans 29 and 30: `/search`, `/archive` and a question card.
// Run without `--target` so the app runs in the phone idiom (390x844, platform "ios").
import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/session-controls.json";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = {
  id: "ses_1", project_id: "prj_1", directory: scenario.directory, title: "Migration plan", model: "scripted/m",
  permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0,
  revision: 1, disabled_tools: "[]",
};
const second = { ...session, id: "ses_2", directory: "/work/demo", title: "Chores", created: 2, updated: 3 };
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };

interface Flags { archive?: boolean; questions?: unknown[] }
function network(flags: Flags = {}) {
  const frames = () => flags.archive
    ? `event: message\ndata: ${JSON.stringify({ id: 2, time: 2, type: "session.archived", session_id: "ses_1", data: { root: "ses_1", sessions: ["ses_1"], archived: true, revisions: { ses_1: 2 } } })}\n\n`
    : "";
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/snapshot*": { body: {
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects: [project], sessions: [session, second], running: [], approvals: [], questions: flags.questions ?? [], selected: null,
    } },
    "GET */api/event*": () => ({ body: connected + frames(), headers: { "content-type": "text/event-stream" } }),
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
    "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
    "GET */api/session/ses_2/message*": { body: [] },
    "GET */api/session/ses_2/snapshot": { body: null },
    "GET */api/session/ses_2/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_2/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
    "GET */api/session/ses_2/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_2/input": { body: { held: false, inputs: [] } },
    "POST */api/session/ses_1/archive": () => { flags.archive = true; return { body: { ...session, archived: true } }; },
    "POST */api/session/ses_1/question/qst_controls": { body: {} },
    "GET */api/search*": { body: { hits: [] } },
    "GET */api/session/ses_1": { body: session },
    "GET */api/session/ses_2": { body: second },
    "GET */api/session/*": { body: session },
  } as Record<string, any>;
}

/** The cut-down phone client. The Core address is a non-loopback name, so no real home is read. */
async function openPhone(flags: Flags = {}) {
  const app = await open("./App.tsx", {
    network: network(flags), width: 390, height: 844, platform: "ios",
    storage: { "nexus.core": "http://nexus.invalid:4747" },
  });
  await app.waitFor("id:splash", { state: "gone" });
  return app;
}

async function openConversation(app: any) {
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:composer-input");
}

describe("phone routes (plans 29 and 30)", () => {
  test("/search opens Sessions with its search field focused", async () => {
    const app = await openPhone();
    await openConversation(app);
    await app.type("id:composer-input", `/search ${scenario.search}`);
    await app.submit("id:composer-input");
    await app.waitFor("id:session-search");
    expect(app.value("id:session-search")).toBe(scenario.search);
    // No second tap: the field already has focus, so a key edits it.
    await app.keyboard.press("backspace");
    await app.waitUntil(() => app.value("id:session-search") === "migratio");
    expect(app.shows("id:session-ses_1")).toBe(true);
    expect(app.shows("id:session-ses_2")).toBe(false);
  });

  test("/archive closes the conversation and returns to Sessions", async () => {
    const flags: Flags = {};
    const app = await openPhone(flags);
    await openConversation(app);
    await app.type("id:composer-input", "/archive");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/ses_1/archive")));
    await app.waitFor("id:session-search", { timeout: 8000 });
    expect(app.shows("id:phone-tab-sessions")).toBe(true);
    expect(app.shows("id:composer-input")).toBe(false);
  });

  test("the compact question card takes a focused free-text answer", async () => {
    const app = await openPhone({ questions: [scenario.question] });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:waiting-card");
    expect(app.enabled("id:composer-input")).toBe(false);
    await app.keyboard.press("z");
    await app.waitUntil(() => app.value("id:question-free-approach") === "z");
    await app.press("label:Which approach should we use? Conservative migration");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length === 1);
    const body = JSON.parse(app.requests().find((r) => r.method === "POST" && r.url.includes("/question/"))!.body as string);
    expect(body.answers[0].selected).toEqual(["safe"]);
    expect(body.answers[0].text).toBe("z");
  });
});
