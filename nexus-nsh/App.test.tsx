// Headless interaction tests (`nsh test`): the real runtime, a fake network standing in for the host's /api proxy.
import { describe, expect, open, test } from "nsh/test";
import { manifest } from "./src/gen/manifest";
import { agentSlot } from "./src/agent-color";
import { Model, VIEW_CACHE, scratchOf, upsertTurn } from "./src/core/reduce";
import { groupRequests, latestUsage, ledgerTotals, sumUsage } from "./src/core/turns";
import { buildRows, capToolDetail, capToolOutput, detailOf, errorText, fmtDuration, patchArg } from "./src/ui/timeline";
import { model } from "./src/store";
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
      [{ name: "ready", state: "on", scope: "project" }, { name: "late", state: "off", locked: true }],
      [{ server: "broken", state: "unavailable", error: "connection refused", tools: null, locked: true, scope: "global" }],
    );
    expect(rows.map((row) => [row.name, row.on, row.hint, row.error])).toEqual([
      ["ready", true, undefined, undefined],
      ["late", false, "new session", undefined],
      ["broken", false, "new session", "connection refused"],
    ]);
    expect(rows.map((row) => row.scope)).toEqual(["project", undefined, "global"]);
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
        { server: "docs", tools: 5, state: "on", locked: false, error: null, scope: "global" },
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
    expect(app.texts().includes("P") || app.texts().includes("A")).toBe(false);
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
    await app.waitFor("id:rail-project-prj_1");
    expect(app.shows("id:header")).toBe(true);
    expect(app.shows("id:toggle-left")).toBe(true);
    expect(app.shows("id:toggle-right")).toBe(true);
    expect(app.shows("id:open-manager-context")).toBe(true);
    expect(app.shows("id:open-settings")).toBe(true);
    // tabs: Sessions and Files, in manifest order; docked, they sit in the header's left slot
    const labels = (manifest.navigation.left_sidebar.tab as readonly { id: string }[]).map((t) => t.id);
    expect(labels).toEqual(["sessions", "files"]);
    expect(app.shows("id:tab-sessions") && app.shows("id:tab-files") && !app.shows("id:tab-projects")).toBe(true);
    // the icons follow the left sidebar: closing it hides them, reopening shows them again
    await app.press("id:toggle-left");
    expect(app.shows("id:tab-files")).toBe(false);
    await app.press("id:toggle-left");
    expect(app.shows("id:tab-files")).toBe(true);
  });

  test("the collapsed project rail counts sessions by state", async () => {
    const busy = { ...snapshot.sessions[0], id: "ses_busy", title: "Busy task", status: "busy", created: 3, updated: 4 };
    const empty = { id: "prj_2", path: "/work/empty", name: "empty", created: 0.5, updated: 0, revision: 1 };
    const app = await open("./App.tsx", {
      network: { ...core(), "GET */api/snapshot*": { body: { ...snapshot, projects: [...snapshot.projects, empty], sessions: [...snapshot.sessions, busy] } } },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.press("id:toggle-left");
    await app.waitFor("id:rail-count-prj_1-running");
    expect(app.shows("id:rail-count-prj_1-running")).toBe(true);
    expect(app.shows("id:rail-count-prj_1-idle")).toBe(true);
    await app.waitFor("id:rail-placeholder-prj_2");
    expect(app.shows("id:rail-count-prj_2-attention")).toBe(false);
    expect(app.shows("id:rail-count-prj_2-idle")).toBe(false);
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
    // ses_2 is already viewed: pressing its title would rename it, as in the TUI.
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
    await app.waitFor("id:rail-project-prj_1");
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
    expect(JSON.parse(request.body)).toEqual({ project: "prj_1", agent: "review", model: "codex/gpt-5", effort: "high" });
    // The created session's tab replaces the New session tab at once; it does not wait for
    // the session's event or detail read (which would flash the header back to New session).
    await app.waitFor("id:session-tab-ses_new");
    expect(app.shows("id:new-session-tab")).toBe(false);
  });

  test("settings sections follow the manifest order", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.keyboard.press(`${mod}+,`);
    await app.waitFor("id:settings");
    const expected = (manifest.settings.section as readonly { id: string; label: string }[]).filter((s) => s.id !== "models").map((s) => s.label);
    const shown = app.texts().filter((t) => expected.includes(t));
    // The header's Manager button shares its label with the Manager section: start at the rail.
    const rail = shown.slice(shown.indexOf("Appearance"));
    expect(rail.slice(0, expected.length)).toEqual(expected);
    expect(expected[0]).toBe("Appearance");
    expect(expected[expected.length - 1]).toBe("About");
  });

  test("palette opens with the key, lists disabled commands with reasons, Esc walks the chain", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_1");
    await app.keyboard.press(`${mod}+p`);
    await app.waitFor("id:palette");
    expect(app.shows("id:palette")).toBe(true);
    // blocked_core command: present, disabled, with its reason once its row is the selected one (D13)
    await app.type("id:palette-input", "Attach file");
    await app.waitFor("text:/Attach file or image/");
    expect(app.texts().some((t) => t.startsWith("Attach file or image · "))).toBe(true);
    await app.keyboard.press("escape");           // rule 1: overlay closes
    expect(app.shows("id:palette")).toBe(false);
    await app.keyboard.press(`${mod}+,`);         // settings opens
    await app.waitFor("id:settings");
    await app.keyboard.press("escape");           // rule 9: settings returns to the previous view
    expect(app.shows("id:settings")).toBe(false);
  });

  test("model and agent pickers are fed by Core, not hard-coded lists", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:rail-project-prj_1");
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
    await app.waitFor("id:rail-project-prj_1");
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
    // The starting context stays open once a session has messages, so its sections are always reachable.
    await app.press("id:ctx-system-open");
    await app.waitFor("id:context-detail-markdown");
    expect(app.shows("id:context-detail")).toBe(true);
    expect(app.texts().join("\n").includes("You are coding in Nexus.")).toBe(true);
    await app.keyboard.press("escape");
    await app.press("id:ctx-instructions-open");
    await app.waitFor("id:context-detail-markdown");
    expect(app.shows("id:context-detail")).toBe(true);
    expect(app.texts().join("\n").includes("Frozen instructions")).toBe(true);

  });

  test("changing effort leaves the starting-context counts and composer meter alone", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        "POST */api/draft/context": {
          body: {
            used: 4200, window: 200000, exact: false,
            parts: { instructions: 3000, tools: 1200, instruction_file: 100 },
            tools: ["read", "bash"], skills: [], mcp: [], session: { effort: null },
          },
          delay: 300,
        },
        "GET */api/model": { body: { source: "t", models: [{ ref: "scripted/m", provider: "scripted", name: "Scripted", context: 200000, efforts: ["low", "medium", "high"] }], model: "scripted/m" } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:~4,200 tok");
    await app.waitFor("id:composer-usage");
    expect(app.texts().join("\n").includes("~4.2K · 2.1%")).toBe(true);

    await app.type("id:composer-input", "/effort");
    await app.press("id:send");
    await app.waitFor("id:picker");
    await app.press("id:pick-1");
    await app.waitFor("text:The new session will use effort medium");
    // The re-plan is in flight: effort is not context, so the counts stay put.
    expect(app.texts().join("\n").includes("~4,200 tok")).toBe(true);
    expect(app.texts().join("\n").includes("~4.2K · 2.1%")).toBe(true);
    await app.wait(400);
    expect(app.texts().join("\n").includes("~4,200 tok")).toBe(true);
    expect(app.texts().join("\n").includes("~4.2K · 2.1%")).toBe(true);
  });

  test("turn footer orders model, tools, tokens, duration and finish time", async () => {
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/turn*": turnPage([turnSummary("trn_1", 0, [["codex/gpt-5", usage(100000, 10000, 50000, 500)]])]),
        "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-5", context: 400000, input_cost: 1, output_cost: 10, cache_read_cost: 0.2 }] } },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:turn-footer");
    const footer = app.texts().join("\n");
    const parts = ["GPT-5", "0 tools", "100Ki", "10.0Ko", "500r", "50%c", "1.0s", new Date(2000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })];
    let previous = -1;
    for (const part of parts) {
      const index = footer.indexOf(part, previous + 1);
      expect(index > previous).toBe(true);
      previous = index;
    }
  });

  test("a running turn has no footer and its live thought stays in the activity group", () => {
    const message = (id: string, seq: number, role: "user" | "assistant" | "tool", parts: any[]) => ({
      id, session_id: "ses_1", seq, role, parts, created: seq,
    });
    model.selected = {
      sessionId: "ses_1",
      messages: [
        message("m1", 1, "user", [{ type: "text", text: "go" }]),
        message("m2", 2, "assistant", [{ type: "tool_call", id: "c1", name: "bash", arguments: "{}" }]),
        message("m3", 3, "tool", [{ type: "tool_result", call_id: "c1", output: "ok" }]),
      ],
      messagesMore: false, turns: [], floor: null, usage: null, usageMark: 0, scratch: [], loaded: true, starting: null, turnsGap: null,
    };
    model.sessions.set("ses_1", { id: "ses_1", project_id: "prj_1", directory: "/", title: "T", agent: "build", parent_id: null, revision: 1, created: 1, updated: 1, status: "busy" });
    model.running.set("ses_1", "trn_1");
    model.streaming.set("ses_1", { text: "", reasoning: "Still thinking" });
    try {
      const rows = buildRows();
      expect(rows.some((r) => r.kind === "footer")).toBe(false);
      const group = rows.find((r) => r.kind === "activity");
      expect(group?.kind === "activity" && group.summary === "Working").toBe(true);
      expect(group?.kind === "activity" && group.items.some((r) => r.kind === "reasoning" && r.text === "Still thinking")).toBe(true);
      model.streaming.delete("ses_1");
      const trailing = buildRows().find((r) => r.kind === "activity");
      expect(trailing?.kind === "activity" && trailing.open && trailing.summary === "Working").toBe(true);
      model.running.delete("ses_1");
      const finished = buildRows().find((r) => r.kind === "activity");
      expect(finished?.kind === "activity" && !finished.open && finished.summary === "1 command").toBe(true);
    } finally {
      model.selected = null;
      model.sessions.delete("ses_1");
      model.running.delete("ses_1");
      model.streaming.delete("ses_1");
    }
  });

  test("a turn with background-shell notices has one footer, after its last notice", () => {
    const message = (id: string, seq: number, role: "user" | "assistant" | "tool", parts: any[]) => ({
      id, session_id: "ses_1", seq, role, parts, created: seq,
    });
    const notice = (id: string, seq: number) => message(id, seq, "user", [{ type: "text", text: "→ Background Shell Done: 'make'\nexit code 0" }]);
    model.selected = {
      sessionId: "ses_1",
      messages: [
        message("m1", 1, "user", [{ type: "text", text: "go" }]),
        message("m2", 2, "assistant", [{ type: "text", text: "started" }]),
        notice("n1", 3),
        message("m3", 4, "assistant", [{ type: "text", text: "again" }]),
        notice("n2", 5),
        message("m4", 6, "user", [{ type: "text", text: "second" }]),
        message("m5", 7, "assistant", [{ type: "text", text: "ok" }]),
      ],
      messagesMore: false, turns: [], floor: null, usage: null, usageMark: 0, scratch: [], loaded: true, starting: null, turnsGap: null,
    };
    try {
      const rows = buildRows();
      const footers = rows.filter((r) => r.kind === "footer").map((r) => r.key);
      expect(footers).toEqual(["footer:ses_1:0", "footer:ses_1:1"]);
      const at = (key: string) => rows.findIndex((r) => r.key === key);
      expect(at("footer:ses_1:0")).toBe(at("n2") + 1);
      expect(at("footer:ses_1:0")).toBeLessThan(at("m4"));
    } finally {
      model.selected = null;
    }
  });

  test("patchArg shows the first patched path and counts the others", () => {
    expect(patchArg("*** Begin Patch\n*** Update File: /a/b.rs\n@@\n-x\n+y\n*** End Patch")).toBe("/a/b.rs");
    expect(patchArg("*** Begin Patch\n*** Add File: a.txt\n+x\n*** Delete File: b.txt\n*** Update File: a.txt\n*** End Patch")).toBe("a.txt +1 file");
    expect(patchArg("*** Begin Patch\n*** Update File: a\n*** Update File: b\n*** Update File: c\n*** End Patch")).toBe("a +2 files");
    expect(patchArg("not a patch")).toBe(null);
  });

  test("an error says its kind once", () => {
    expect(errorText("interrupted", "interrupted by the user")).toBe("Interrupted by the user");
    expect(errorText("rate_limit", "429 too many requests")).toBe("rate_limit: 429 too many requests");
    expect(errorText("interrupted", "")).toBe("interrupted");
  });

  test("location strip shows the context meter", async () => {
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/turn*": turnPage([turnSummary("trn_1", 0, [["codex/gpt-5", usage(1000)], ["codex/gpt-5", usage(11100)]])]),
        "GET */api/session/*/context": { body: { used: 11100, window: 1_000_000, exact: true } },
        "GET */api/model": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-6.1 Terra", context: 1_000_000, tiers: [270_000] }] } },
      }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-usage");
    const shown = app.texts().join("\n");
    expect(shown.includes("GPT-6.1 Terra") && shown.includes("codex")).toBe(true);
    expect(shown.includes("11K · 1.1%")).toBe(true);
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
    await app.press("text:1 edit");
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
  test("a diff or the raw view keeps a head and a tail; command output keeps its command rows and last rows", () => {
    const output = Array.from({ length: 500 }, (_, i) => `plain line ${i + 1}`).join("\n");
    const patch = Array.from({ length: 500 }, (_, i) => `+diff line ${i + 1}`).join("\n");
    const args = JSON.stringify({ command: "printf output" });
    for (const lines of [detailOf("bash", args, output, true), detailOf("edit", JSON.stringify({ patch }), undefined, false)]) {
      const capped = capToolDetail(lines, false);
      expect(capped).toHaveLength(201);
      expect(capped[100].text).toContain(`${lines.length - 200} lines hidden · show all (o)`);
      expect(capToolDetail(lines, true)).toEqual(lines);
    }
    const command = detailOf("bash", args, output, false);
    expect(command[0]).toEqual({ text: "$ printf output", tone: "command" });
    const capped = capToolOutput(command, false, 1);
    expect(capped.map((l) => l.text)).toEqual([
      "$ printf output", "↑ 492 earlier lines · enter show all",
      ...Array.from({ length: 8 }, (_, i) => `plain line ${493 + i}`),
    ]);
    expect(capToolOutput(command, true, 1)[1].text).toBe("↑ showing all 500 lines · o show less");
    const short = detailOf("bash", args, "one\ntwo", false);
    expect(capToolOutput(short, false, 1)).toEqual(short);
    expect(detailOf("bash", args, undefined, false, false, undefined, false).at(-1)).toEqual({ text: "no output", tone: "muted" });
  });

  test("command output folds to its last rows, a press or o restores it, and a diff keeps its head and tail", async () => {
    const plain = Array.from({ length: 500 }, (_, i) => `plain line ${i + 1}`).join("\n");
    const patch = Array.from({ length: 500 }, (_, i) => `+diff line ${i + 1}`).join("\n");
    const messages = [
      { ...userMessage, parts: [{ type: "text", text: "Run the tools" }] },
      { id: "m_tools", session_id: "ses_1", seq: 2, role: "assistant", created: 2, parts: [{ type: "tool_call", id: "call_bash", name: "bash", arguments: JSON.stringify({ command: "printf output" }) }] },
      { id: "m_result", session_id: "ses_1", seq: 3, role: "tool", created: 3, parts: [{ type: "tool_result", call_id: "call_bash", output: plain, is_error: false }] },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: messages } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("text:1 command");
    await app.press("text:1 command");
    await app.waitFor("id:tool-row-call_bash");
    await app.press("id:tool-row-call_bash");
    await app.waitFor("text:plain line 500");
    let text = app.texts().join("\n");
    expect(text.includes("$ printf output") && text.includes("plain line 493")).toBe(true);
    expect(text.includes("↑ 492 earlier lines · enter show all")).toBe(true);
    expect(text.includes("plain line 492")).toBe(false);

    await app.press("id:tool-show-all-call_bash");
    await app.wait(300);
    expect(app.texts().join("\n").includes("plain line 250")).toBe(true);
    expect(app.texts().join("\n").includes("↑ showing all 500 lines · o show less")).toBe(true);
    await app.keyboard.press("o");
    expect(app.texts().join("\n").includes("plain line 250")).toBe(false);
    await app.press("id:tool-raw-call_bash");
    await app.waitFor('text:{"command":"printf output"}');

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
    await diffApp.waitFor("text:1 edit");
    await diffApp.press("text:1 edit");
    await diffApp.waitFor("id:tool-row-call_edit");
    await diffApp.press("id:tool-row-call_edit");
    await diffApp.waitFor("text:/diff line 100/");
    text = diffApp.texts().join("\n");
    expect(text.includes("diff line 1") && text.includes("diff line 100")).toBe(true);
    expect(text.includes("diff line 401") && text.includes("diff line 500")).toBe(true);
    expect(text.includes("300 lines hidden · show all (o)")).toBe(true);
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
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: messages }, "PATCH */api/config": { body: {} } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("text:2 commands");
    expect(app.shows("id:tool-row-call_bash")).toBe(false);
    await app.keyboard.press("alt+d");
    await app.waitFor("text:Detail: high");
    await app.waitFor("text:high line 120");
    let text = app.texts().join("\n");
    expect(text.includes("high line 113") && text.includes("high line 120")).toBe(true);
    expect(text.includes("↑ 112 earlier lines · enter show all")).toBe(true);
    expect(text.includes("high line 112")).toBe(false);
    await app.press("id:tool-show-all-call_bash");
    await app.wait(300);
    expect(app.texts().join("\n").includes("high line 100")).toBe(true);
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
    await app.press("id:toggle-right");
    await app.waitFor("id:right-sidebar");
    await app.waitFor("text:61.0K");
    const shown = app.texts().join("\n");
    for (const expected of [
      "Turns", "1 · 3 calls", "Used", "bash 12 · edit 4 · +2", "Tools", "9 on · 1 off",
      "Input", "61.0K", "Cached", "20.0K", "Output", "3.0K", "Reasoning", "500",
      "Context", "25%", "Cost", "$0.07+", "Children", "2 (1 running, 1 failed)",
    ]) expect(shown.includes(expected)).toBe(true);
  });

  test("says when Core has not reported usage", async () => {
    const app = await open("./App.tsx", { network: selectedSessionNetwork(), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:toggle-right");
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
    await app.press("id:toggle-right");
    await app.waitFor("text:1 request reported no usage (unknown, not zero)");
    const shown = app.texts().join("\n");
    expect(shown.includes("Turns above have no details")).toBe(true);
    expect(shown.includes("usage unknown")).toBe(true);
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
    let info = 0;
    const app = await open("./App.tsx", {
      network: {
        ...core(),
        // The first handshake answers; the stream ends and every reconnect is refused, so the view
        // goes read-only while the project row is still there.
        "GET */api/info": () => info++ === 0 ? { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } } : { status: 502, body: { error: "Core went away" } },
        "GET */api/snapshot*": { body: { ...snapshot, sessions: [snapshot.sessions[0], child] } },
        "DELETE */api/project/prj_1": { status: 204, body: null },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.waitUntil(() => app.texts().join(" ").includes("Disconnected from Core"));
    await app.keyboard.press("mod+3");
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
        "GET */api/model": { body: { source: "fixture catalog", models: [{ ref: "codex/gpt-5", name: "GPT-5", context: 200_000, efforts: ["low", "medium", "high"] }] } },
        "GET */api/model/last*": { body: { model: "codex/gpt-5" } },
        "GET */api/config": { body: { revision: "rev1", values: { agents: { models: { plan: ["codex/gpt-5", "groq/llama"] }, fallbacks: { plan: ["codex/gpt-4"] }, efforts: { plan: { "codex/gpt-5": "low" } } }, defaults: { effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
        "PATCH */api/config": { body: { revision: "rev2", values: { defaults: { effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
        "GET */api/provider": { body: [{ id: "codex", label: "Codex", connected: true, verified: true, methods: ["browser", "device"] }, { id: "groq", label: "Groq", connected: false, verified: true, methods: ["api_key"], key_env: "GROQ_API_KEY", key_hint: "from console.groq.com/keys", key_source: null }, { id: "grok", label: "Grok", connected: false, verified: true, methods: ["browser", "device"] }] },
        "GET */api/agent*": { body: { agents: [{ name: "build", mode: "all", source: "builtin", description: "Builds", subagent: { pool: [], fallbacks: [], effective: { model: "", source: "parent" } } }, { name: "plan", mode: "subagent", source: "builtin", description: "Plans", subagent: { pool: [], fallbacks: [], effective: { model: "", source: "parent" } } }] } },
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.press("id:open-settings");
    expect(app.shows("id:section-models")).toBe(false);
    await app.press("id:section-providers");
    await app.waitFor("id:settings-page-providers");
    await app.waitFor("id:provider-codex");
    const text = () => app.texts().join("\n");
    expect(text().includes("Connected · 1 model")).toBe(true);
    expect(text().includes("Not connected · API key")).toBe(true);
    await app.press("id:provider-open-groq");
    await app.waitFor("id:provider-detail");
    expect(text().includes("Add API key")).toBe(true); // a key provider; saving needs a live stream (see PLAN.md, Browser tests)
    await app.keyboard.press("escape");
    expect(app.shows("id:provider-detail")).toBe(false);
    // A provider with both methods offers both rows; Codex no longer only has a device code.
    await app.press("id:provider-open-grok");
    await app.waitFor("id:provider-detail");
    expect(text().includes("Sign in with browser") && text().includes("Sign in with a device code")).toBe(true);
    expect(app.shows("id:provider-detail-browser") && app.shows("id:provider-detail-device")).toBe(true);
    await app.keyboard.press("escape");
    // On the provider page the first Esc steps back to the section rail, then closes.
    expect(app.texts().includes("esc back")).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:settings")).toBe(true);
    expect(app.texts().includes("esc close")).toBe(true);
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:settings"));
    await app.press("id:open-settings");
    await app.waitFor("id:settings");
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
    expect(text().includes("effort low")).toBe(true); // the pool model's configured effort
    expect(app.shows("id:agent-effort-plan-groq/llama")).toBe(false); // groq lists no levels and has none set
    await app.press("id:agent-effort-up-plan-codex/gpt-5"); // writes agents.efforts; the mock config answers unchanged
    expect(text().includes("←/→ sets this model's effort")).toBe(true);
    await app.keyboard.press("escape"); // back to the Agents list
    await app.waitFor("id:agent-row-plan");
    await app.keyboard.press("escape"); // back to the section rail
    expect(app.shows("id:settings")).toBe(true);
    await app.keyboard.press("escape"); // close Settings
    await app.waitUntil(() => !app.shows("id:settings"));
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
    await app.press("id:composer-image-1");
    await app.waitFor("id:annotate");
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:annotate"));
    await app.press("id:remove-image-1");
    await app.waitUntil(() => !app.shows("id:image-previews"));
    await app.paste("id:composer-input", { files: ["./fixtures/clipboard.png"] });
    await app.waitFor("id:image-previews");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/prompt")));
    const req = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/prompt"))!;
    const body = JSON.parse(req.body!);
    // The composer keeps the TUI's `[Image A]` marker in the text it sends; Core labels the image too.
    expect(body.text).toBe("[Image A]");
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

describe("large text paste", () => {
  const big = Array.from({ length: 8 }, (_, i) => `row ${i + 1}: ${"x".repeat(70)}`).join("\n");
  const marker = `[Pasted ~${Math.max(1, Math.floor([...big].length / 4))} tokens]`;

  test("collapses in the composer and sends a marker plus the full text", async () => {
    const app = await open("./ComposerFixture.tsx", {
      network: selectedSessionNetwork({ "POST */api/session/ses_1/prompt": { status: 202, body: { status: "busy" } } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:msg-user");
    await app.paste("id:composer-input", { text: big });
    await app.waitUntil(() => app.value("id:composer-input") === marker);
    expect(app.value("id:composer-input")).toBe(marker);
    await app.waitFor("id:paste-previews");
    expect(app.shows("id:paste-previews")).toBe(true);
    const shown = app.texts().join("\n");
    expect(shown).toContain("Pasted text");
    expect(shown).toContain("8 lines");
    expect(shown).toContain("Show");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/prompt")));
    const req = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/prompt"))!;
    const body = JSON.parse(req.body!);
    expect(body.text).toBe(marker);
    expect(body.pastes).toEqual([{ marker, text: big }]);
  });

  test("a short paste stays inline", async () => {
    const app = await open("./ComposerFixture.tsx", {
      network: selectedSessionNetwork(),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:msg-user");
    await app.paste("id:composer-input", { text: "one\ntwo\nthree" });
    await app.waitUntil(() => app.value("id:composer-input") === "one\ntwo\nthree");
    expect(app.shows("id:paste-previews")).toBe(false);
    expect(app.texts().join("\n").includes("Pasted text")).toBe(false);
  });

  test("the user message shows a clickable marker, never the pasted text", async () => {
    const collapsed = {
      id: "m_paste", session_id: "ses_1", seq: 1, created: 1, role: "user",
      parts: [
        { type: "text", text: "Summarise this log:", origin: null, raw: null },
        { type: "text", text: big, origin: null, raw: null, collapsed: true },
      ],
    };
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({ "GET */api/session/ses_1/message*": { body: [collapsed] } }),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("text:Refactor loader");
    await app.waitFor("id:msg-user");
    expect(app.shows("id:user-pastes")).toBe(true);
    const shown = app.texts().join("\n");
    expect(shown).toContain(marker);
    expect(shown.includes("row 1:")).toBe(false);
    await app.press("id:user-paste-1-0");
    await app.waitFor("id:context-detail");
    expect(app.texts().join("\n")).toContain("row 8:");
  });
});

describe("composer prompt history", () => {
  // The project's prompts, newest first, as Core serves them.
  const prompts = (...entries: string[]) => ({
    "GET */api/project/prj_1/prompts*": { body: { prompts: entries.map((text) => ({ text, image_count: 0 })) } },
  });
  const composer = async (entries: string[]) => {
    const app = await open("./ComposerFixture.tsx", {
      network: selectedSessionNetwork(prompts(...entries)),
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.waitUntil(() => app.requests().some((r) => r.url.includes("/api/project/prj_1/prompts")));
    return app;
  };

  test("Up walks a recalled multi-line prompt's lines before an older prompt", async () => {
    const app = await composer(["line one\nline two", "older"]);
    await app.type("id:composer-input", "mine");
    await app.keyboard.press("up"); // recalls with the caret at the prompt's end
    expect(app.value("id:composer-input")).toBe("line one\nline two");
    // The caret is on the recalled prompt's last line: Up belongs to the input, not to history.
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("line one\nline two");
    // On the first line it recalls the older prompt.
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("older");
    // Down comes back, and then to the draft the recall kept.
    await app.keyboard.press("down");
    expect(app.value("id:composer-input")).toBe("line one\nline two");
    await app.keyboard.press("down");
    expect(app.value("id:composer-input")).toBe("mine");
  });

  test("Up in a multi-line draft does not recall while its caret is below the first line", async () => {
    const app = await composer(["older"]);
    await app.type("id:composer-input", "a");
    await app.keyboard.press("shift+enter"); // a line break in a multiline input that submits
    await app.type("id:composer-input", "b");
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("a\nb");
    // The caret reached the first line: now Up recalls.
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("older");
  });

  test("Up on an empty composer steps through history and Down returns to the draft", async () => {
    const app = await composer(["newest", "older"]);
    await app.keyboard.press(`${mod}+l`); // composer.focus
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("newest");
    await app.keyboard.press("up");
    expect(app.value("id:composer-input")).toBe("older");
    await app.keyboard.press("down");
    expect(app.value("id:composer-input")).toBe("newest");
    await app.keyboard.press("down");
    expect(app.value("id:composer-input")).toBe("");
  });
});
