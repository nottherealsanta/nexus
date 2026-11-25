// The phone redesign of plans/05 section 5.2: sessions sections, the drawer, the row menu, the approval card, the Settings
// connection card and the pairing screen. Run without `--target` so the app runs in the phone idiom (390x844, "ios").
import { describe, expect, test } from "nsh/test";
import { open } from "./open-bundle";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const base = { model: "scripted/m", permission: "ask", effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const sessions = [
  { ...base, id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Migration plan", created: 1, updated: 2 },
  { ...base, id: "ses_2", project_id: "prj_1", directory: "/work/demo", title: "Chores", created: 2, updated: 3 },
  { ...base, id: "ses_3", project_id: "prj_1", directory: "/work/demo", title: "Build the thing", created: 3, updated: 4, status: "busy" },
  { ...base, id: "ses_4", project_id: "prj_2", directory: "/work/other", title: "Elsewhere", created: 4, updated: 5 },
];
const projects = [
  { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 },
  { id: "prj_2", path: "/work/other", name: "other", created: 1, updated: 2, revision: 1 },
];
const approval = { id: "perm_1", session_id: "ses_2", tool: "bash", call_id: "c1", args: { command: "rm -rf build && npm run build" } };
const empty = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };

function network(extra: Record<string, any> = {}) {
  const per = (id: string) => ({
    [`GET */api/session/${id}/message*`]: { body: [] },
    [`GET */api/session/${id}/snapshot`]: { body: null },
    [`GET */api/session/${id}/context`]: { body: { used: 0, window: null, exact: false } },
    [`GET */api/session/${id}/turn*`]: { body: empty },
    [`GET */api/session/${id}/worktrees`]: { body: { worktrees: [] } },
    [`GET */api/session/${id}/input`]: { body: { held: false, inputs: [] } },
    [`GET */api/session/${id}`]: { body: sessions.find((s) => s.id === id) },
    [`PATCH */api/session/${id}`]: { body: sessions.find((s) => s.id === id) },
    [`POST */api/session/${id}/archive`]: { body: {} },
    [`POST */api/session/${id}/permission/perm_1`]: { body: {} },
  });
  return {
    ...extra,
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "ask" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/snapshot*": { body: {
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects, sessions, running: [], approvals: [approval], questions: [], selected: null,
    } },
    "GET */api/event*": () => ({ body: connected, headers: { "content-type": "text/event-stream" } }),
    "GET */api/provider": { body: [] },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "GET */api/search*": { body: { hits: [] } },
    ...per("ses_1"), ...per("ses_2"), ...per("ses_3"), ...per("ses_4"),
    "GET */api/session/*": { body: sessions[0] },
  } as Record<string, any>;
}

async function openPhone(extra: Record<string, any> = {}, routes: Record<string, any> = {}, options: { safeArea?: { top: number; right: number; bottom: number; left: number }; clock?: { time?: number } } = {}) {
  const app = await open("./App.tsx", {
    network: { ...network(extra), ...routes }, width: 390, height: 844, platform: "ios", storage: { "nexus.core": "http://nexus.invalid:4747" },
    ...(options.safeArea ? { safe_area: options.safeArea } : {}), ...(options.clock ? { clock: options.clock } : {}),
  });
  await app.waitFor("id:splash", { state: "gone" });
  await app.waitFor("id:session-ses_1");
  return app;
}
const posts = (app: any, suffix: string) => app.requests().filter((r: any) => r.method === "POST" && r.url.endsWith(suffix));

const message = (id: string, seq: number, role: string, parts: any[]) => ({ id, session_id: "ses_1", seq, role, parts, created: seq });
const call = (id: string, name = "bash", args: Record<string, unknown> = { command: `echo ${id}` }) => ({ type: "tool_call", id, name, arguments: JSON.stringify(args) });

describe("phone sessions (plans/05 5.2)", () => {
  test("lists Needs you with its count, Running, then Recent, and marks the waiting row", async () => {
    const app = await openPhone();
    await app.waitFor("id:sessions-section-needs");
    expect(app.shows("id:sessions-section-running")).toBe(true);
    expect(app.shows("id:sessions-section-recent")).toBe(true);
    expect(app.shows("id:session-needs-ses_2")).toBe(true);
    expect(app.texts()).toContain("Wants to run bash");
    expect(app.texts()).toContain("rm -rf build && npm run build");
    expect(app.shows("id:session-needs-ses_1")).toBe(false);
  });

  test("a waiting row allows once from the list, or opens the session (plans/05 5.5)", async () => {
    const app = await openPhone();
    await app.waitFor("id:session-allow-ses_2");
    expect(app.shows("id:session-open-ses_2")).toBe(true);
    expect(app.shows("id:phone-tab-sessions-badge")).toBe(true);
    await app.press("id:session-allow-ses_2");
    await app.waitUntil(() => posts(app, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(app, "/permission/perm_1")[0].body as string)).toEqual({ reply: "allow" });
    const second = await openPhone();
    await second.press("id:session-open-ses_2");
    await second.waitFor("id:phone-approval");
  });

  test("the project switcher filters the list and keeps the choice for new sessions", async () => {
    const app = await openPhone();
    await app.press("id:project-selector");
    await app.waitFor("id:project-sheet");
    await app.press("id:project-prj_2");
    await app.waitFor("id:project-sheet", { state: "gone" });
    expect(app.shows("id:session-ses_4")).toBe(true);
    expect(app.shows("id:session-ses_1")).toBe(false);
    expect(app.texts()).toContain("other");
    await app.press("id:project-selector");
    await app.press("id:project-all");
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:session-ses_4")).toBe(true);
  });

  test("long press opens the row menu: rename and archive", async () => {
    const app = await openPhone();
    await app.longPress("id:session-ses_1");
    await app.waitFor("id:row-sheet");
    await app.press("id:row-rename");
    await app.type("id:row-rename-input", " v2");
    await app.press("id:row-rename-save");
    await app.waitUntil(() => app.requests().some((r: any) => r.method === "PATCH" && r.url.endsWith("/ses_1")));
    expect(JSON.parse(app.requests().find((r: any) => r.method === "PATCH")!.body as string).title).toContain("v2");
    await app.longPress("id:session-ses_3");
    await app.press("id:row-archive");
    await app.waitUntil(() => posts(app, "/ses_3/archive").length === 1);
  });

  test("long-pressing Send in a running turn offers Queue or Interrupt and send", async () => {
    const app = await openPhone({ "POST */api/session/ses_3/prompt*": { body: {} } });
    await app.press("id:session-ses_3");
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "stop and do this");
    await app.longPress("id:send");
    await app.waitFor("id:send-sheet");
    expect(app.shows("id:send-queue")).toBe(true);
    await app.press("id:send-interrupt");
    await app.waitUntil(() => posts(app, "/ses_3/prompt").length === 1);
    expect(JSON.parse(posts(app, "/ses_3/prompt")[0].body as string).mode).toBe("interrupt");
  });

  test("the left-edge swipe goes back to Sessions, with no drawer", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:phone-edge-swipe")).toBe(true);
    await app.drag("id:phone-edge-swipe", { x: 120, y: 0 });
    await app.waitFor("id:phone-sessions");
    expect(app.shows("id:phone-drawer")).toBe(false);
    expect(app.shows("id:phone-conversation")).toBe(false);
  });

  test("the root tabs are ordered Sessions, Manager, Settings, and a conversation hides the bar", async () => {
    const app = await openPhone();
    const texts = app.texts();
    expect(texts.indexOf("Sessions")).toBeGreaterThanOrEqual(0);
    expect(texts.indexOf("Sessions")).toBeLessThan(texts.indexOf("Manager"));
    expect(texts.indexOf("Manager")).toBeLessThan(texts.indexOf("Settings"));
    expect(app.count("id:phone-tabs")).toBe(1);
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-conversation");
    // The conversation is one page: no root tab bar, and Back to Sessions is its way out.
    expect(app.count("id:phone-tabs")).toBe(0);
    for (const tab of ["sessions", "manager", "settings"]) expect(app.shows(`id:phone-tab-${tab}`)).toBe(false);
    expect(app.shows("id:phone-back")).toBe(true);
    expect(app.texts()).not.toContain("Sessions");
    expect(app.shows("id:phone-drawer-button")).toBe(false);
  });

  test("Back returns to Sessions with the tab bar, and reopening the session keeps its draft", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "unfinished message");
    await app.press("id:phone-back");
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:phone-tabs")).toBe(true);
    expect(app.shows("id:phone-back")).toBe(false);
    expect(app.shows("id:composer-input")).toBe(false);
    // Manager and Settings are root tabs again, and the session list is one tap away from a conversation.
    await app.press("id:phone-tab-manager");
    await app.waitFor("id:phone-tab-title");
    await app.press("id:phone-tab-sessions");
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.value("id:composer-input")).toBe("unfinished message");
    expect(app.shows("id:phone-tabs")).toBe(false);
  });

  test("with home-indicator safe areas, the conversation keeps Back and the composer, and the bar returns on Back", async () => {
    const app = await openPhone({}, {}, { safeArea: { top: 47, right: 0, bottom: 34, left: 0 } });
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:phone-back")).toBe(true);
    expect(app.count("id:phone-tabs")).toBe(0);
    await app.press("id:phone-back");
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:phone-tabs")).toBe(true);
  });

  test("the header shows project and status, and its menu reaches rename", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-session-title");
    expect(app.shows("id:phone-session-project")).toBe(true);
    expect(app.shows("id:phone-session-status")).toBe(true);
    await app.press("id:phone-session-menu");
    await app.waitFor("id:row-sheet");
    expect(app.shows("id:row-rename")).toBe(true);
  });

  test("the phone composer is a compact two-row card with no bottom status bar", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:composer-surface")).toBe(true);
    expect(app.shows("id:phone-composer-meta")).toBe(true);
    expect(app.shows("id:composer-agent")).toBe(true);
    expect(app.shows("id:composer-model")).toBe(true);
    expect(app.shows("id:dictation-start")).toBe(true);
    expect(app.shows("id:send")).toBe(true);
    // The bottom Working row and activity bar belong to desktop only now.
    expect(app.shows("id:working-row")).toBe(false);
    expect(app.shows("id:location-strip")).toBe(false);
    expect(app.shows("id:composer-bottom-edge")).toBe(false);
  });

  test("the header shows live Working seconds, and the phone keeps no bottom working row or activity bar", async () => {
    const start = 1_700_000_000_000;
    const started = start / 1000 - 11;
    const busySessions = sessions.map((s) => s.id === "ses_1" ? { ...s, status: "busy" } : s);
    const snapshot = {
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects, sessions: busySessions, running: [{ id: "trn_1", session_id: "ses_1" }], approvals: [approval], questions: [], selected: null,
    };
    const app = await openPhone({}, {
      "GET */api/snapshot*": { body: snapshot },
      "GET */api/session/ses_1": { body: { ...sessions[0], status: "busy" } },
      "GET */api/session/ses_1/message*": { body: [{ id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "Go" }], created: started }] },
    }, { clock: { time: start } });
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-session-status");
    expect(app.shows("id:phone-session-timer")).toBe(true);
    expect(app.texts()).toContain("Working");
    expect(app.texts()).toContain("11s");
    expect(app.shows("id:working-row")).toBe(false);
    expect(app.shows("id:location-strip")).toBe(false);
    expect(app.shows("id:composer-bottom-edge")).toBe(false);
    // The compact composer keeps its selectors while busy; an empty draft offers Stop (plans/05 5.5).
    expect(app.shows("id:phone-composer-meta")).toBe(true);
    expect(app.shows("id:composer-agent")).toBe(true);
    expect(app.shows("id:composer-model")).toBe(true);
    expect(app.shows("id:stop")).toBe(true);
    await app.clock!.fastForward(1200);
    expect(app.texts()).toContain("12s");
  });

  test("phone prose uses the 16 px UI scale while fenced code keeps its monospace scale", async () => {
    const messages = [
      { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: "Plain user line" }], created: 1 },
      { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "Assistant paragraph for the phone.\n\n```bash\necho code-line\n```" }], created: 2 },
    ];
    const app = await openPhone({}, { "GET */api/session/ses_1/message*": { body: messages } });
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-assistant");
    await app.waitFor("id:code-fence");
    expect(app.spanStyle("Plain user line").fontSize).toBe(16);
    expect(app.spanStyle("Assistant paragraph for the phone.").fontSize).toBe(16);
    expect(app.spanStyle("echo code-line").fontSize).toBe(14);
  });

  test("the phone keeps both selectors usable with long names", async () => {
    const long = { ...sessions[0], agent: "orchestrate-with-a-very-long-agent-name", model: "openai/gpt-6.1-terra-ultra-long-model-name" };
    const app = await openPhone({}, {
      "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects, sessions: [long, ...sessions.slice(1)], running: [], approvals: [approval], questions: [], selected: null } },
      "GET */api/session/ses_1": { body: long },
    });
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:phone-composer-meta")).toBe(true);
    expect(app.shows("id:composer-agent")).toBe(true);
    expect(app.shows("id:composer-model")).toBe(true);
  });

  test("repeated rapid Send taps make one prompt, and a later distinct message still sends", async () => {
    const app = await openPhone({}, { "POST */api/session/ses_1/prompt*": () => ({ body: {}, delay: 600 }) });
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "first message");
    await app.press("id:send");
    await app.press("id:send");
    await app.press("id:send");
    await app.press("id:send");
    expect(posts(app, "/ses_1/prompt").length).toBe(1);
    await app.waitUntil(() => posts(app, "/ses_1/prompt").length === 1);
    await app.wait(700);
    expect(app.value("id:composer-input")).toBe("");
    await app.type("id:composer-input", "a genuinely later message");
    await app.press("id:send");
    await app.waitUntil(() => posts(app, "/ses_1/prompt").length === 2);
  });

  test("a failed send keeps the draft, releases the guard, and a retry can send", async () => {
    let attempts = 0;
    const app = await openPhone({}, {
      "POST */api/session/ses_1/prompt*": () => {
        attempts++;
        return attempts === 1 ? { status: 503, body: { error: "temporary" }, delay: 200 } : { body: {}, delay: 100 };
      },
    });
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    await app.type("id:composer-input", "keep this draft");
    await app.press("id:send");
    await app.waitUntil(() => posts(app, "/ses_1/prompt").length === 1);
    await app.wait(300);
    expect(app.value("id:composer-input")).toBe("keep this draft");
    await app.press("id:send");
    await app.waitUntil(() => posts(app, "/ses_1/prompt").length === 2);
    await app.wait(150);
    expect(app.value("id:composer-input")).toBe("");
  });
});

describe("phone user messages", () => {
  const reply = { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: "A reply" }], created: 2 };

  test("hides the message time, number, copy and fold controls until a long press", async () => {
    const app = await openPhone({}, { "GET */api/session/ses_1/message*": { body: [message("m1", 1, "user", [{ type: "text", text: "Short message" }]), reply] } });
    await app.press("id:session-ses_1");
    await app.waitFor("id:msg-user");
    expect(app.shows("id:copy-user-1")).toBe(false);
    expect(app.shows("id:fold-user-1")).toBe(false);
    expect(app.shows("id:user-details-1")).toBe(false);
    expect(app.texts().join(" ")).not.toContain("#1");
    await app.longPress("id:msg-user");
    await app.waitFor("id:user-details-1");
    expect(app.shows("id:copy-user-1")).toBe(true);
    expect(app.shows("id:fold-user-1")).toBe(true);
    expect(app.texts().join(" ")).toContain("#1");
    await app.longPress("id:msg-user");
    await app.waitFor("id:user-details-1", { state: "gone" });
    await app.longPress("id:msg-user");
    await app.waitFor("id:user-details-1");
    await app.press("id:copy-user-1");
    expect(app.clipboard()).toBe("Short message");
    await app.press("id:fold-user-1");
    await app.waitFor("id:msg-user-folded-1");
  });

  test("keeps a long message readable with Show full and shows no sticky user bar", async () => {
    const long = Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join("\n");
    const prose = Array.from({ length: 30 }, (_, i) => `Reply paragraph ${i + 1}.`).join("\n\n");
    const app = await openPhone({}, {
      "GET */api/session/ses_1/message*": { body: [
        { id: "m1", session_id: "ses_1", seq: 1, role: "user", parts: [{ type: "text", text: long }], created: 1 },
        { id: "m2", session_id: "ses_1", seq: 2, role: "assistant", parts: [{ type: "text", text: prose }], created: 2 },
      ] },
    });
    await app.press("id:session-ses_1");
    await app.waitFor("id:user-full-1");
    await app.press("id:user-full-1");
    await app.waitUntil(() => app.texts().join(" ").includes("line 60"));
    await app.scroll("id:timeline", -400);
    await app.waitFor("id:jump-latest");
    expect(app.shows("id:sticky-user")).toBe(false);
    expect(app.shows("id:sticky-user-button")).toBe(false);
  });

  test("keeps an approval and the compact composer usable inside safe areas", async () => {
    const app = await openPhone({}, {}, { safeArea: { top: 47, right: 0, bottom: 34, left: 0 } });
    await app.press("id:session-ses_2");
    await app.waitFor("id:phone-approval");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:allow-once")).toBe(true);
    expect(app.shows("id:deny")).toBe(true);
    expect(app.shows("id:phone-composer-meta")).toBe(true);
    expect(app.shows("id:send")).toBe(true);
    expect(app.shows("id:working-row")).toBe(false);
    expect(app.shows("id:location-strip")).toBe(false);
    expect(app.shows("id:composer-bottom-edge")).toBe(false);
  });
});

describe("phone ignores the tab layout (plan 45)", () => {
  test("a session view has no group strip, tabs, splits or saved layout, and Back returns to the three tabs", async () => {
    const app = await openPhone();
    // The three main tabs belong to the root screens.
    for (const tab of ["sessions", "manager", "settings"]) expect(app.shows(`id:phone-tab-${tab}`)).toBe(true);
    expect(app.count("id:phone-tabs")).toBe(1);
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-conversation");
    expect(app.count("id:phone-tabs")).toBe(0);
    await app.press("id:phone-back");
    await app.waitFor("id:session-ses_1");
    for (const tab of ["sessions", "manager", "settings"]) expect(app.shows(`id:phone-tab-${tab}`)).toBe(true);
    await app.press("id:phone-tab-settings");
    await app.waitFor("id:connection-card");
    await app.press("id:phone-tab-sessions");
    await app.waitFor("id:session-ses_1");
    await app.press("id:phone-tab-manager");
    await app.waitFor("id:phone-tab-title");
    await app.press("id:phone-tab-sessions");
    await app.waitFor("id:session-ses_1");
    for (const id of ["groups", "group-strip-g1", "group-menu-g1", "session-tab-ses_1", "new-session-tab", "terminal-panel", "file-panel"]) expect(app.shows(`id:${id}`)).toBe(false);
    expect(Object.keys(await app.storage()).some((k) => k.startsWith("nexus.layout."))).toBe(false);
  });
});

describe("phone tool summaries (plans/05 5.1, user 2026-11-23)", () => {
  test("keeps each tool group in order as one compact summary, with no members or tool detail", async () => {
    const messages = [
      message("m1", 1, "user", [{ type: "text", text: "First" }]),
      message("m2", 2, "assistant", [call("a1"), call("a2")]),
      message("m3", 3, "assistant", [{ type: "text", text: "Between" }]),
      message("m4", 4, "assistant", [call("a3", "read", { path: "secret/a.txt" })]),
    ];
    const app = await openPhone({}, { "GET */api/session/ses_1/message*": { body: messages } });
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-activity");
    const texts = app.texts();
    const at = (text: string) => texts.indexOf(text);
    expect(at("First") >= 0 && at("2 commands") > at("First")).toBe(true);
    expect(at("Between") > at("2 commands") && at("1 read") > at("Between")).toBe(true);
    expect(app.count("id:phone-activity")).toBe(2);
    expect(app.count("id:phone-activity-done")).toBe(2);
    // Always collapsed and non-expandable: no member row, no tool row (so no command, path or diff), no earlier-items line.
    expect(app.count("id:activity-member")).toBe(0);
    expect(app.count("id:tool-row-a1")).toBe(0);
    expect(texts.join(" ").includes("echo a1") || texts.join(" ").includes("secret/a.txt")).toBe(false);
  });

  test("a running group counts every member, including the ones it truncates", async () => {
    const messages = [
      message("m1", 1, "user", [{ type: "text", text: "Go" }]),
      message("m2", 2, "assistant", [{ type: "reasoning", summary: "Splitting the rebuild into batches." }, ...Array.from({ length: 7 }, (_, i) => call(`a${i + 1}`))]),
    ];
    const snapshot = {
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects, sessions: sessions.map((s) => s.id === "ses_1" ? { ...s, status: "busy" } : s),
      running: [{ id: "trn_1", session_id: "ses_1" }], approvals: [approval], questions: [], selected: null,
    };
    const app = await openPhone({}, {
      "GET */api/snapshot*": { body: snapshot },
      "GET */api/session/ses_1": { body: { ...sessions[0], status: "busy" } },
      "GET */api/session/ses_1/message*": { body: messages },
    });
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-activity-running");
    expect(app.texts()).toContain("1 thought, 7 commands");
    expect(app.texts()).not.toContain("1 thought, 5 commands");
    expect(app.count("id:activity-member")).toBe(0);
    expect(app.texts().join(" ").includes("earlier")).toBe(false);
  });

  test("a waiting group says so and leaves its approval card usable", async () => {
    const messages = [message("m1", 1, "assistant", [call("c1", "bash", { command: "rm -rf build" }), call("c2")])];
    const app = await openPhone({}, { "GET */api/session/ses_2/message*": { body: messages } });
    await app.press("id:session-ses_2");
    await app.waitFor("id:phone-activity-waiting");
    await app.waitFor("id:phone-approval");
    expect(app.texts()).toContain("2 commands");
    await app.press("id:allow-once");
    await app.waitUntil(() => posts(app, "/permission/perm_1").length === 1);
  });
});

describe("phone subagents (user, 2026-11-24)", () => {
  const child = { ...base, id: "ses_c", project_id: "prj_1", directory: "/work/demo", title: "Map the store", created: 2, updated: 3, parent_id: "ses_1", parent_call_id: "c9", agent: "explore", status: "busy" };
  const withChild = () => {
    const all = [...sessions, child];
    return {
      "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects, sessions: all, running: ["ses_c"], approvals: [approval], questions: [], selected: null } },
      "GET */api/session/ses_1/message*": { body: [message("m1", 1, "assistant", [{ type: "text", text: "I will ask a subagent." }, call("c9", "session", { agent: "explore", title: "Map the store", prompt: "Map the store module" })])] },
      "GET */api/session/ses_c/message*": { body: [message("mc", 2, "assistant", [{ type: "text", text: "Reading the store." }])] },
      "GET */api/session/ses_c/snapshot": { body: null },
      "GET */api/session/ses_c/context": { body: { used: 0, window: null, exact: false } },
      "GET */api/session/ses_c/turn*": { body: empty },
      "GET */api/session/ses_c/worktrees": { body: { worktrees: [] } },
      "GET */api/session/ses_c/input": { body: { held: false, inputs: [] } },
      "GET */api/session/ses_c": { body: child },
    };
  };

  test("a parent's row counts its subagents, and its timeline shows one card per subagent that opens it", async () => {
    const app = await openPhone({}, withChild());
    await app.waitFor("id:session-subagents-ses_1");
    expect(app.texts()).toContain("1 subagent · 1 running");
    // A subagent is not a root row of its own.
    expect(app.shows("id:session-ses_c")).toBe(false);
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-child");
    expect(app.texts()).toContain("EXPLORE");
    expect(app.texts()).toContain("Map the store");
    expect(app.shows("id:phone-child-running")).toBe(true);
    await app.press("id:child-card");
    await app.waitFor("id:phone-session-parent");
    expect(app.texts()).toContain("Subagent of Migration plan");
    expect(app.texts()).toContain("Reading the store.");
  });

  test("Back and the edge swipe from a subagent return to its parent conversation, then to Sessions", async () => {
    const app = await openPhone({}, withChild());
    await app.press("id:session-ses_1");
    await app.waitFor("id:child-card");
    await app.press("id:child-card");
    await app.waitFor("id:phone-session-parent");
    await app.press("id:phone-back");
    await app.waitFor("id:phone-child");
    expect(app.shows("id:phone-session-parent")).toBe(false);
    expect(app.shows("id:phone-conversation")).toBe(true);
    await app.press("id:child-card");
    await app.waitFor("id:phone-session-parent");
    await app.drag("id:phone-edge-swipe", { x: 120, y: 0 });
    await app.waitFor("id:phone-child");
    await app.press("id:phone-back");
    await app.waitFor("id:phone-sessions");
  });
});

describe("phone Manager tab", () => {
  test("opening the tab shows the panel without starting the Manager or its voice (user, 2026-11-24)", async () => {
    const app = await openPhone();
    await app.press("id:phone-tab-manager");
    await app.waitFor("id:manager-field");
    expect(app.shows("id:manager-input-row")).toBe(true);
    await app.clock?.fastForward(2000);
    expect(app.requests().filter((r: any) => r.method === "POST" && r.url.includes("/api/manager")).length).toBe(0);
  });
});

describe("phone approval card", () => {
  test("Allow once, Always allow and Deny with a note", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_2");
    await app.waitFor("id:phone-approval");
    expect(app.texts().join(" ")).toContain("rm -rf build");
    await app.press("id:allow-once");
    await app.waitUntil(() => posts(app, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(app, "/permission/perm_1")[0].body as string)).toEqual({ reply: "allow" });
  });

  test("Always allow grants the session; Deny sends the note", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_2");
    await app.waitFor("id:phone-approval");
    await app.press("id:allow-session");
    await app.waitUntil(() => posts(app, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(app, "/permission/perm_1")[0].body as string)).toEqual({ reply: "allow", scope: "session" });
    const second = await openPhone();
    await second.press("id:session-ses_2");
    // The note is the composer's text (plans/05 5.5): Deny takes it along.
    await second.waitFor("id:phone-approval");
    await second.type("id:composer-input", "use make instead");
    await second.press("id:deny");
    await second.waitUntil(() => posts(second, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(second, "/permission/perm_1")[0].body as string)).toEqual({ reply: "deny", note: "use make instead" });
  });

  test("sending from the composer while an approval waits denies with that text", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_2");
    await app.waitFor("id:phone-approval");
    await app.type("id:composer-input", "run the tests first");
    await app.press("id:send");
    await app.waitUntil(() => posts(app, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(app, "/permission/perm_1")[0].body as string)).toEqual({ reply: "deny", note: "run the tests first" });
    expect(posts(app, "/prompt").length).toBe(0);
  });
});

describe("phone settings and pairing", () => {
  test("Settings has a connection card and drill-in pages", async () => {
    const app = await openPhone();
    await app.press("id:phone-tab-settings");
    await app.waitFor("id:connection-card");
    expect(app.texts()).toContain("nexus.invalid");
    expect(app.shows("id:core-transport")).toBe(true);
    await app.waitUntil(() => app.requests().filter((r: any) => r.url.endsWith("/api/info")).length >= 1);
    await app.press("id:phone-setting-appearance");
    await app.waitFor("id:phone-settings-page-appearance");
    expect(app.shows("id:phone-settings-back")).toBe(true);
    await app.press("id:phone-settings-back");
    await app.waitFor("id:connection-card");
    expect(app.shows("id:phone-setting-providers")).toBe(true);
    expect(app.shows("id:phone-setting-about")).toBe(true);
  });

  test("the pairing screen explains the setup and names Local Network when a LAN Core is unreachable", async () => {
    const app = await openPhone({ "GET http://192.168.1.20:4747/api/info": { status: 503, body: { error: "down" } } });
    await app.press("id:phone-tab-settings");
    await app.press("id:core-change");
    await app.waitFor("id:phone-connect");
    expect(app.shows("id:connect-step-3")).toBe(true);
    await app.type("id:connect-link", "nexus://pair?url=http%3A%2F%2F192.168.1.20%3A4747&name=mac");
    await app.press("id:connect-submit");
    await app.waitFor("id:connect-error", { timeout: 12000 });
    expect(app.texts().join(" ")).toContain("Local Network");
    expect(app.shows("id:connect-retry")).toBe(true);
  });
});
