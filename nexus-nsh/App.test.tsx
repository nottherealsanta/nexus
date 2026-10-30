// Headless interaction tests (`nsh test`): the real runtime, a fake network standing in for the host's /api proxy.
import { describe, expect, open, test } from "nsh/test";
import { manifest } from "./src/gen/manifest";
import { agentSlot } from "./src/agent-color";
import { Model, VIEW_CACHE, scratchOf, upsertTurn } from "./src/core/reduce";
import { groupRequests, latestUsage, ledgerTotals, sumUsage } from "./src/core/turns";
import { capToolDetail, detailOf, fmtDuration } from "./src/ui/timeline";
import { diffItems, splitRows } from "./src/ui/diff";
import { defaultSkillSelection, draftInventoryRows, emptyGlobalToastState, globalEventToast, selectedSkillCounts, sessionInventoryRows, setDisabledTool, shouldInstallRequestedDirectly, toggleDisabledTool } from "./src/skills-mcp";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const snapshot = {
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [{ id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" }],
  running: [], approvals: [], questions: [], selected: null,
};
const core = (info: object = { name: "nexus", version: "0.10.0.dev0", pid: 1 }) => ({
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: info },
  "GET */api/snapshot*": { body: snapshot },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/session/*/context": { body: { used: 0, window: null, exact: false } },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/*": { body: snapshot.sessions[0] },
});
const mod = "mod";
const quietTurns = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
type Usage = { input: number; cached: number; output: number; reasoning: number } | null;
const usage = (input: number, output = 0, cached = 0, reasoning = 0): Usage => ({ input, cached, output, reasoning });
/** A turn summary as `GET …/turn` serves it: one request per `[model, usage]`, all closed. */
function turnSummary(id: string, startSeq: number, requests: [string | null, Usage][], extra: Record<string, unknown> = {}) {
  return {
    id, status: "completed", started: 1, finished: 1.9, start_seq: startSeq, steps: requests.length, stop: "end_turn",
    ended: { stop: "end_turn", steps: requests.length }, error: null, recovery: "none", tools: [],
    requests: requests.map(([model, u], i) => ({
      id: `${id}-r${i + 1}`, step: i + 1, status: "completed", model, effort: null, started: 1.1 + i * 0.1, finished: 1.15 + i * 0.1,
      stop: i + 1 === requests.length ? "end_turn" : "tool_calls", error: null, message_id: null, usage: u, reasoning_seconds: null,
      attempts: [{ n: 1, status: "completed", delay: null, started: 1.1 + i * 0.1, finished: 1.15 + i * 0.1, error: null, usage: u }],
    })),
    ...extra,
  };
}
const turnPage = (turns: object[], extra: Record<string, unknown> = {}) => ({ body: { ...quietTurns, turns, ...extra } });
const userMessage = { id: "m_user", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "hello" }], created: 1 };
const assistantMessage = { id: "m_assistant", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "done" }], created: 2 };

describe("skills and MCP", () => {
  test("direct install requires every requested skill to be found and not already installed", () => {
    const found = [{ name: "new", installed: false }, { name: "old", installed: true }];
    expect(shouldInstallRequestedDirectly(["new"], found)).toBe(true);
    expect(shouldInstallRequestedDirectly([], found)).toBe(false);
    expect(shouldInstallRequestedDirectly(["missing"], found)).toBe(false);
    expect(shouldInstallRequestedDirectly(["old"], found)).toBe(false);
    expect(defaultSkillSelection(found)).toEqual(["new"]);
    expect(selectedSkillCounts(found, ["new", "old"])).toEqual({ newCount: 1, replacedCount: 1, replacedNames: ["old"] });
  });

  test("disabled_tools switches preserve other entries and draft inventory filters disabled MCP servers", () => {
    const off = setDisabledTool(["bash", "mcp:other"], "skill:design", true);
    expect(off).toEqual(["bash", "mcp:other", "skill:design"]);
    expect(toggleDisabledTool(off, "skill", "design")).toEqual(["bash", "mcp:other"]);
    expect(draftInventoryRows([{ name: "design", state: "on" }], [
      { server: "live", enabled: true, state: "online", tools: 3 },
      { server: "invalid", enabled: true, state: "invalid" },
      { server: "disabled", enabled: false, state: "online" },
    ], ["mcp:live"]).map((row) => [row.name, row.on])).toEqual([["design", true], ["live", false]]);
  });

  test("running-session inventory marks locked entries as new-session-only and shows MCP failures", () => {
    const rows = sessionInventoryRows(
      [{ name: "ready", state: "on" }, { name: "late", state: "off", locked: true }],
      [{ server: "broken", state: "unavailable", error: "connection refused", tools: null, locked: true }],
    );
    expect(rows.map((row) => [row.name, row.on, row.hint, row.error])).toEqual([
      ["ready", true, undefined, undefined],
      ["late", false, "new session", undefined],
      ["broken", false, "new session", "connection refused"],
    ]);
  });

  test("global event toasts deduplicate changes and suppress this client's successful learn", () => {
    const state = emptyGlobalToastState();
    expect(globalEventToast("skills.changed", {}, state, 10_000)?.text).toBe("Skills changed. New sessions will use them.");
    expect(globalEventToast("skills.changed", {}, state, 12_999)).toBe(null);
    state.skillInstallPending = true;
    expect(globalEventToast("skills.changed", {}, state, 14_000)).toBe(null);
    state.skillInstallPending = false;
    state.muteSkillsUntil = 20_000;
    expect(globalEventToast("skills.changed", {}, state, 17_001)).toBe(null);
    expect(globalEventToast("mcp.changed", {}, state, 10_000)?.text).toBe("MCP servers changed. New sessions will use them.");
    expect(globalEventToast("mcp.changed", {}, state, 12_999)).toBe(null);
    expect(globalEventToast("mcp.failed", { server: "docs", error: "timeout" }, state, 13_000)).toEqual({ level: "warning", text: "MCP server docs failed: timeout" });
  });
});

describe("starting-context skills and MCP switches", () => {
  test("a running session shows tool counts and errors, patches an unlocked row and hints on locked rows", async () => {
    const session = {
      ...snapshot.sessions[0], skills: [
        { name: "design", description: "Design system", scope: "project", state: "on", locked: false },
        { name: "late", description: "Added later", scope: "global", state: "off", locked: true },
      ],
      mcp: [
        { server: "docs", tools: 5, state: "on", locked: false, error: null },
        { server: "broken", tools: null, state: "unavailable", locked: true, error: "connection refused" },
      ],
      disabled_tools: [],
    };
    const updated = { ...session, revision: 2, disabled_tools: ["skill:design"], skills: session.skills.map((skill) => skill.name === "design" ? { ...skill, state: "off" } : skill) };
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/snapshot*": { body: { ...snapshot, sessions: [session] } },
        "GET */api/session/ses_1": { body: session },
        "GET */api/session/ses_1/message*": { body: [] },
        "GET */api/session/ses_1/snapshot": { body: { system_prompt: "prompt", tools: [], instruction_file: {} } },
        "PATCH */api/session/ses_1": { body: updated },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:ctx-skill-row-design");
    expect(app.texts().join("\n").includes("5 tools")).toBe(true);
    expect(app.texts().join("\n").includes("connection refused")).toBe(true);
    await app.press("id:ctx-skill-row-late");
    await app.waitFor("text:Available in a new session.");
    await app.press("id:ctx-skill-row-design");
    await app.waitUntil(() => app.requests().some((r) => r.method === "PATCH" && r.url.endsWith("/api/session/ses_1")));
    const patch = app.requests().find((r) => r.method === "PATCH" && r.url.endsWith("/api/session/ses_1")) as any;
    expect(JSON.parse(patch.body)).toEqual({ disabled_tools: ["skill:design"] });
  });
});

function selectedSessionNetwork(extra: Record<string, unknown> = {}) {
  return {
    "GET */api/session/ses_1": { body: { ...snapshot.sessions[0], model: "codex/gpt-5", directory: "/work/demo" } },
    "GET */api/session/ses_1/message*": { body: [userMessage, assistantMessage] },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
    "GET */api/session/ses_1/worktrees": { body: { session_directory: "/work/demo", project_directory: "/work/demo", worktrees: [{ path: "/work/demo", branch: "trunk", main: true }] } },
    ...core(),
    ...extra,
  };
}

describe("agent colours", () => {
  test("pins and hashes match the TUI", () => {
    expect(["orchestrate", "build", "quick", "scout", "plan", "review", "tester"].map(agentSlot))
      .toEqual(["amber", "blue", "green", "teal", "violet", "rose", "sky"]);
    expect(agentSlot("BUILD")).toBe("blue");
  });
});

describe("shell", () => {
  test("header anchors, sidebar tabs in the header, fixed Settings entry", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    expect(app.shows("id:header")).toBe(true);
    expect(app.shows("id:toggle-left")).toBe(true);
    expect(app.shows("id:toggle-right")).toBe(true);
    expect(app.shows("id:open-settings")).toBe(true);
    // tabs: Sessions, Files, Projects, in manifest order; docked, they sit in the header's left slot
    const labels = (manifest.navigation.left_sidebar.tab as readonly { id: string }[]).map((t) => t.id);
    expect(labels).toEqual(["sessions", "files", "projects"]);
    expect(app.shows("id:tab-sessions") && app.shows("id:tab-files") && app.shows("id:tab-projects")).toBe(true);
    // the icons follow the left sidebar: closing it hides them, reopening shows them again
    await app.press("id:toggle-left");
    expect(app.shows("id:tab-files")).toBe(false);
    await app.press("id:toggle-left");
    expect(app.shows("id:tab-files")).toBe(true);
  });

  test("session tabs open, switch and close without deleting their sessions", async () => {
    const second = { ...snapshot.sessions[0], id: "ses_2", title: "Second task", created: 3, updated: 4 };
    const app = await open("./App.tsx", {
      // Specific stubs first: the first matching key answers, and core() ends with a broad `session/*`.
      network: {
        "GET */api/session/ses_2": { body: second },
        "GET */api/session/ses_1/message*": { body: [] },
        "GET */api/session/ses_1/snapshot": { body: null },
        "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
        "GET */api/session/ses_2/message*": { body: [] },
        "GET */api/session/ses_2/snapshot": { body: null },
        "GET */api/session/ses_2/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
        "GET */api/session/*/worktrees": { body: { worktrees: [] } },
        ...core(),
        "GET */api/snapshot*": { body: { ...snapshot, sessions: [snapshot.sessions[0], second] } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:session-ses_2");
    await app.waitFor("id:session-tab-ses_1");
    await app.waitFor("id:session-tab-ses_2");
    await app.press("id:session-tab-ses_2");
    await app.press("id:tab-close-ses_2");
    expect(app.shows("id:session-tab-ses_2")).toBe(false);
    expect(app.shows("id:session-ses_2")).toBe(true);
    await app.press("id:tab-close-ses_1");
    await app.waitFor("id:new-session-tab");
    expect(app.shows("id:session-ses_1")).toBe(true);
  });

  test("pre-session picker choices and slash commands become creation fields", async () => {
    const created = { ...snapshot.sessions[0], id: "ses_new", title: "Drafted session", agent: "review", model: "codex/gpt-5", effort: "high" };
    const app = await open("./App.tsx", {
      network: {
        "POST */api/session": { body: created },
        "GET */api/session/ses_new": { body: created },
        "GET */api/session/ses_new/message*": { body: [] },
        "GET */api/session/ses_new/snapshot": { body: null },
        "GET */api/session/ses_new/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
        "GET */api/session/ses_new/worktrees": { body: { worktrees: [] } },
        "POST */api/session/ses_new/prompt": { status: 204, body: null },
        ...core(),
        "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", name: "GPT-5", context: 200_000, efforts: ["low", "medium", "high"] }] } },
        "GET */api/model/last*": { body: { model: "codex/gpt-5" } },
        "GET */api/agent*": { body: { agents: [{ name: "build", source: "builtin", mode: "all" }, { name: "plan", source: "builtin", mode: "subagent" }, { name: "review", source: "project", mode: "primary" }] } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.waitFor("id:composer-agent");
    const slash = async (value: string) => {
      await app.type("id:composer-input", value);
      await app.press("id:send");
    };
    await slash("/agent");
    await app.waitFor("id:picker");
    expect(app.shows("text:plan")).toBe(false);
    await app.press("id:pick-1");
    await app.waitFor("text:The new session will use agent review");
    await slash("/model");
    await app.waitFor("id:picker");
    await app.press("id:pick-0");
    await slash("/effort");
    await app.waitFor("id:picker");
    await app.press("id:pick-2");
    await app.type("id:composer-input", "hello");
    await app.press("id:send");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/api/session")));
    const request = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/api/session")) as any;
    expect(request.body).toEqual({ project: "prj_1", agent: "review", model: "codex/gpt-5", effort: "high" });
  });

  test("settings sections follow the manifest order", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.keyboard.press(`${mod}+,`);
    await app.waitFor("id:settings");
    const expected = (manifest.settings.section as readonly { id: string; label: string }[]).filter((s) => s.id !== "models").map((s) => s.label);
    const shown = app.texts().filter((t) => expected.includes(t));
    expect(shown.slice(0, expected.length)).toEqual(expected);
    expect(expected[0]).toBe("Appearance");
    expect(expected[expected.length - 1]).toBe("About");
  });

  test("palette opens with the key, lists disabled commands with reasons, Esc walks the chain", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    await app.keyboard.press(`${mod}+p`);
    await app.waitFor("id:palette");
    expect(app.shows("id:palette")).toBe(true);
    // blocked_core command: present, disabled, with its reason once its row is the selected one (D13)
    await app.type("id:palette-input", "Attach file");
    await app.waitFor("text:Attach file or image");
    expect(app.texts().some((t) => t.includes("Needs a Core increment"))).toBe(true);
    await app.keyboard.press("escape");           // rule 1: overlay closes
    expect(app.shows("id:palette")).toBe(false);
    await app.keyboard.press(`${mod}+,`);         // settings opens
    await app.waitFor("id:settings");
    await app.keyboard.press("escape");           // rule 9: settings returns to the previous view
    expect(app.shows("id:settings")).toBe(false);
  });

  test("model and agent pickers are fed by Core, not hard-coded lists", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    await app.keyboard.press(`${mod}+shift+m`);
    await app.waitFor("id:picker");
    expect(app.texts().some((t) => t.includes("Nothing to choose from"))).toBe(true);
    await app.keyboard.press("escape");           // rule 1: overlay closes
    expect(app.shows("id:picker")).toBe(false);
    await app.keyboard.press(`${mod}+,`);
    await app.waitFor("id:settings");
  });

  test("narrow layout: sidebars are drawers and the toggles stay in the header", async () => {
    const app = await open("./App.tsx", { network: core(), width: 390, height: 844, platform: "web" });
    await app.waitFor("id:toggle-left");
    expect(app.shows("id:left-sidebar")).toBe(false);
    await app.press("id:toggle-left");
    await app.waitFor("id:left-sidebar");
    expect(app.shows("id:toggle-left")).toBe(true);   // anchor still visible
    await app.keyboard.press("escape");               // rule 4: drawer closes
    expect(app.shows("id:left-sidebar")).toBe(false);
  });
});

describe("connection states", () => {
  test("live: no connection banner, composer enabled", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    // Core reports api_version now: no compatibility banner.
    expect(app.shows("text:Core does not report an API version yet")).toBe(false);
    expect(app.shows("text:Disconnected from Core")).toBe(false);
    expect(app.shows("id:composer-input")).toBe(true);
  });

  test("offline: banner names the problem and the view is read-only", async () => {
    const app = await open("./App.tsx", { network: { ...core(), "GET */api/info": { status: 502, body: { error: "Nexus Core is not running (no daemon.json)", code: "no_daemon" } } }, width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:Nexus Core is not running");
    expect(app.texts().some((t) => t.includes("nexus daemon start"))).toBe(true);
    expect(app.shows("id:composer-input")).toBe(true);
    // the view is read-only until Core returns: the banner offers the recovery action
    expect(app.shows("text:Retry now")).toBe(true);
  });

  test("unreachable then reconnecting banner appears after a live stream drops", async () => {
    let calls = 0;
    const app = await open("./App.tsx", {
      network: { ...core(), "GET */api/info": () => (calls++ === 0 ? { body: { name: "nexus", version: "x", pid: 1, api_version: "1.0" } } : { error: "offline" }) },
      width: 1024, height: 768, platform: "web",
    });
    // first connect works and the fake stream ends at once, which is a drop: the banner must say reconnecting
    await app.waitFor("text:/Disconnected from Core/");
    expect(app.texts().some((t) => t.includes("reconnecting"))).toBe(true);
  });

  test("incompatible API major blocks with a non-dismissable dialog naming the side to update", async () => {
    const app = await open("./App.tsx", { network: core({ name: "nexus", version: "9", pid: 1, api_version: "2.0" }), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:blocking-dialog");
    expect(app.texts().some((t) => t.includes("Update NSH"))).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:blocking-dialog")).toBe(true);
  });
});

describe("history", () => {
  const ev = (id: number, type = "usage", data: any = {}) => ({ id, type, session_id: "ses_1", cursor: `log_t:${id}`, seq: id, time: id, data }) as any;
  const msg = (id: string, seq: number) => ({ id, session_id: "ses_1", seq, role: "assistant", parts: [], time: 0 }) as any;
  const live = (m: Model) => { m.generation = "log_t"; return m; };

  test("history keeps messages and summaries the stream applied after the fetch", async () => {
    const m = live(new Model());
    m.select("ses_1");
    m.selected!.messages = [msg("m1", 1), msg("m3", 3)]; // m3 arrived live
    const closedLive = turnSummary("trn_2", 2, [["codex/gpt-5", usage(5)]], { started: 2 });
    m.selected!.turns = [closedLive as any];
    const fetched = { ...turnSummary("trn_2", 2, [["codex/gpt-5", null]], { started: 2 }), status: "running" };
    m.applyHistory({ sessionId: "ses_1", messages: [msg("m1", 1), msg("m2", 2)], turns: [turnSummary("trn_1", 0, []) as any, fetched as any], floor: null, usage: null, starting: null, turnsGap: null });
    expect(m.selected!.messages.map((x) => x.id)).toEqual(["m1", "m2", "m3"]);
    expect(m.selected!.turns.map((t) => [t.id, t.status])).toEqual([["trn_1", "completed"], ["trn_2", "completed"]]);
  });

  test("a view left behind is cached, kept current by events and restored on select", () => {
    const m = live(new Model());
    m.select("ses_1");
    m.applyHistory({ sessionId: "ses_1", messages: [msg("m1", 1)], turns: [], floor: null, usage: null, starting: null, turnsGap: null });
    m.select("ses_2");
    expect(m.cached.map((v) => v.sessionId)).toEqual(["ses_1"]);
    expect(m.selected!.messages).toEqual([]);
    // Events of the cached session still reach its view.
    m.applyEvent(ev(1, "message.added", msg("m2", 2)));
    m.applyEvent(ev(2, "step_start", { step: 1 }));
    m.select("ses_1");
    expect(m.selected!.loaded).toBe(true);
    expect(m.selected!.messages.map((x) => x.id)).toEqual(["m1", "m2"]);
    expect(m.selected!.scratch.map((e) => e.type)).toEqual(["message.added", "step_start"]);
    expect(m.cached.map((v) => v.sessionId)).toEqual(["ses_2"]);
    // Re-selecting the shown session keeps its view.
    m.select("ses_1");
    expect(m.selected!.messages.length).toBe(2);
  });

  test("the view cache is bounded and drops deleted sessions", () => {
    const m = live(new Model());
    for (let n = 0; n < VIEW_CACHE + 3; n++) m.select(`ses_${n}`);
    expect(m.cached.length).toBe(VIEW_CACHE);
    m.select("ses_1");
    m.select(null);
    expect(m.cached[0].sessionId).toBe("ses_1");
    m.applyEvent({ ...ev(1, "session.deleted", { id: "ses_1" }) });
    expect(m.cached.some((v) => v.sessionId === "ses_1")).toBe(false);
  });

  test("history replaces a held running turn that the fetch read closed", () => {
    const m = live(new Model());
    m.select("ses_1");
    m.selected!.turns = [{ ...turnSummary("trn_1", 0, []), status: "running" } as any];
    m.applyHistory({ sessionId: "ses_1", messages: [], turns: [turnSummary("trn_1", 0, []) as any], floor: null, usage: null, starting: null, turnsGap: null });
    expect(m.selected!.turns.map((t) => t.status)).toEqual(["completed"]);
  });

  test("scratch keeps live events without deltas or wire bodies; turn.summary replaces it", async () => {
    expect(scratchOf(ev(1, "text_delta", { text: "x" }))).toBe(null);
    expect(scratchOf(ev(1, "wire_event", {}))).toBe(null);
    expect(scratchOf(ev(1, "wire_request", { body: { model: "gpt-5", input: ["huge"] } }))!.data).toEqual({ body: { model: "gpt-5" } });
    const m = live(new Model());
    m.select("ses_1");
    for (const e of [ev(1, "step_start", { step: 1 }), ev(2, "text_delta", { text: "hi" }), ev(3, "usage", { input_tokens: 10, output_tokens: 4 })]) m.applyEvent(e);
    expect(m.selected!.scratch.map((e) => e.type)).toEqual(["step_start", "usage"]);
    expect(m.streaming.get("ses_1")?.text).toBe("hi");
    expect(sumUsage(groupRequests([], m.selected!.scratch)).output).toBe(4);
    m.applyEvent(ev(4, "turn.summary", turnSummary("trn_1", 0, [["codex/gpt-5", usage(10, 4)]])));
    expect(m.selected!.scratch).toEqual([]);
    expect(m.selected!.turns.map((t) => t.id)).toEqual(["trn_1"]);
    // Replacing, not appending: a second summary for the same turn wins.
    m.applyEvent(ev(5, "turn.summary", turnSummary("trn_1", 0, [["codex/gpt-5", usage(12, 4)]])));
    expect(m.selected!.turns.length).toBe(1);
    expect(latestUsage(m.selected!.turns, m.selected!.scratch)).toEqual({ input: 12, model: "codex/gpt-5" });
  });

  test("global live-only events advance the cursor without entering session scratch", () => {
    const m = live(new Model());
    m.select("ses_1");
    const global = { ...ev(1, "skills.changed", {}), session_id: null };
    expect(m.applyEvent(global)).toBe("applied");
    expect(m.lastId).toBe(1);
    expect(m.selected!.scratch).toEqual([]);
    expect(m.applyEvent(global)).toBe("duplicate");
  });

  test("a running turn's loaded summary joins its live requests without counting twice", async () => {
    const running = { ...turnSummary("trn_1", 0, [["codex/gpt-5", usage(100, 10)], ["codex/gpt-5", null]]), status: "running" } as any;
    running.requests[1].status = "running";
    const scratch = [
      ev(1, "step_start", { step: 1 }), ev(2, "usage", { input_tokens: 100, output_tokens: 10 }),
      ev(3, "step_start", { step: 2 }), ev(4, "usage", { input_tokens: 200, output_tokens: 20 }), ev(5, "step_start", { step: 3 }),
    ];
    // Step 1 is in both and counted once; step 2 gets its live usage; step 3 is new and still running.
    const reqs = groupRequests([running], scratch);
    expect(reqs.map((r) => r.step)).toEqual([1, 2, 3]);
    expect(sumUsage(reqs)).toEqual({ input: 300, cached: 0, output: 30, reasoning: 0, known: 2, unknown: 0 });
  });

  test("summaries are ordered by start; the ledger adds only live usage it has not counted", async () => {
    const order = upsertTurn(upsertTurn([], turnSummary("b", 1, [], { started: 5 }) as any), turnSummary("a", 0, [], { started: 1 }) as any);
    expect(order.map((t) => t.id)).toEqual(["a", "b"]);
    const ledger = { since: 1, models: { "codex/gpt-5": { requests: 1, input: 100, cached: 0, output: 10, reasoning: 0 } } };
    const rows = ledgerTotals(ledger, [ev(3, "usage", { input_tokens: 50 }), ev(7, "usage", { input_tokens: 70 })], 5, "codex/gpt-5");
    expect(rows.get("codex/gpt-5")).toEqual({ requests: 2, input: 170, cached: 0, output: 10, reasoning: 0 });
  });

  test("selecting loads history without a global snapshot; a daemon restart reloads it", async () => {
    let pid = 1;
    const app = await open("./App.tsx", {
      clock: {},
      network: {
        "GET */api/session/ses_1/message*": { body: [] },
        "GET */api/session/ses_1/snapshot": { body: {} },
        "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
        ...core(),
        "GET */api/info": () => ({ body: { name: "nexus", version: "x", pid, api_version: { major: 1, minor: 0 } } }),
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.wait(100);
    const loads = () => app.requests().filter((r) => r.url.includes("/api/session/ses_1/message")).length;
    expect(loads()).toBe(1);
    expect(app.requests().some((r) => r.url.includes("/api/snapshot") && r.url.includes("session=ses_1"))).toBe(false);
    pid = 2; // the stream keeps dropping; the next handshake sees a new daemon and resyncs
    for (let i = 0; i < 12; i++) await app.clock.fastForward(5000);
    expect(app.requests().some((r) => r.url.includes("/api/snapshot") && r.url.includes("session=ses_1"))).toBe(true);
    expect(loads()).toBe(2);
  });

  test("a daemon that will not start is started once per outage", async () => {
    const app = await open("./App.tsx", {
      clock: {},
      network: {
        ...core(),
        "GET */__nexus/host": { body: { host: "test", canStart: true } },
        "GET */api/info": { status: 502, body: { error: "Nexus Core is not running (no daemon.json)", code: "no_daemon" } },
        "POST */__nexus/start": { status: 500, body: { exit: 1 } },
      },
      width: 1024, height: 768, platform: "web",
    });
    for (let i = 0; i < 30; i++) await app.clock.fastForward(5000); // several backoff cycles
    expect(app.requests().filter((r) => r.url.includes("/api/info")).length > 3).toBe(true);
    expect(app.requests().filter((r) => r.method === "POST" && r.url.includes("/__nexus/start")).length).toBe(1);
  });
});

describe("context, composer and footer parity", () => {
  const starting = {
    origin: "created",
    system_prompt: `# Agent prompt\nYou are coding in Nexus.\n${"x".repeat(4000)}`,
    tools: ["read", "bash", "edit"],
    tool_definitions: { read: { name: "read" } },
    instruction_file: { path: "/work/demo/AGENTS.md", status: "loaded", bytes: 800, content: "Frozen instructions" },
    agent: { name: "build", source: "builtin", description: "Does the work." },
    child_agents: [{ name: "scout" }, { name: "plan" }],
    workspace: { active: true },
  };

  test("starting context shows the TUI items, estimates and catalog window share", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...selectedSessionNetwork({
          "GET */api/session/ses_1/message*": { body: [] },
          "GET */api/session/ses_1/snapshot": { body: starting },
          "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-5", context: 100000 }] } },
        }),
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:context-section");
    const shown = app.texts().join("\n");
    expect(shown.includes("Child agents")).toBe(false);
    await app.press("id:context-fold");
    expect(app.shows("id:ctx-system-open")).toBe(false);
    await app.press("id:context-overview");
    await app.waitFor("id:context-detail-text");
    expect(app.shows("id:context-detail")).toBe(true);
    expect(app.texts().join("\n").includes("You are coding in Nexus.")).toBe(true);
    await app.keyboard.press("escape");
    await app.press("id:context-fold");
    await app.press("id:ctx-system-open");
    await app.waitFor("id:context-detail-text");
    expect(app.shows("id:context-detail")).toBe(true);
    expect(app.texts().join("\n").includes("You are coding in Nexus.")).toBe(true);
    await app.keyboard.press("escape");
    await app.press("id:ctx-instructions-open");
    await app.waitFor("id:context-detail-text");
    expect(app.shows("id:context-detail")).toBe(true);
    expect(app.texts().join("\n").includes("Frozen instructions")).toBe(true);

  });

  test("turn footer prices cached and uncached input using the catalog", async () => {
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/turn*": turnPage([turnSummary("trn_1", 0, [["codex/gpt-5", usage(100000, 10000, 50000)]])]),
        "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-5", context: 400000, input_cost: 1, output_cost: 10, cache_read_cost: 0.2 }] } },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:turn-footer");
    const footer = app.texts().join("\n");
    expect(footer.includes("100K in 50.0K cached 10.0K out")).toBe(true);
    expect(footer.includes("$0.16")).toBe(true);
  });

  test("composer control row shows the context meter; the project strip shows the palette hint", async () => {
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/turn*": turnPage([turnSummary("trn_1", 0, [["codex/gpt-5", usage(1000)], ["codex/gpt-5", usage(11100)]])]),
        "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-6.1 Terra", context: 1_000_000, tiers: [270_000] }] } },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-usage");
    const shown = app.texts().join("\n");
    expect(shown.includes("GPT-6.1 Terra") && shown.includes("codex")).toBe(true);
    expect(shown.includes("11k · 1.1%")).toBe(true);
    expect(app.shows("id:composer-meter")).toBe(true);
    expect(app.shows("id:location-strip")).toBe(true);
    expect(shown.includes("ctrl+p commands")).toBe(false);
  });

  test("location strip uses the longest matching linked worktree and shows disabled action reasons", async () => {
    // The session moved: the snapshot still has the old directory, the session view the new one
    // (as after `session.updated`), so the worktrees are read again for it. No event is loaded.
    const session = { ...snapshot.sessions[0], directory: "/work/demo/ws/nested", model: "codex/gpt-5", revision: 2, directory_changed: true };
    let worktreeReads = 0;
    const app = await open("./App.tsx", {
      network: {
        ...selectedSessionNetwork({
          "GET */api/snapshot*": { body: { ...snapshot, sessions: [{ ...session, directory: "/work/demo", revision: 1, directory_changed: false }] } },
          "GET */api/session/ses_1": { body: session },
          "GET */api/session/ses_1/worktrees": () => {
            worktreeReads++;
            return { body: {
              session_directory: session.directory, project_directory: "/work/demo",
              worktrees: [
                { path: "/work/demo", branch: "trunk", main: true },
                { path: "/work/demo/ws", branch: "feature", main: false },
                { path: "/work/demo/ws/nested", branch: "nested", main: false },
              ],
            } };
          },
        }),
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("text:⌥ nested");
    await app.waitFor("id:location-worktree");
    const shown = app.texts().join("\n");
    expect(shown.includes("demo")).toBe(true);
    expect(shown.includes("nested")).toBe(true);
    expect(shown.includes("/work/demo/ws/nested")).toBe(true);
    expect(shown.includes("worktree")).toBe(false);
    expect(worktreeReads >= 2).toBe(true);
    expect(app.shows("id:location-directory")).toBe(true);
    expect(app.shows("id:location-project")).toBe(false);
  });

  test("new-session location loads project worktrees and omits the linked marker for main", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        "GET */api/project/prj_1/worktrees": { body: { project_directory: "/work/demo", worktrees: [{ path: "/work/demo", branch: "trunk", main: true }] } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:New session");
    await app.waitFor("text:⌥ trunk");
    expect(app.requests().some((r) => r.url.includes("/api/project/prj_1/worktrees"))).toBe(true);
    expect(app.shows("id:location-worktree")).toBe(false);
  });
});

describe("markdown fences and diffs (I4)", () => {
  test("splits runs of removals and additions into paired rows with their own numbers", () => {
    const lines = detailOf("edit", JSON.stringify({ patch: "@@ -1,3 +1,3 @@\n a\n-b\n+c\n d" }), undefined, false);
    const rows = splitRows(diffItems(lines));
    expect(rows.map((r) => r.kind)).toEqual(["hunk", "ctx", "pair", "ctx"]);
    const pair = rows[2] as { left?: { o?: number; text: string }; right?: { n?: number; text: string } };
    expect(pair.left?.o).toBe(2);
    expect(pair.right?.n).toBe(2);
    expect(pair.left?.text).toBe("b");
    expect(pair.right?.text).toBe("c");
    const uneven = splitRows(diffItems(detailOf("edit", JSON.stringify({ patch: "@@ -1 +1 @@\n-x\n-y\n+z" }), undefined, false)));
    const last = uneven[uneven.length - 1] as { left?: unknown; right?: unknown };
    expect(last.left !== undefined && last.right === undefined).toBe(true);
  });

  test("a fenced block shows its language and a Copy button; an edit shows the diff view", async () => {
    const messages = [
      userMessage,
      { id: "m_fence", session_id: "ses_1", seq: 2, role: "assistant", created: 2, parts: [{ type: "text", text: "Use:\n\n```python\nprint(1)\n```" }] },
      { id: "m_edit", session_id: "ses_1", seq: 3, role: "assistant", created: 3, parts: [{ type: "tool_call", id: "call_edit", name: "edit", arguments: JSON.stringify({ patch: "@@ -1 +1 @@\n-old line\n+new line" }) }] },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: messages } }),
      width: 1280, height: 800, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:code-fence");
    const fence = app.texts().join("\n");
    expect(fence.includes("python") && fence.includes("print(1)") && fence.includes("Copy")).toBe(true);
    await app.waitFor("id:tool-row-call_edit");
    await app.press("id:tool-row-call_edit");
    await app.waitFor("id:diff");
    expect(app.texts().join("\n").includes("new line")).toBe(true);
  });
});

describe("timeline rhythm", () => {
  test("durations read 0.4s, 4.2s, 1m 05s and 1h 02m, never <1 s", () => {
    expect([0.4, 4.2, 12, 65, 3720].map(fmtDuration)).toEqual(["0.4s", "4.2s", "12s", "1m 05s", "1h 02m"]);
    expect(fmtDuration(0.04).includes("<1")).toBe(false);
  });
});

describe("tool output detail cap", () => {
  test("caps plain, raw, and diff detail at the same head and tail", () => {
    const output = Array.from({ length: 500 }, (_, i) => `plain line ${i + 1}`).join("\n");
    const patch = Array.from({ length: 500 }, (_, i) => `+diff line ${i + 1}`).join("\n");
    const args = JSON.stringify({ command: "printf output" });
    const details = [
      detailOf("bash", args, output, false),
      detailOf("bash", args, output, true),
      detailOf("edit", JSON.stringify({ patch }), undefined, false),
    ];
    for (const lines of details) {
      const capped = capToolDetail(lines, false);
      expect(capped).toHaveLength(201);
      expect(capped[100].text).toContain(`${lines.length - 200} lines hidden · show all (o)`);
      expect(capToolDetail(lines, true)).toEqual(lines);
    }
    expect(capToolDetail(details[1], false)[100].text).toContain("301 lines hidden");
    const high = capToolDetail(details[0].slice(0, 120), false, 30, 20);
    expect(high).toHaveLength(51);
    expect(high[30].text).toContain("70 lines hidden");
    expect(high[29].text).toBe("plain line 30");
    expect(high[31].text).toBe("plain line 101");
  });

  test("keeps the first and last 100 lines for plain, raw, and diff detail, with a reversible toggle", async () => {
    const plain = Array.from({ length: 500 }, (_, i) => `plain line ${i + 1}`).join("\n");
    const patch = Array.from({ length: 500 }, (_, i) => `+diff line ${i + 1}`).join("\n");
    const messages = [
      { ...userMessage, parts: [{ type: "text", text: "Run the tools" }] },
      {
        id: "m_tools", session_id: "ses_1", seq: 2, role: "assistant", created: 2,
        parts: [
          { type: "tool_call", id: "call_bash", name: "bash", arguments: JSON.stringify({ command: "printf output" }) },
          { type: "tool_call", id: "call_edit", name: "edit", arguments: JSON.stringify({ patch }) },
        ],
      },
      { id: "m_result", session_id: "ses_1", seq: 3, role: "tool", created: 3, parts: [{ type: "tool_result", call_id: "call_bash", output: plain, is_error: false }] },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: messages } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:tool-row-call_bash");
    await app.press("id:tool-row-call_bash");
    await app.waitFor("text:plain line 1");
    let text = app.texts().join("\n");
    expect(text.includes("plain line 1") && text.includes("plain line 100")).toBe(true);
    expect(text.includes("plain line 401") && text.includes("plain line 500")).toBe(true);
    expect(text.includes("↑ 300 earlier lines") && text.includes("Show all")).toBe(true);
    expect(text.includes("plain line 250")).toBe(false);

    await app.keyboard.press("o");
    expect(app.texts().join("\n").includes("plain line 250")).toBe(true);
    expect(app.texts().includes("show less o")).toBe(true);
    await app.keyboard.press("o");
    expect(app.texts().join("\n").includes("plain line 250")).toBe(false);
    await app.keyboard.press("r");
    text = app.texts().join("\n");
    expect(text.includes("[raw]")).toBe(true);

    const diffMessages = [
      { ...userMessage, parts: [{ type: "text", text: "Run the edit" }] },
      { id: "m_edit", session_id: "ses_1", seq: 2, role: "assistant", created: 2, parts: [{ type: "tool_call", id: "call_edit", name: "edit", arguments: JSON.stringify({ patch }) }] },
      { id: "m_edit_result", session_id: "ses_1", seq: 3, role: "tool", created: 3, parts: [{ type: "tool_result", call_id: "call_edit", output: "edited", is_error: false }] },
    ];
    const diffApp = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: diffMessages } }),
      width: 1024, height: 768, platform: "web",
    });
    await diffApp.waitFor("text:Refactor loader");
    await diffApp.press("id:session-ses_1");
    await diffApp.waitFor("id:tool-row-call_edit");
    await diffApp.press("id:tool-row-call_edit");
    text = diffApp.texts().join("\n");
    expect(text.includes("diff line 1") && text.includes("diff line 100")).toBe(true);
    expect(text.includes("diff line 401") && text.includes("diff line 500")).toBe(true);
    expect(text.includes("↑ 300 earlier lines") && text.includes("Show all")).toBe(true);
  });

  test("detail cycles groups, tools and high-detail clipping, then resets hand-open state", async () => {
    const highOutput = Array.from({ length: 120 }, (_, i) => `high line ${i + 1}`).join("\n");
    const messages = [
      { ...userMessage, parts: [{ type: "text", text: "Run the tools" }] },
      {
        id: "m_tools", session_id: "ses_1", seq: 2, role: "assistant", created: 2,
        parts: [
          { type: "tool_call", id: "call_bash", name: "bash", arguments: JSON.stringify({ command: "printf output" }) },
          { type: "tool_call", id: "call_echo", name: "bash", arguments: JSON.stringify({ command: "echo second" }) },
        ],
      },
      { id: "m_result", session_id: "ses_1", seq: 3, role: "tool", created: 3, parts: [{ type: "tool_result", call_id: "call_bash", output: highOutput, is_error: false }, { type: "tool_result", call_id: "call_echo", output: "second output", is_error: false }] },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: messages } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:tool-row-call_bash");
    expect(app.texts().join("\n").includes("Ran 2 commands")).toBe(true);
    await app.keyboard.press("alt+d");
    await app.waitFor("text:Detail: high");
    await app.waitFor("text:high line 1");
    let text = app.texts().join("\n");
    expect(text.includes("high line 30") && text.includes("high line 101") && text.includes("high line 120")).toBe(true);
    expect(text.includes("↑ 70 earlier lines") && text.includes("Show all")).toBe(true);
    expect(text.includes("high line 31") || text.includes("high line 100")).toBe(false);
    await app.press("id:tool-show-all-call_bash");
    await app.waitFor("text:high line 100");
    await app.keyboard.press("alt+d");
    await app.waitFor("text:Detail: low");
    expect(app.shows("id:tool-row-call_bash")).toBe(false);
    expect(app.texts().join("\n").includes("high line 100")).toBe(false);
  });
});

describe("session info totals", () => {
  test("matches call, tool, token, context, price, and child totals", async () => {
    const session = {
      ...snapshot.sessions[0], model: "codex/gpt-5",
      tools: ["bash", "read", "edit", "write", "code", "ask", "bash_output", "bash_kill", "expose_workspace_tools", "web_search"],
      disabled_tools: ["write"],
    };
    const children = [
      { ...session, id: "ses_running", title: "Running child", parent_id: "ses_1", parent_call_id: "child-running", status: "busy" },
      { ...session, id: "ses_failed", title: "Failed child", parent_id: "ses_1", parent_call_id: "child-failed", status: "failed" },
    ];
    const toolCalls = [
      ...Array.from({ length: 12 }, (_, i) => ({ type: "tool_call", id: `bash-${i}`, name: "bash", arguments: "{}" })),
      ...Array.from({ length: 4 }, (_, i) => ({ type: "tool_call", id: `edit-${i}`, name: "edit", arguments: "{}" })),
      { type: "tool_call", id: "read-1", name: "read", arguments: "{}" },
      { type: "tool_call", id: "code-1", name: "code", arguments: "{}" },
    ];
    const messages = [
      { ...userMessage, parts: [{ type: "text", text: "Summarize this session" }] },
      { id: "m_tools", session_id: "ses_1", seq: 2, role: "assistant", parts: toolCalls, created: 2 },
    ];
    // The ledger is per model (a backfilled row has the bare id); the newest request with usage gives the context bar.
    const ledger = { since: 1.1, models: {
      "unpriced/model": { requests: 1, input: 1000, cached: 0, output: 0, reasoning: 0 },
      "gpt-5": { requests: 1, input: 10_000, cached: 0, output: 1000, reasoning: 200 },
      "codex/gpt-5": { requests: 1, input: 50_000, cached: 20_000, output: 2000, reasoning: 300 },
    } };
    const turns = [turnSummary("trn_1", 0, [["unpriced/model", usage(1000)], ["gpt-5", usage(10_000, 1000, 0, 200)], ["codex/gpt-5", usage(50_000, 2000, 20_000, 300)]])];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/snapshot*": { body: { ...snapshot, sessions: [session, ...children], running: [{ id: "turn_child", session_id: "ses_running" }] } },
        "GET */api/session/ses_1": { body: session },
        "GET */api/session/ses_1/message*": { body: messages },
        "GET */api/session/ses_1/turn*": turnPage(turns, { usage: ledger }),
        "GET */api/model": { body: { source: "fixture catalog", models: [{ ref: "codex/gpt-5", context: 200_000, input_cost: 1, output_cost: 10, cache_read_cost: 0.2 }] } },
        "GET */api/model/last*": { body: { model: "codex/gpt-5" } },
      }),
      width: 1440, height: 900, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:right-sidebar");
    await app.waitFor("text:61.0K");
    const shown = app.texts().join("\n");
    for (const expected of [
      "Turns", "1 · 3 calls", "Used", "bash 12 · edit 4 · +2", "Tools", "9 on · 1 off",
      "Input", "61.0K", "Cached", "20.0K", "Output", "3.0K", "Reasoning", "500",
      "CONTEXT", "25%", "Cost", "$0.07+", "Children", "2 (1 running, 1 failed)",
      "Kernel state is not reported by Core yet.",
    ]) expect(shown.includes(expected)).toBe(true);
  });

  test("says when Core has not reported usage", async () => {
    const app = await open("./App.tsx", { network: selectedSessionNetwork(), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("text:not reported");
    const shown = app.texts().join("\n");
    expect(shown.includes("Tokens") && shown.includes("not reported")).toBe(true);
    expect(shown.includes("Input")).toBe(false);
  });
});

describe("turn summaries", () => {
  const user = (id: string, seq: number, text: string) => ({ id, session_id: "ses_1", seq, role: "user", parts: [{ type: "text", text }], created: seq });
  const reply = (id: string, seq: number) => ({ id, session_id: "ses_1", seq, role: "assistant", parts: [{ type: "text", text: `reply ${seq}` }], created: seq + 0.5 });

  test("unknown usage reads unknown, a floor row marks turns without details, and no event history is read", async () => {
    const turns = [turnSummary("trn_2", 2, [["codex/gpt-5", null]], { started: 3, finished: 4 })];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/message*": { body: [user("u1", 1, "first"), reply("a1", 2), user("u2", 3, "second"), reply("a2", 4)] },
        "GET */api/session/ses_1/turn*": turnPage(turns, { floor: { seq: 3 } }),
      }),
      width: 1440, height: 900, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-older");
    await app.waitFor("text:reply 4");
    const shown = app.texts().join("\n");
    expect(shown.includes("Turns above have no details")).toBe(true);
    expect(shown.includes("tokens unknown")).toBe(true);
    expect(shown.includes("0 i")).toBe(false);
    expect(shown.includes("1 request reported no usage (unknown, not zero)")).toBe(true);
    expect(shown.includes("/continue")).toBe(false); // a turn without a summary is not called interrupted
    expect(app.requests().some((r) => r.url.includes("/api/session/ses_1/turn"))).toBe(true);
    expect(app.requests().some((r) => r.url.includes("/api/session/ses_1/event"))).toBe(false);
  });

  test("retries and the turn error come from the summary's attempts", async () => {
    const failed = turnSummary("trn_1", 0, [["codex/gpt-5", null]], {
      status: "failed", stop: null, ended: null, error: { kind: "provider", message: "overloaded", status: 529 },
    }) as any;
    failed.requests[0].status = "failed";
    failed.requests[0].attempts = [
      { n: 1, status: "failed", delay: null, started: 1.1, finished: 1.2, error: "HTTP 529", usage: null },
      { n: 2, status: "failed", delay: 2, started: 3.2, finished: 3.3, error: "HTTP 529 again", usage: null },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/turn*": turnPage([failed]) }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-diag");
    const shown = app.texts().join("\n");
    expect(shown.includes("provider retry 2/2 in 2.0s")).toBe(true);
    expect(shown.includes("provider retries exhausted")).toBe(true);
    expect(shown.includes("provider: overloaded")).toBe(true);
  });

  test("a turn the client cannot read details for is announced, and the timeline still loads", async () => {
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/turn*": { status: 500, body: { error: "boom" } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-older");
    expect(app.texts().join("\n").includes("Turn details failed to load")).toBe(true);
    expect(app.texts().includes("done")).toBe(true);
  });
});

describe("sidebar row menus", () => {
  test("session menu comes from row_menu commands and supports Shift+F10", async () => {
    const app = await open("./App.tsx", { network: selectedSessionNetwork(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.rightClick("id:session-ses_1");
    await app.waitFor("text:Rename session");
    let shown = app.texts().join("\n");
    for (const label of ["Rename session", "Archive session", "Unarchive session", "Delete session", "Export session", "Copy session id", "Open session in inspector"])
      expect(shown.includes(label)).toBe(true);
    expect(shown.includes("This session is not archived.")).toBe(true);
    await app.keyboard.press("escape");
    await app.press("id:session-ses_1");
    await app.keyboard.press("shift+f10");
    await app.waitFor("text:Open session in inspector");
    shown = app.texts().join("\n");
    expect(shown.includes("Delete session") && shown.includes("Export session")).toBe(true);
  });

  test("project row menu confirms removal with its session count; a disconnected Core blocks the delete", async () => {
    const child = { ...snapshot.sessions[0], id: "ses_child", title: "Child", parent_id: "ses_1", parent_call_id: "call_1" };
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        "GET */api/snapshot*": { body: { ...snapshot, sessions: [snapshot.sessions[0], child] } },
        "DELETE */api/project/prj_1": { status: 204, body: null },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:tab-projects");
    await app.waitFor("id:project-prj_1");
    expect(app.shows("id:add-project")).toBe(true);
    await app.rightClick("id:project-prj_1");
    await app.waitFor("text:Remove project");
    await app.press("id:row-action-project.remove");
    await app.waitFor("id:confirm-dialog");
    const dialog = app.texts().join("\n");
    expect(dialog.includes("demo") && dialog.includes("2 sessions")).toBe(true);
    expect(dialog.includes("The directory and its files stay. This cannot be undone.")).toBe(true);
    await app.press("id:confirm-ok");
    expect(app.requests().some((request) => request.method === "DELETE" && request.url.includes("/api/project/prj_1"))).toBe(false);
    expect(app.texts().join("\n").includes("Core is not connected: the view is read-only.")).toBe(true);
  });
});

describe("Core-backed settings values", () => {
  test("Providers and Agents open as pages with connections, odds and fallbacks", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        "GET */api/model": { body: { source: "fixture catalog", models: [{ ref: "codex/gpt-5", name: "GPT-5", context: 200_000 }] } },
        "GET */api/model/last*": { body: { model: "codex/gpt-5" } },
        "GET */api/config": { body: { revision: "rev1", values: { agents: { models: { plan: ["codex/gpt-5", "groq/llama"] }, fallbacks: { plan: ["codex/gpt-4"] } }, defaults: { effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
        "PATCH */api/config": { body: { revision: "rev2", values: { defaults: { effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
        "GET */api/provider": { body: [{ id: "codex", label: "Codex", connected: true, verified: true, methods: ["device"] }, { id: "groq", label: "Groq", connected: false, verified: true, methods: ["api_key"], key_env: "GROQ_API_KEY", key_hint: "from console.groq.com/keys", key_source: null }] },
        "GET */api/agent*": { body: { agents: [{ name: "build", mode: "all", source: "builtin", description: "Builds", subagent: { pool: [], fallbacks: [], effective: { model: "", source: "parent" } } }, { name: "plan", mode: "subagent", source: "builtin", description: "Plans", subagent: { pool: [], fallbacks: [], effective: { model: "", source: "parent" } } }] } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:demo");
    await app.press("id:open-settings");
    expect(app.shows("id:section-models")).toBe(false);
    await app.press("id:section-providers");
    await app.waitFor("id:settings-page-providers");
    await app.waitFor("id:provider-codex");
    const text = () => app.texts().join("\n");
    expect(text().includes("Connected · 1 model")).toBe(true);
    expect(text().includes("Not connected · API key")).toBe(true);
    expect(text().includes("Add key")).toBe(true); // a key provider; pressing it needs a live stream (see PLAN.md, Browser tests)
    expect(app.texts().includes("esc back")).toBe(true);
    await app.keyboard.press("escape");
    await app.waitFor("id:page-entry-providers");
    expect(app.shows("id:settings")).toBe(true);
    await app.press("id:section-agents");
    await app.waitFor("id:agent-row-plan");
    expect(text().includes("Default agent for new sessions")).toBe(true);
    expect(text().includes("parent's model")).toBe(true); // build has no models of its own
    expect(text().includes("2 models · 1 fallback · 1 skipped")).toBe(true);
    expect(app.shows("id:agent-models-plan")).toBe(false); // lists live on each subagent's page
    await app.screenshot("/private/tmp/claude-501/-Users-santa-repos-nexus10/a98b4c5b-f494-47cf-a88b-f5f971196393/scratchpad/nsh-agents-list.png");
    await app.press("id:agent-row-plan");
    await app.waitFor("id:agent-models-plan");
    await app.screenshot("/private/tmp/claude-501/-Users-santa-repos-nexus10/a98b4c5b-f494-47cf-a88b-f5f971196393/scratchpad/nsh-agent-plan.png");
    expect(text().includes("100%")).toBe(true); // groq is not connected: codex has all the odds
    expect(text().includes("not connected · skipped")).toBe(true);
    expect(text().includes("1.")).toBe(true);
    await app.keyboard.press("escape"); // back to the list, then to Settings
    await app.waitFor("id:agent-row-plan");
    await app.keyboard.press("escape");
    await app.waitFor("id:page-entry-agents");
  });
});

describe("image paste and background shell context", () => {
  test("pasted images preview, remove, and send without text", async () => {
    const app = await open("./ComposerFixture.tsx", {
      network: selectedSessionNetwork({ "POST */api/session/ses_1/prompt": { status: 202, body: { status: "busy" } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:msg-user");
    await app.screenshot("/private/tmp/nexus-before-paste.png");
    await app.paste("id:composer-input", { files: ["./fixtures/clipboard.png"] });
    await app.waitFor("id:image-previews");
    await app.screenshot("/private/tmp/nexus-image-preview.png");
    await app.press("text:Remove image");
    expect(app.shows("id:image-previews")).toBe(false);
    await app.paste("id:composer-input", { files: ["./fixtures/clipboard.png"] });
    await app.waitFor("id:image-previews");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/prompt")));
    const req = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/prompt"))!;
    const body = JSON.parse(req.body!);
    expect(body.text).toBe("");
    expect(body.images.length).toBe(1);
    expect(body.images[0].data_url.startsWith("data:image/png;base64,")).toBe(true);
    await app.waitUntil(() => !app.shows("id:image-previews"));
  });

  test("a background shell completion has no user-message card", async () => {
    const notice = { ...userMessage, id: "background", seq: 3, parts: [{ type: "text", text: "→ Background Shell Done: 'make test'\nexit code 0" }] };
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: [notice] } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:background-shell-done");
    expect(app.shows("id:msg-user")).toBe(false);
    await app.screenshot("/private/tmp/nexus-background-shell.png");
  });
});
