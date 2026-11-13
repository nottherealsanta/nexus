// The phone redesign of plans/05 section 5.2: sessions sections, the drawer, the row menu, the approval card, the Settings
// connection card and the pairing screen. Run without `--target` so the app runs in the phone idiom (390x844, "ios").
import { describe, expect, open, test } from "nsh/test";

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

async function openPhone(extra: Record<string, any> = {}) {
  const app = await open("./App.tsx", { network: network(extra), width: 390, height: 844, platform: "ios", storage: { "nexus.core": "http://nexus.invalid:4747" } });
  await app.waitFor("id:splash", { state: "gone" });
  await app.waitFor("id:session-ses_1");
  return app;
}
const posts = (app: any, suffix: string) => app.requests().filter((r: any) => r.method === "POST" && r.url.endsWith(suffix));

describe("phone sessions (plans/05 5.2)", () => {
  test("lists Needs you with its count, Running, then Recent, and marks the waiting row", async () => {
    const app = await openPhone();
    await app.waitFor("id:sessions-section-needs");
    expect(app.shows("id:sessions-section-running")).toBe(true);
    expect(app.shows("id:sessions-section-recent")).toBe(true);
    expect(app.shows("id:session-needs-ses_2")).toBe(true);
    expect(app.texts()).toContain("Needs your input");
    expect(app.shows("id:session-needs-ses_1")).toBe(false);
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

  test("the drawer opens from the header button, lists sessions and switches", async () => {
    const app = await openPhone();
    await app.press("id:session-ses_1");
    await app.waitFor("id:composer-input");
    expect(app.shows("id:phone-drawer")).toBe(false);
    expect(app.shows("id:phone-edge-swipe")).toBe(true);
    await app.press("id:phone-drawer-button");
    await app.waitFor("id:phone-drawer");
    expect(app.shows("id:phone-drawer-sessions")).toBe(true);
    await app.press("id:session-ses_3");
    await app.waitFor("id:phone-drawer", { state: "gone" });
    expect(app.texts()).toContain("Build the thing");
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
});

describe("phone ignores the tab layout (plan 45)", () => {
  test("a session view has no group strip, tabs, splits or saved layout, and the three tabs stay", async () => {
    const app = await openPhone();
    // The three tabs on the list screen; the conversation is a page over them (its drawer switches sessions).
    for (const tab of ["sessions", "manager", "settings"]) expect(app.shows(`id:phone-tab-${tab}`)).toBe(true);
    expect(app.count("id:phone-tabs")).toBe(1);
    await app.press("id:session-ses_1");
    await app.waitFor("id:phone-conversation");
    for (const id of ["groups", "group-strip-g1", "group-menu-g1", "session-tab-ses_1", "new-session-tab", "terminal-panel", "file-panel"]) expect(app.shows(`id:${id}`)).toBe(false);
    expect(Object.keys(await app.storage()).some((k) => k.startsWith("nexus.layout."))).toBe(false);
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
    await second.waitFor("id:deny-note");
    await second.type("id:deny-note", "use make instead");
    await second.press("id:deny");
    await second.waitUntil(() => posts(second, "/permission/perm_1").length === 1);
    expect(JSON.parse(posts(second, "/permission/perm_1")[0].body as string)).toEqual({ reply: "deny", note: "use make instead" });
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
