// Shared scenarios: `interface/scenarios/*.json`, which the TUI's `tests/scenarios.rs` also loads.
import { describe, expect, open, test } from "nsh/test";
import activityGroup from "../interface/scenarios/activity-group.json";
import collapsedPaste from "../interface/scenarios/collapsed-paste.json";
import mermaid from "../interface/scenarios/mermaid.json";
import subagentDoneScenario from "../interface/scenarios/subagent-done.json";
import zmxDenial from "../interface/scenarios/zmx-denial.json";
import zmxInterrupt from "../interface/scenarios/zmx-interrupt.json";
import zmxNeedsAttention from "../interface/scenarios/zmx-needs-attention.json";
import zmxParallel from "../interface/scenarios/zmx-parallel-tools.json";
import zmxRecovered from "../interface/scenarios/zmx-recovered.json";
import { subagentDone } from "./src/store";

const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Scenario", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const emptyTurns = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';
const network = (messages: unknown[], sessions: any[] = [], primary: any = session) => {
  const all = [primary, ...sessions];
  const routes: Record<string, any> = {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: all, running: [], approvals: [], questions: [], selected: null } },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
  };
  for (const s of all) {
    routes[`GET */api/session/${s.id}/message*`] = { body: s.id === "ses_1" ? messages.map((m: any) => ({ ...m, session_id: "ses_1" })) : [] };
    routes[`GET */api/session/${s.id}/snapshot`] = { body: null };
    routes[`GET */api/session/${s.id}/turn*`] = { body: emptyTurns };
    routes[`GET */api/session/${s.id}/worktrees`] = { body: { worktrees: [] } };
    routes[`GET */api/session/${s.id}`] = { body: s };
  }
  routes["GET */api/session/*"] = { body: primary };
  return routes;
};

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

  test("subagent done notice", async () => {
    const scenario = subagentDoneScenario;
    const app = await open("./App.tsx", { network: network(scenario.messages, scenario.sessions), width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    const timeline = app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
    for (const fact of scenario.expect) expect(timeline).toContain(fact);
    for (const fact of scenario.absent) expect(timeline.includes(fact)).toBe(false);
    await app.press("id:subagent-done >> nth=0");
    await app.waitFor("id:subagent-done-modal");
    const modal = app.texts().join("\n");
    for (const fact of scenario.expect_modal) expect(modal).toContain(fact);
    expect(modal).toContain("Open session");
    await app.press("id:subagent-open-session");
    expect(subagentDone()).toBe(null);
    // The child session is now the view (it has no messages in this stub).
    await app.waitFor("text:No messages yet");
  });

  // A ZMX child (Claude Code or Antigravity): the shared scenario sets the session's `worker`, the card says what is not known.
  const zmx = (name: string, scenario: any) => test(name, async () => {
    const primary = { ...session, ...scenario.session, permission: scenario.session.permission ?? "ask", parent_id: null };
    const routes = network(scenario.messages, [], primary);
    routes["GET */api/session/ses_1/snapshot"] = { body: scenario.starting }; // the frozen starting context; a ZMX child replaces it with the CLI card
    const app = await open("./App.tsx", { network: routes, width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    const visible = () => app.texts().filter((t) => !t.includes("Disconnected") && !t.includes("event stream closed")).join("\n");
    for (const fact of scenario.expect) expect(visible()).toContain(fact);
    for (const fact of scenario.absent) expect(visible().includes(fact)).toBe(false);
    if (scenario.expect_open) {
      // Open the group, then every tool card in it (the TUI test uses high detail, which opens them all).
      const calls: string[] = scenario.messages.flatMap((m: any) => m.parts.filter((p: any) => p.type === "tool_call").map((p: any) => p.id));
      await app.press(`id:activity-group-tool-${calls[0]}`);
      for (const id of calls) { await app.waitFor(`id:tool-row-${id}`); await app.press(`id:tool-row-${id}`); }
      for (const fact of scenario.expect_open) expect(visible()).toContain(fact);
    }
  });
  zmx("zmx parallel tools and Claude's own subagent nested in its call", zmxParallel);
  zmx("zmx denied action shows what the CLI refused", zmxDenial);
  zmx("zmx needs attention names the manual step", zmxNeedsAttention);
  zmx("zmx interrupted turn keeps the unknown tool outcome", zmxInterrupt);
  zmx("zmx recovered turn says the CLI ran the tool", zmxRecovered);

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
