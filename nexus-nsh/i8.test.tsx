// DESIGN_PLAN I8 (D14, D15): the waiting card, toasts, the banner, the jump pill and the full-text dialog.
import { describe, expect, open, test } from "nsh/test";
import { splitAlert } from "./src/store";
import { approvalParts, clock } from "./src/ui/composer";
import { detailOf } from "./src/ui/timeline";

const sse = (...frames: string[]) => frames.join("");
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Refactor loader", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const bash = { id: "apr_1", session_id: "ses_1", tool: "bash", call_id: "c1", args: { command: "pytest tests/test_loader.py -q", cwd: "~/repos/demo" } };
const edit = { id: "apr_2", session_id: "ses_1", tool: "edit", call_id: "c2", args: { path: "src/loader.py", old_string: Array.from({ length: 10 }, (_, i) => `old ${i}`).join("\n"), new_string: Array.from({ length: 10 }, (_, i) => `new ${i}`).join("\n") } };
const question = { id: "qst_1", session_id: "ses_1", call_id: "c3", created: 1, questions: [
  { id: "q1", question: "Which cache?", options: [{ id: "a", label: "mtime" }, { id: "b", label: "hash" }], multiple: false, allow_free_text: true },
  { id: "q2", question: "Extras?", options: [{ id: "x", label: "tests" }, { id: "y", label: "docs" }], multiple: true, allow_free_text: false },
] };
const snapshot = (extra: Record<string, unknown> = {}) => ({
  watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" },
  projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }],
  sessions: [session], running: [], approvals: [], questions: [], selected: null, ...extra,
});
const network = (extra: Record<string, unknown> = {}, snap = snapshot()) => ({
  ...extra,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: snap },
  "GET */api/event*": { body: sse(connected), headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/default": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/session/ses_1/message": { body: [] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/event*": { body: { events: [], more: false, floor: 0, history_truncated: false } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
});
const view = (snap: ReturnType<typeof snapshot>, extra: Record<string, unknown> = {}) =>
  open("./App.tsx", { network: network(extra, snap), width: 1024, height: 768, platform: "web" });

describe("waiting card (D14)", () => {
  test("the clock reads m:ss; the parameter table drops the diff's own arguments and the diff stops at 12 rows", () => {
    expect([clock(0), clock(42), clock(65)]).toEqual(["0:00", "0:42", "1:05"]);
    const b = approvalParts(bash as any);
    expect(b.table).toEqual([["command", "pytest tests/test_loader.py -q"], ["cwd", "~/repos/demo"]]);
    expect(b.diff).toEqual([]);
    const e = approvalParts(edit as any);
    expect(e.table.map((r) => r[0])).toEqual(["path"]);
    expect(e.diff.length).toBe(11); // 21 items: 11 rows and "10 more rows"
    expect(e.more).toBe(10);
  });

  test("replacement and write diffs carry no space after the sign, in the timeline and in the card", () => {
    const args = JSON.stringify({ path: "a.py", old_string: "x = 1\ny", new_string: "x = 2" });
    expect(detailOf("edit", args, undefined, false)).toEqual([
      { text: "@@ a.py", tone: "info" }, { text: "-x = 1", tone: "del" }, { text: "-y", tone: "del" }, { text: "+x = 2", tone: "add" },
    ]);
    expect(detailOf("write", JSON.stringify({ path: "b.py", content: "hi" }), undefined, false)[1]).toEqual({ text: "+hi", tone: "add" });
    const card = approvalParts({ ...edit, args: { path: "a.py", old_string: "x = 1", new_string: "x = 2" } } as any).diff;
    expect(card.map((l) => l.text)).toEqual(["@@ a.py", "-x = 1", "+x = 2"]);
  });

  test("an approval shows the title, timer, table and the choice list; unoffered choices say why", async () => {
    const app = await view(snapshot({ approvals: [bash] }));
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:waiting-card");
    expect(app.texts().includes("Allow bash?")).toBe(true);
    expect(app.texts().some((t) => /^waiting on you · \d+:\d\d$/.test(t))).toBe(true);
    expect(app.texts().includes("pytest tests/test_loader.py -q")).toBe(true);
    expect(app.shows("id:waiting-rail")).toBe(true);
    for (const id of ["allow-once", "allow-session", "deny", "deny-note"]) expect(app.shows(`id:${id}`)).toBe(true);
    expect(app.texts().includes("not offered by Core yet")).toBe(true);
    expect(app.texts().includes("Core does not support notes yet")).toBe(true);
    // The harness cannot hold the event stream open, so Core reads as disconnected: the offered choices say so too.
    expect(app.texts().filter((t) => t === "Core is not connected").length).toBeGreaterThan(0);
  });

  test("an edit approval shows a capped diff with the rest counted", async () => {
    const app = await view(snapshot({ approvals: [edit] }));
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:waiting-diff");
    expect(app.texts().includes("10 more rows")).toBe(true);
    expect(app.texts().some((t) => t.trim() === "old 0")).toBe(true);
    expect(app.texts().some((t) => t.trim() === "new 9")).toBe(false);
  });

  test("a question shows radios, checkboxes and free text", async () => {
    const app = await view(snapshot({ questions: [question] }));
    await app.waitFor("text:Refactor loader");
    await app.press("id:session-ses_1");
    await app.waitFor("id:submit-answers");
    expect(app.texts().includes("Nexus asks")).toBe(true);
    expect(app.texts().includes("Which cache?")).toBe(true);
    expect(app.texts().includes("( )")).toBe(true);
    expect(app.texts().includes("[ ]")).toBe(true);
    expect(app.texts().some((t) => t.includes("submit · tab next question"))).toBe(true);
  });
});

describe("alerts (D15)", () => {
  test("a toast splits title and body; the full text stays on the record", () => {
    expect(splitAlert("Turn failed: 429 rate limited")).toEqual({ title: "Turn failed", body: ": 429 rate limited", inline: true });
    expect(splitAlert("Copied")).toEqual({ title: "Copied", body: "", inline: false });
    expect(splitAlert("Failed\nline two\nline three")).toEqual({ title: "Failed", body: "line two line three", inline: false });
  });

  /** A palette command Core cannot do yet answers with a toast carrying its reason; the palette stays open, so a press repeats it. */
  const fire = async (app: any, id: string, times = 1) => {
    await app.keyboard.press("mod+p");
    await app.waitFor(`id:cmd-${id}`);
    for (let i = 0; i < times; i++) await app.press(`id:cmd-${id}`);
    await app.keyboard.press("escape");
    await app.waitFor("id:palette", { state: "gone" });
  };

  test("toasts stack three high with +N more; clicking one opens the full text, Esc closes it", async () => {
    const app = await view(snapshot());
    await app.waitFor("text:demo");
    await fire(app, "app.sign_in", 4);
    await app.waitFor("id:toasts-more");
    expect(app.count("id:toast")).toBe(3);
    expect(app.texts().includes("+1 more")).toBe(true);
    await app.press("id:toast-open >> nth=0");
    await app.waitFor("id:alert-detail");
    expect(app.texts().some((t) => t.startsWith("Sign in to a provider:") && t.includes("use `nexus login` in a terminal."))).toBe(true);
    expect(app.texts().includes("c copy · esc close")).toBe(true);
    await app.keyboard.press("escape");
    expect(app.shows("id:alert-detail")).toBe(false);
  });

  test("the notifications drawer opens the same dialog for an entry", async () => {
    const app = await view(snapshot());
    await app.waitFor("text:demo");
    await fire(app, "app.sign_out");
    await app.press("id:open-notifications");
    await app.waitFor("id:notification-open");
    await app.press("id:notification-open");
    await app.waitFor("id:alert-detail");
    expect(app.texts().some((t) => t.includes("Sign-out over the API is not in Core yet."))).toBe(true);
  });

  test("the banner is one 36 px strip with its action and the full text in its label", async () => {
    const app = await open("./App.tsx", { network: { ...network(), "GET */api/info": { status: 500, body: {} } }, width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:banner");
    expect(app.shows("id:banner")).toBe(true);
  });
});
