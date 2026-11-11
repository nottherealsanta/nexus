// Plans 29-32 in NSH: archive closes tabs from any source, /archive and /search, the one-step
// question card and locked composer, queued-row actions, and the worktree icon + details modal.
import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/session-controls.json";
import { Model } from "./src/core/reduce";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const sse = (...frames: string[]) => frames.join("");
const event = (id: number, type: string, session_id: string | null, data: unknown) =>
  `event: message\ndata: ${JSON.stringify({ id, time: id, type, session_id, data })}\n\n`;

const session = {
  id: "ses_1", project_id: "prj_1", directory: scenario.directory, title: "Migration plan", model: "scripted/m",
  permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0,
  revision: 1, disabled_tools: "[]",
};
const second = { ...session, id: "ses_2", directory: "/work/demo", title: "Chores", created: 2, updated: 3 };
const child = { ...session, id: "ses_child", title: "Child work", parent_id: "ses_1", created: 3, updated: 4 };
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const worktrees = {
  repository: "/work/demo",
  main: scenario.worktree_info.main_path,
  session_directory: scenario.worktree_info.path,
  worktrees: scenario.worktrees,
};
const questionWith = (extra: Record<string, unknown>) => ({
  ...scenario.question,
  questions: scenario.question.questions.map((q) => ({ ...q, ...extra })),
});

interface Flags {
  question?: "none" | "asked" | "replied";
  archive?: boolean;
  archivedSnapshot?: boolean;
  overflow?: boolean;
  snapshots?: number;
  inputs?: unknown[];
  questions?: unknown[];
  answerFailures?: number;
  archiveError?: boolean;
  directory?: string;
}
function network(flags: Flags = {}) {
  const active = flags.directory ? { ...session, directory: flags.directory } : session;
  const sseFrames = () => {
    const frames: string[] = [];
    if (flags.question === "asked") frames.push(event(2, "question.asked", "ses_1", scenario.question));
    if (flags.question === "replied") frames.push(event(3, "question.replied", "ses_1", { id: scenario.question.id }));
    if (flags.archive) frames.push(event(2, "session.archived", "ses_1", {
      root: "ses_1", sessions: ["ses_1", "ses_child"], archived: true,
      revisions: { ses_1: 2, ses_child: 2 },
    }));
    return frames.join("");
  };
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/snapshot*": () => {
      flags.snapshots = (flags.snapshots ?? 0) + 1;
      return {
        body: {
          watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
          projects: [project],
          sessions: [
            flags.archivedSnapshot ? { ...active, archived: 1, revision: 2 } : active,
            second,
            flags.archivedSnapshot ? { ...child, archived: 1, revision: 2 } : child,
          ],
          running: [], approvals: [], questions: flags.questions ?? [], selected: null,
        },
      };
    },
    "GET */api/event*": () => {
      const overflow = !!flags.overflow;
      flags.overflow = false;
      return { body: sse(connected, overflow ? "event: bus.overflow\ndata: {}\n\n" : "", sseFrames()), headers: { "content-type": "text/event-stream" } };
    },
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
    "GET */api/session/ses_1/worktrees": { body: flags.directory ? {
      ...worktrees, session_directory: flags.directory,
      worktrees: [scenario.worktrees[0], { ...scenario.worktrees[1], path: flags.directory }],
    } : worktrees },
    "GET */api/session/ses_1/input": () => ({ body: { held: true, inputs: flags.inputs ?? [] } }),
    "GET */api/session/ses_2/message*": { body: [] },
    "GET */api/session/ses_2/snapshot": { body: null },
    "GET */api/session/ses_2/context": { body: { used: 0, window: null, exact: false } },
    "GET */api/session/ses_2/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
    "GET */api/session/ses_2/worktrees": { body: { worktrees: [] } },
    "GET */api/session/ses_2/input": { body: { held: false, inputs: [] } },
    "POST */api/session/ses_1/archive": () => {
      if (flags.archiveError) return { status: 409, body: { error: "A background job is running" } };
      flags.archive = true; return { body: { ...active, archived: true } };
    },
    "POST */api/session/ses_1/question/qst_controls": () => {
      if (flags.answerFailures) { flags.answerFailures--; return { status: 503, body: { error: "Temporary answer failure" } }; }
      return { body: {}, delay: 300 };
    },
    "POST */api/session/ses_1/question/qst_two": { body: {}, delay: 200 },
    "POST */api/session/ses_1/input/*/move": { body: {} },
    "DELETE */api/session/ses_1/input/*": (r: any) => {
      const id = r.url.split("/").pop();
      const input = (flags.inputs as any[] | undefined)?.find((i) => i.id === id);
      flags.inputs = (flags.inputs as any[] | undefined)?.filter((i) => i.id !== id) ?? [];
      return { body: input ?? { parts: [] } };
    },
    "GET */api/search*": { body: { hits: [] } },
    "GET */api/session/ses_1": () => ({ body: flags.archivedSnapshot ? { ...active, archived: 1, revision: 2 } : active }),
    "GET */api/session/ses_2": { body: second },
    "GET */api/session/ses_child": { body: child },
    "GET */api/session/*": { body: session },
  } as Record<string, any>;
}

async function openApp(flags: Flags, options: Record<string, unknown> = {}) {
  const app = await open("./App.tsx", { network: network(flags), width: 1280, height: 768, platform: "web", ...options });
  await app.waitFor("id:splash", { state: "gone" });
  return app;
}

async function openSession(app: any, id = "ses_1", title = "Migration plan") {
  await app.waitFor(`id:session-${id}`);
  await app.press(`id:session-${id}`);
  // The viewed tab's accessible name is its Rename action; the others keep Open.
  await app.waitFor(`label:/^Rename ${title},/`);
}

describe("archive closes its tab (plan 29)", () => {
  test("an archive event from another client closes the tab and focuses its neighbour", async () => {
    const flags: Flags = {};
    const app = await openApp(flags);
    await openSession(app, "ses_1");
    await openSession(app, "ses_2", "Chores");
    // Both roots stay open as tabs; bring the session that will be archived back to the front.
    await app.press("id:session-tab-ses_1");
    await app.waitFor("label:/^Rename Migration plan,/");
    flags.archive = true;
    await app.waitUntil(() => !app.shows("id:session-tab-ses_1"), { timeout: 8000 });
    expect(app.shows("id:session-tab-ses_2")).toBe(true);
    expect(app.requests().some((r) => r.method === "GET" && r.url.includes("/api/session/ses_2/message"))).toBe(true);
  });

  test("/arch completes to /archive and the archive command closes the tab on its event", async () => {
    const flags: Flags = {};
    const app = await openApp(flags);
    await openSession(app, "ses_1");
    await openSession(app, "ses_2", "Chores");
    await app.press("id:session-tab-ses_1");
    await app.waitFor("label:/^Rename Migration plan,/");
    await app.type("id:composer-input", "/arch");
    await app.waitFor("id:completion-session.archive");
    expect(app.texts().some((t) => t.includes("Archive session"))).toBe(true);
    await app.keyboard.press("tab");
    await app.waitUntil(() => app.value("id:composer-input") === "/archive ");
    await app.submit("id:composer-input");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/ses_1/archive")));
    await app.waitUntil(() => !app.shows("id:session-tab-ses_1"), { timeout: 8000 });
    expect(app.shows("id:session-tab-ses_2")).toBe(true);
  });

  test("the reducer records the archived root and its descendants", () => {
    const model = new Model();
    model.applySnapshot({
      watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
      projects: [project as any], sessions: [session as any, child as any], running: [], approvals: [], questions: [], selected: null,
    });
    model.applyEvent({ id: 1, time: 1, type: "session.archived", session_id: "ses_1", data: { sessions: ["ses_1"], revisions: {} } } as any);
    expect([...model.archivedTabs].sort()).toEqual(["ses_1", "ses_child"]);
    expect(model.sessions.get("ses_1")?.archived).toBe(true);

    // A reconnect snapshot seeds the closure set from its own archived rows and drops the pending event.
    model.applySnapshot({
      watermark: { generation: "log_t", head: 3, floor: 0, head_cursor: "log_t:3" },
      projects: [project as any], sessions: [{ ...session, archived: 1 } as any, child as any], running: [], approvals: [], questions: [], selected: null,
    });
    expect([...model.archivedTabs]).toEqual([]);
    expect([...model.archivedTabsSnapshot]).toEqual(["ses_1"]);
  });

  test("a reconnect snapshot closes an archived tab; an explicit reopen survives the next snapshot", async () => {
    const flags: Flags = {};
    const app = await openApp(flags);
    await openSession(app, "ses_1");
    await openSession(app, "ses_2", "Chores");
    await app.press("id:session-tab-ses_1");
    await app.waitFor("label:/^Rename Migration plan,/");
    flags.archivedSnapshot = true;
    flags.overflow = true;
    await app.waitUntil(() => !app.shows("id:session-tab-ses_1"), { timeout: 8000 });
    expect(app.shows("id:session-tab-ses_2")).toBe(true);

    await app.press("id:chip-archived");
    await app.waitFor("id:session-ses_1");
    await app.press("id:session-ses_1");
    await app.waitFor("id:session-tab-ses_1");
    const before = flags.snapshots ?? 0;
    flags.overflow = true;
    await app.waitUntil(() => (flags.snapshots ?? 0) > before, { timeout: 8000 });
    await app.wait(300);
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
  });
});

describe("/search opens the focused switcher (plan 29)", () => {
  test("the argument prefills it, typing filters at once and the field keeps focus", async () => {
    const app = await openApp({});
    await openSession(app, "ses_1");
    await app.type("id:composer-input", `/search ${scenario.search}`);
    await app.submit("id:composer-input");
    await app.waitFor("id:session-switcher-input");
    expect(app.value("id:session-switcher-input")).toBe(scenario.search);
    // No second click: the field already has focus, so a key press edits it.
    await app.keyboard.press("backspace");
    await app.waitUntil(() => app.value("id:session-switcher-input") === "migratio");
    await app.keyboard.press("n");
    await app.waitUntil(() => app.value("id:session-switcher-input") === "migration");
    await app.waitFor("id:switch-session-ses_1");
    expect(app.shows("id:switch-session-ses_2")).toBe(false);
  });
});

describe("questions (plan 30)", () => {
  test("a single choice submits at once and the composer is locked with the draft kept", async () => {
    const flags: Flags = { question: "none" };
    const app = await openApp(flags);
    await openSession(app, "ses_1");
    await app.type("id:composer-input", scenario.draft);
    flags.question = "asked";
    await app.waitFor("id:waiting-card", { timeout: 8000 });
    expect(app.value("id:composer-input")).toBe(scenario.draft);
    expect(app.enabled("id:composer-input")).toBe(false);
    expect(app.shows("text:Answer the question above")).toBe(true);
    expect(app.shows("id:submit-answers")).toBe(false);
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-question-lock-dark.png");

    // The single free-text answer line is focused, so a key press lands there, not in the composer.
    await app.keyboard.press("z");
    await app.waitUntil(() => app.value("id:question-free-approach") === "z");
    expect(app.value("id:composer-input")).toBe(scenario.draft);

    await app.press("label:Which approach should we use? Conservative migration");
    // A second press while the first POST is in flight must not duplicate it.
    await app.press("label:Which approach should we use? Direct migration");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length === 1);
    await app.wait(500);
    expect(app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length).toBe(1);
    const posts = app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/"));
    const body = JSON.parse(posts[0].body as string);
    expect(body.answers[0].selected).toEqual(["safe"]);
    expect(body.answers[0].text).toBe("z");

    flags.question = "replied";
    await app.waitUntil(() => !app.shows("id:waiting-card"), { timeout: 8000 });
    await app.waitUntil(() => app.enabled("id:composer-input"));
    expect(app.value("id:composer-input")).toBe(scenario.draft);
  });

  test("a free-text answer submits with Enter for a text-only question", async () => {
    const app = await openApp({ questions: [questionWith({ options: [], allow_free_text: true })] });
    await openSession(app, "ses_1");
    await app.waitFor("id:waiting-card");
    await app.keyboard.press("y");
    await app.waitUntil(() => app.value("id:question-free-approach") === "y");
    await app.keyboard.press("enter");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length === 1);
    const body = JSON.parse(app.requests().find((r) => r.method === "POST" && r.url.includes("/question/"))!.body as string);
    expect(body.answers[0]).toEqual({ question_id: "approach", selected: [], text: "y" });
  });

  test("a single multi-select needs one submit, with no review page", async () => {
    const app = await openApp({ questions: [questionWith({ multiple: true })] });
    await openSession(app, "ses_1");
    await app.waitFor("id:submit-answers");
    await app.press("label:Which approach should we use? Conservative migration");
    await app.press("label:Which approach should we use? Direct migration");
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/question/"))).toBe(false);
    await app.press("id:submit-answers");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length === 1);
    const body = JSON.parse(app.requests().find((r) => r.method === "POST" && r.url.includes("/question/"))!.body as string);
    expect(body.answers[0].selected.sort()).toEqual(["fast", "safe"]);
    expect(app.shows("id:waiting-card")).toBe(true);
  });
});

describe("two questions step through to a review", () => {
  const two = {
    ...scenario.question,
    id: "qst_two",
    questions: [
      { ...scenario.question.questions[0], id: "approach", question: "Which approach should we use?", multiple: false, allow_free_text: true },
      { id: "note", question: "Anything else for the release?", options: [], multiple: false, allow_free_text: true },
    ],
  };
  test("Enter advances a page, the text field takes focus, and the review submits every answer", async () => {
    const app = await openApp({ questions: [two] });
    await openSession(app, "ses_1");
    await app.waitFor("id:question-page");
    expect(app.texts().includes("Which approach should we use?")).toBe(true);
    expect(app.texts().includes("Anything else for the release?")).toBe(false);
    expect(app.texts().includes("Question 1 of 2")).toBe(true);

    await app.press("label:Which approach should we use? Conservative migration");
    expect(app.requests().some((r) => r.method === "POST" && r.url.includes("/question/"))).toBe(false);
    await app.press("id:question-next");
    await app.waitFor("text:Anything else for the release?");
    expect(app.texts().includes("Question 2 of 2")).toBe(true);
    // The second page's free-text line has focus, so typing lands there and Enter advances to review.
    await app.keyboard.press("q");
    await app.waitUntil(() => app.value("id:question-free-note") === "q");
    await app.keyboard.press("enter");
    await app.waitFor("id:question-review");
    expect(app.texts().includes("Conservative migration")).toBe(true);
    expect(app.texts().includes("q")).toBe(true);
    await app.press("id:submit-answers");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/qst_two")).length === 1);
    const body = JSON.parse(app.requests().find((r) => r.method === "POST" && r.url.includes("/qst_two"))!.body as string);
    expect(body.answers).toEqual([
      { question_id: "approach", selected: ["safe"], text: null },
      { question_id: "note", selected: [], text: "q" },
    ]);
  });
});

describe("queued row actions (plan 31)", () => {
  const inputs = [
    { id: "in_steer", session_id: "ses_1", mode: "steer", parts: [{ type: "text", text: scenario.steer[0] }] },
    { id: "in_q1", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: scenario.queue[0] }] },
    { id: "in_q2", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: scenario.queue[1] }] },
    { id: "in_q3", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: scenario.queue[2] }] },
  ];
  test("only queued rows show edit, up, down and remove; bounds follow the queued group", async () => {
    const app = await open("./ComposerFixture.tsx", { network: network({ inputs }), width: 1280, height: 768, platform: "web" });
    await app.waitFor("id:splash", { state: "gone" });
    await openSession(app, "ses_1");
    await app.wait(500);
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-queue-state.png");
    await app.waitFor("id:pending-in_q1");
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-queue-actions-dark.png");
    await app.hover("id:pending-in_steer");
    expect(app.shows("id:pending-edit-in_steer")).toBe(false);
    expect(app.shows("id:pending-steer-in_q1")).toBe(false);
    await app.hover("id:pending-in_q1");
    for (const id of scenario.queue_actions) expect(app.shows(`id:pending-${id}-in_q1`)).toBe(true);
    expect(app.enabled("id:pending-up-in_q1")).toBe(false);
    expect(app.enabled("id:pending-down-in_q1")).toBe(true);
    expect(app.enabled("id:pending-up-in_q2")).toBe(true);
    expect(app.enabled("id:pending-down-in_q2")).toBe(true);
    expect(app.enabled("id:pending-up-in_q3")).toBe(true);
    expect(app.enabled("id:pending-down-in_q3")).toBe(false);

    await app.press("id:pending-down-in_q1");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/input/in_q2/move")));
    const down = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/input/in_q2/move"))!;
    expect(JSON.parse(down.body as string)).toEqual({ before: "in_q1" });

    await app.hover("id:pending-in_q3");
    await app.press("id:pending-up-in_q3");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/input/in_q3/move")));
    const up = app.requests().find((r) => r.method === "POST" && r.url.endsWith("/input/in_q3/move"))!;
    expect(JSON.parse(up.body as string)).toEqual({ before: "in_q2" });
  });
});

describe("worktree indicator (plan 32)", () => {
  test("the icon has the tooltip, opens the copyable details and shows a Copied toast", async () => {
    const app = await openApp({});
    await openSession(app, "ses_1");
    await app.waitFor("id:location-worktree");
    expect(app.shows("label:Worktree session, open details")).toBe(true);
    // The strip's shared tooltip is the Button hint; headless web does not raise hint overlays.
    await app.hover("id:location-worktree");
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-worktree-tooltip-dark.png");
    await app.press("id:location-worktree");
    await app.waitFor("id:worktree-info-dialog");
    expect(app.texts().includes(scenario.worktree_info.title)).toBe(true);
    expect(app.texts().includes(scenario.worktree_info.body)).toBe(true);
    const plain = (text: string) => text.replace(/\u200b/g, "");
    expect(app.texts().some((t) => plain(t) === scenario.worktree_info.path)).toBe(true);
    expect(app.texts().some((t) => plain(t) === scenario.worktree_info.branch)).toBe(true);
    expect(app.texts().some((t) => plain(t) === scenario.worktree_info.main_path)).toBe(true);
    expect(app.shows("text:Manage worktrees")).toBe(false);
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-worktree-modal-dark.png");

    await app.press("id:worktree-path-copy");
    await app.waitFor("text:Copied");
    expect(app.clipboard()).toBe(scenario.worktree_info.path);
    await app.press("id:worktree-branch-copy");
    await app.waitFor("text:Copied");
    expect(app.clipboard()).toBe(scenario.worktree_info.branch);
    await app.press("id:worktree-main-path-copy");
    await app.waitFor("text:Copied");
    expect(app.clipboard()).toBe(scenario.worktree_info.main_path);
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:worktree-info-dialog"));
  });
});

describe("session control failure and edge cases", () => {
  test("an archive refusal is shown and leaves its tab open", async () => {
    const app = await openApp({ archiveError: true });
    await openSession(app);
    await app.type("id:composer-input", "/archive");
    await app.submit("id:composer-input");
    await app.waitFor("text:/background job is running/");
    expect(app.shows("id:session-tab-ses_1")).toBe(true);
  });

  test("a failed answer keeps its text and can be retried", async () => {
    const textOnly = questionWith({ options: [], allow_free_text: true });
    const app = await openApp({ questions: [textOnly], answerFailures: 1 });
    await openSession(app);
    await app.waitFor("id:question-free-approach");
    await app.type("id:question-free-approach", "Keep this answer");
    await app.submit("id:question-free-approach");
    await app.waitFor("text:Temporary answer failure");
    expect(app.value("id:question-free-approach")).toBe("Keep this answer");
    await app.submit("id:question-free-approach");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/")).length === 2);
    const posts = app.requests().filter((r) => r.method === "POST" && r.url.includes("/question/"));
    expect(posts[0].body).toBe(posts[1].body);
  });

  test("edit restores a queued message, while remove confirms and deletes it", async () => {
    const queued = (id: string, text: string) => ({ id, session_id: "ses_1", mode: "queue", parts: [{ type: "text", text }] });
    const flags: Flags = { inputs: [queued("q1", scenario.queue[0]), queued("q2", scenario.queue[1])] };
    const app = await openApp(flags);
    await openSession(app);
    await app.wait(150);
    await app.hover("id:pending-q1");
    await app.press("id:pending-edit-q1");
    await app.waitUntil(() => app.value("id:composer-input") === scenario.queue[0]);
    await app.hover("id:pending-q2");
    await app.press("id:pending-remove-q2");
    await app.waitFor("id:confirm-dialog");
    expect(app.requests().filter((r) => r.method === "DELETE").length).toBe(1);
    await app.press("id:confirm-ok");
    await app.waitUntil(() => app.requests().filter((r) => r.method === "DELETE").length === 2);
    expect(app.value("id:composer-input")).toBe(scenario.queue[0]);
  });

  test("a visible question blocks queued edit and Send next", async () => {
    const input = { id: "q1", session_id: "ses_1", mode: "queue", parts: [{ type: "text", text: scenario.queue[0] }] };
    const app = await openApp({ inputs: [input], questions: [scenario.question] });
    await openSession(app);
    await app.wait(150);
    await app.hover("id:pending-q1");
    await app.press("id:pending-edit-q1");
    expect(app.requests().some((r) => r.method === "DELETE")).toBe(false);
    expect(app.enabled("id:pending-send-next")).toBe(false);
    expect(app.value("id:composer-input")).toBe("");
  });

  test("a long worktree path wraps, scrolls, and copies exactly", async () => {
    const directory = `/work/${"unbroken_component_".repeat(60)}final-tail`;
    const app = await openApp({ directory });
    await openSession(app);
    await app.waitFor("id:location-worktree");
    await app.press("id:location-worktree");
    await app.waitFor("id:worktree-path-copy");
    await app.press("id:worktree-path-copy");
    await app.waitUntil(() => app.clipboard() === directory);
    await app.scroll("id:worktree-info-scroll", 10000);
    await app.waitUntil(() => app.shows("id:worktree-info-dialog"));
    expect(app.shows("id:worktree-info-dialog")).toBe(true);
    await app.screenshot("../reports/session-controls-2026-11-11/nsh-worktree-long-path-dark.png");
  });
});
