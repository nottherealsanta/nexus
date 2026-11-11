// Shift+Tab cycles the eligible agents in the composer: a draft's next session and a
// viewed session (through Core's switch-cost check), skipping subagents where required.
import { describe, expect, open, test } from "nsh/test";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader",
  model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build",
  parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const core = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": {
    body: {
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
      sessions: [session], running: [], approvals: [], questions: [], selected: null,
    },
  },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/session/*/context": { body: { used: 0, window: null, exact: false } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/*/message*": { body: [] },
  "GET */api/session/*/snapshot": { body: null },
  "GET */api/session/*/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1": { body: session },
  "GET */api/session/ses_1/switch-cost*": { body: { cache_lost: false } },
  "POST */api/session/ses_1/agent": { body: { ...session, agent: "plan", revision: 2 } },
};
const agents = [
  { name: "build", source: "builtin", mode: "all" },
  { name: "plan", source: "builtin", mode: "primary" },
  { name: "scout", source: "builtin", mode: "subagent" },
  { name: "review", source: "project", mode: "primary" },
];

const app = async () => {
  const opened = await open("./ComposerFixture.tsx", {
    network: { ...core, "GET */api/agent*": { body: { agents, problems: [] } } },
    width: 1024, height: 768, platform: "web",
  });
  await opened.press("id:session-ses_1");
  await opened.waitFor("text:Refactor loader");
  await opened.waitUntil(() => opened.requests().some((r) => r.url.includes("/api/agent")));
  return opened;
};

describe("composer agent cycling", () => {
  test("shift+tab switches a viewed session through Core's switch-cost check", async () => {
    const opened = await app();
    await opened.type("id:composer-input", "hello");
    await opened.keyboard.press("shift+tab");
    await opened.waitFor("text:Agent changed from the next call");
    const switchCost = opened.requests().find((r) => r.method === "GET" && r.url.includes("/switch-cost"));
    expect(switchCost?.url).toContain("agent=plan");
    const switched = opened.requests().find((r) => r.method === "POST" && r.url.endsWith("/api/session/ses_1/agent"));
    expect(JSON.parse(String((switched as any).body))).toEqual({ agent: "plan" });
  });

  test("shift+tab cycles a draft, skips subagents and wraps", async () => {
    const opened = await app();
    await opened.keyboard.press("mod+n");
    await opened.waitUntil(() => opened.shows("text:Message to start a new session"));
    await opened.type("id:composer-input", "hello");
    await opened.keyboard.press("shift+tab");
    await opened.waitFor("text:Plan");
    await opened.keyboard.press("shift+tab");
    await opened.waitFor("text:Review");
    await opened.keyboard.press("shift+tab");
    await opened.waitFor("text:Build");
    expect(opened.texts().some((text) => text.startsWith("The new session will use"))).toBe(false);
  });
});
