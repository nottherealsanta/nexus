// Shared scenarios: `interface/scenarios/*.json`, which the TUI's `tests/scenarios.rs` also loads.
import { describe, expect, open, test } from "nsh/test";
import activityGroup from "../interface/scenarios/activity-group.json";
import collapsedPaste from "../interface/scenarios/collapsed-paste.json";
import mermaid from "../interface/scenarios/mermaid.json";

const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Scenario", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const emptyTurns = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const network = (messages: unknown[]) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: messages.map((m: any) => ({ ...m, session_id: "ses_1" })) },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: emptyTurns },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
});

describe("shared scenarios", () => {
  test("activity group", async () => {
    const scenario = activityGroup;
    const app = await open("./App.tsx", { network: network(scenario.messages), width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    let shown = app.texts().join("\n");
    for (const fact of scenario.expect) expect(shown).toContain(fact);
    // The stubbed stream's closed banner is not part of the timeline.
    const timeline = app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
    for (const fact of scenario.absent) expect(timeline.includes(fact)).toBe(false);
    await app.press("id:activity-group-tool-call_1");
    await app.waitFor("id:tool-row-call_1");
    shown = app.texts().join("\n");
    for (const fact of scenario.expect_open) expect(shown).toContain(fact);
  });

  test("collapsed paste", async () => {
    const scenario = collapsedPaste;
    const app = await open("./App.tsx", { network: network(scenario.messages), width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    const timeline = app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
    for (const fact of scenario.expect) expect(timeline).toContain(fact);
    for (const fact of scenario.absent) expect(timeline.includes(fact)).toBe(false);
  });

  test("mermaid", async () => {
    const scenario = mermaid;
    const app = await open("./App.tsx", { network: network(scenario.messages), width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    // The flowchart is the shared PNG; the pie is the shared text renderer.
    await app.waitFor("id:mermaid-diagram");
    await app.waitFor("id:mermaid-text");
    const timeline = app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
    for (const fact of scenario.expect) expect(timeline).toContain(fact);
    for (const fact of scenario.absent) expect(timeline.includes(fact)).toBe(false);
    await app.press("id:mermaid-copy >> nth=0");
  });
});
