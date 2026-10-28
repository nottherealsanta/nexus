// Headless interaction tests (`nsh test`): the real runtime, a fake network standing in for the host's /api proxy.
import { describe, expect, open, test } from "nsh/test";
import { manifest } from "./src/gen/manifest";
import { agentSlot } from "./src/agent-color";
import { Model, foldEventPages } from "./src/core/reduce";

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
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/session/*": { body: snapshot.sessions[0] },
});
const mod = "mod";
const userMessage = { id: "m_user", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "hello" }], created: 1 };
const assistantMessage = { id: "m_assistant", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "done" }], created: 2 };

function selectedSessionNetwork(extra: Record<string, unknown> = {}) {
  return {
    "GET */api/session/ses_1": { body: { ...snapshot.sessions[0], model: "codex/gpt-5", directory: "/work/demo" } },
    "GET */api/session/ses_1/message": { body: [userMessage, assistantMessage] },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/event*": { body: { events: [], more: false, floor: 0, history_truncated: false } },
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
  test("header anchors, left tabs and fixed Settings entry", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.waitFor("text:demo");
    expect(app.shows("id:toggle-left")).toBe(true);
    expect(app.shows("id:toggle-right")).toBe(true);
    expect(app.shows("id:open-settings")).toBe(true);
    // tabs: Sessions (selected, with label), Files, Projects, in manifest order
    const labels = (manifest.navigation.left_sidebar.tab as readonly { id: string }[]).map((t) => t.id);
    expect(labels).toEqual(["sessions", "files", "projects"]);
    expect(app.shows("id:tab-sessions") && app.shows("id:tab-files") && app.shows("id:tab-projects")).toBe(true);
  });

  test("settings sections follow the manifest order", async () => {
    const app = await open("./App.tsx", { network: core(), width: 1024, height: 768, platform: "web" });
    await app.keyboard.press(`${mod}+,`);
    await app.waitFor("id:settings");
    const expected = (manifest.settings.section as readonly { label: string }[]).map((s) => s.label);
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
    // blocked_core command: present, disabled, with its reason
    expect(app.shows("text:Hand off to a new session")).toBe(true);
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
  const ev = (id: number) => ({ id, type: "usage", session_id: "ses_1", cursor: `log_t:${id}`, seq: id, time: 0, data: {} }) as any;
  const msg = (id: string, seq: number) => ({ id, session_id: "ses_1", seq, role: "assistant", parts: [], time: 0 }) as any;

  test("event pages keep the newest and say what is missing", async () => {
    expect(foldEventPages([{ events: [ev(4), ev(5)] }], null, 10)).toEqual({ events: [ev(4), ev(5)], gap: null });
    const capped = foldEventPages([{ events: [ev(4), ev(5), ev(6)], floor: 3, history_truncated: true }, { events: [ev(7), ev(8)] }], null, 3);
    expect(capped.events.map((e) => e.id)).toEqual([6, 7, 8]);
    expect(capped.gap!.includes("at or before event 3 expired")).toBe(true);
    expect(capped.gap!.includes("2 older diagnostics")).toBe(true);
    const failed = foldEventPages([{ events: [ev(4)] }], { after: 4, error: "timed out" }, 10);
    expect(failed.gap!.includes("after event 4 failed to load (timed out)")).toBe(true);
  });

  test("history keeps messages and events the stream applied after the fetch", async () => {
    const m = new Model();
    m.select("ses_1");
    m.selected!.messages = [msg("m1", 1), msg("m3", 3)]; // m3 arrived live
    m.selected!.events = [ev(9)];
    m.applyHistory({ sessionId: "ses_1", messages: [msg("m1", 1), msg("m2", 2)], events: [ev(4)], starting: null, eventsGap: null });
    expect(m.selected!.messages.map((x) => x.id)).toEqual(["m1", "m2", "m3"]);
    expect(m.selected!.events.map((e) => e.id)).toEqual([4, 9]);
  });

  test("selecting loads history without a global snapshot; a daemon restart reloads it", async () => {
    let pid = 1;
    const app = await open("./App.tsx", {
      clock: {},
      network: {
        "GET */api/session/ses_1/message": { body: [] },
        "GET */api/session/ses_1/snapshot": { body: {} },
        "GET */api/session/ses_1/event*": { body: { events: [], more: false, floor: 0, history_truncated: false } },
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
    instruction_file: { path: "/work/demo/AGENTS.md", status: "loaded", bytes: 800 },
    agent: { name: "build", source: "builtin", description: "Does the work." },
    child_agents: [{ name: "scout" }, { name: "plan" }],
    workspace: { active: true },
  };

  test("starting context shows the TUI items, estimates and catalog window share", async () => {
    const app = await open("./App.tsx", {
      network: {
        ...selectedSessionNetwork({
          "GET */api/session/ses_1/message": { body: [] },
          "GET */api/session/ses_1/snapshot": { body: starting },
          "GET */api/model*": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-5", context: 100000 }] } },
        }),
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:context-section");
    const shown = app.texts().join("\n");
    for (const text of [
      "Context", "6 items", "~1.0K tokens", "1.0% of window", "System prompt", "You are coding in Nexus.",
      "Tools", "[3]", "read  bash  edit", "~6", "Instructions", "AGENTS.md loaded", "800 B, part of the system prompt",
      "Agent", "build", "builtin · Does the work.", "Child agents", "scout  plan", "Workspace tools", "active", "total", "of the window",
    ]) expect(shown.includes(text)).toBe(true);
  });

  test("turn footer prices cached and uncached input using the catalog", async () => {
    const events = [
      { id: 1, type: "wire_request", session_id: "ses_1", cursor: "log_t:1", seq: 1, time: 1.1, data: { body: { model: "gpt-5" } } },
      { id: 2, type: "usage", session_id: "ses_1", cursor: "log_t:2", seq: 2, time: 1.2, data: { input_tokens: 100000, cached_input_tokens: 50000, output_tokens: 10000 } },
      { id: 3, type: "turn_end", session_id: "ses_1", cursor: "log_t:3", seq: 3, time: 1.9, data: { stop: "completed", steps: 1 } },
    ];
    const app = await open("./App.tsx", {
      network: selectedSessionNetwork({
        "GET */api/session/ses_1/event*": { body: { events, more: false, floor: 0, history_truncated: false } },
        "GET */api/model*": { body: { models: [{ ref: "codex/gpt-5", provider: "codex", name: "GPT-5", context: 400000, input_cost: 1, output_cost: 10, cache_read_cost: 0.2 }] } },
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

  test("location strip uses the longest matching linked worktree and shows disabled action reasons", async () => {
    const session = { ...snapshot.sessions[0], directory: "/work/demo/ws/nested", model: "codex/gpt-5" };
    const app = await open("./App.tsx", {
      network: {
        ...selectedSessionNetwork({
          "GET */api/snapshot*": { body: { ...snapshot, sessions: [session] } },
          "GET */api/session/ses_1": { body: session },
          "GET */api/session/ses_1/worktrees": { body: {
            session_directory: session.directory, project_directory: "/work/demo",
            worktrees: [
              { path: "/work/demo", branch: "trunk", main: true },
              { path: "/work/demo/ws", branch: "feature", main: false },
              { path: "/work/demo/ws/nested", branch: "nested", main: false },
            ],
          } },
        }),
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:location-worktree");
    const shown = app.texts().join("\n");
    expect(shown.includes("demo")).toBe(true);
    expect(shown.includes("nested")).toBe(true);
    expect(shown.includes("/work/demo/ws/nested")).toBe(true);
    expect(shown.includes("worktree")).toBe(true);
    expect(app.requests().some((r) => r.url.includes("/api/session/ses_1/worktrees"))).toBe(true);
    expect(app.shows("id:location-directory")).toBe(true);
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
    await app.waitFor("text:trunk");
    expect(app.requests().some((r) => r.url.includes("/api/project/prj_1/worktrees"))).toBe(true);
    expect(app.shows("id:location-worktree")).toBe(false);
  });
});
