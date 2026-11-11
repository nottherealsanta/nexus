// DESIGN_PLAN I6 (D03, D12): sessions sidebar rows and right sidebar, on the real runtime.
import { describe, expect, open, test } from "nsh/test";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = (id: string, title: string, extra: object = {}) => ({
  id, project_id: "prj_1", directory: "/work/demo", title, model: "", permission: "ask", created: 1, updated: 2,
  effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]", ...extra,
});
const snapshot = (sessions: object[]) => ({
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions, running: [], approvals: [], questions: [], selected: null,
});
const quiet = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
function network(sessions: object[], extra: Record<string, unknown> = {}) {
  return {
    // Specific stubs first: the first matching key answers.
    ...extra,
    "GET */api/session/ses_1/message*": { body: [] },
    "GET */api/session/ses_1/snapshot": { body: null },
    "GET */api/session/ses_1/turn*": { body: quiet },
    "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_1": { body: sessions[0] },
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/snapshot*": { body: snapshot(sessions) },
    "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/project/prj_1/worktrees": { body: { worktrees: [{ path: "/work/demo", branch: "trunk", main: true }] } },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
  };
}

describe("sessions sidebar (I6)", () => {
  test("the sidebar lists the current project: the rail switches it and search stays inside it", async () => {
    const local = session("ses_1", "Find local");
    const remote = session("ses_2", "Find remote", { project_id: "prj_2" });
    const data = snapshot([local, remote]);
    data.projects.push({ id: "prj_2", path: "/work/other", name: "other", created: 1, updated: 2, revision: 1 });
    const app = await open("./App.tsx", { network: { ...network([local, remote]), "GET */api/snapshot*": { body: data } }, width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    expect(app.shows("id:chip-all-projects")).toBe(false);
    expect(app.shows("id:session-ses_2")).toBe(false);
    await app.press("id:session-search");
    await app.waitFor("id:session-switcher-input");
    await app.keyboard.press("r");
    await app.waitUntil(() => app.value("id:session-switcher-input") === "r");
    await app.keyboard.press("escape");
    await app.press("id:rail-project-prj_2");
    await app.waitFor("id:session-ses_2");
    expect(app.shows("id:session-ses_1")).toBe(false);
  });

  test("the row shows its branch and child count, with New session in the footer", async () => {
    const parent = session("ses_1", "Refactor loader", { agent: "orchestrate" });
    const child = session("ses_2", "Child task", { parent_id: "ses_1" });
    const app = await open("./App.tsx", { network: network([parent, child]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    expect(app.shows("id:session-group")).toBe(false);
    expect(app.shows("id:new-session-row")).toBe(true);
    await app.waitFor("id:session-place-ses_1");
    await app.waitFor("text:⌥ trunk");
    expect(app.texts().includes("orchestrate")).toBe(false);
    expect(app.texts().includes("1 ↳")).toBe(true);
    expect(app.shows("id:session-ses_2")).toBe(false); // children live under their parent
  });

  test("the rail stays with the sidebar open or closed, with one counter in each corner", async () => {
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:rail-count-prj_1-idle");
    expect(app.shows("id:left-sidebar") && app.shows("id:session-rail")).toBe(true);
    await app.press("id:toggle-left");
    expect(app.shows("id:left-sidebar")).toBe(false);
    expect(app.shows("id:session-rail") && app.shows("id:rail-count-prj_1-idle") && app.shows("id:rail-settings")).toBe(true);
  });

  test("the collapsed project rail shows each count on one line of a two-row card", async () => {
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:toggle-left");
    await app.waitFor("id:rail-project-prj_1");
    await app.hover("id:rail-project-prj_1");
    await app.waitFor("id:rail-popup-prj_1");
    expect(app.shows("id:rail-popup-prj_1-above")).toBe(true);
    expect(app.shows("id:rail-popup-prj_1-below")).toBe(true);
    const shown = app.texts();
    for (const text of ["0 attention", "0 unread", "0 running", "1 idle"]) expect(shown.includes(text)).toBe(true);
  });

  test("keyboard focus on a rail project shows its card too", async () => {
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:toggle-left");
    await app.waitFor("id:rail-project-prj_1");
    for (let i = 0; i < 40 && !app.shows("id:rail-popup-prj_1"); i++) await app.keyboard.press("tab");
    expect(app.shows("id:rail-popup-prj_1")).toBe(true);
  });

  test("the hover ⋯ button opens the same row menu as a right click", async () => {
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:session-ses_1");
    await app.hover("id:session-ses_1");
    expect(app.shows("id:row-menu-ses_1")).toBe(true);
    await app.press("id:row-menu-ses_1");
    await app.waitFor("id:row-action-session.rename");
    expect(app.shows("id:row-action-session.delete")).toBe(true);
  });
});

describe("right sidebar (I6)", () => {
  test("Session info and Context sections, with the meter only when usage is known", async () => {
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")]), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:toggle-right");
    await app.waitFor("id:right-sidebar");
    await app.waitFor("id:right-section-session-info");
    expect(app.texts().includes("Context")).toBe(true);
    expect(app.texts().includes("not reported")).toBe(true);
    expect(app.shows("id:session-context")).toBe(false);
  });

  test("Git shows the branch, groups with counts and one row per file", async () => {
    const git = { branch: "main", ahead: 1, behind: 0, changes: [
      { path: "src/staged.py", status: "M ", staged: true, unstaged: false, added: 3, removed: 1 },
      { path: "README.md", status: " M", staged: false, unstaged: true, added: 2, removed: 0 },
      { path: "tests/new.py", status: "??", staged: false, unstaged: true, added: 0, removed: 0 },
    ] };
    const app = await open("./App.tsx", { network: network([session("ses_1", "Refactor loader")], { "GET */api/session/ses_1/git": { body: git } }), width: 1440, height: 900, platform: "web" });
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.press("id:toggle-right");
    await app.waitFor("id:git-group-staged");
    expect(app.count("id:git-row")).toBe(3);
    for (const t of ["⌥ main", "↑1", "Staged", "Unstaged", "Untracked", "src/staged.py", "+3", "−1"]) expect(app.texts().includes(t)).toBe(true);
  });
});
