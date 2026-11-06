// The timeline follows the end as the TUI does, and keeps measured rows across rebuilds.
import { describe, expect, open, test } from "nsh/test";
import { keepIdentity } from "./src/ui/keep-identity";
import { followsAfterScroll, pinReleased, pinSpacer } from "./src/scroll-follow";

const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Long session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const messages = Array.from({ length: 60 }, (_, i) => ({
  id: `m${i}`, session_id: "ses_1", seq: i + 1, role: i % 2 ? "assistant" : "user", parts: [{ type: "text", text: `Message ${i}\n\nSome text to give the row a height.` }], created: i + 1,
}));
const session2 = { ...session, id: "ses_2", title: "Other session", created: 2, updated: 3 };
const messages2 = messages.map((m) => ({ ...m, id: `n${m.id.slice(1)}`, session_id: "ses_2" }));
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": {
    body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session, session2], running: [], approvals: [], questions: [], selected: null },
  },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: messages },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_2/message*": { body: messages2 },
  "GET */api/session/ses_2/snapshot": { body: null },
  "GET */api/session/ses_2/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_2/worktrees": { body: { worktrees: [] } },
  "GET */api/session/*": { body: session },
};

describe("timeline scrolling", () => {
  test("a session opens at its end, scrolling up leaves it, and the jump returns to it", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768 });
    await app.waitFor("text:Long session");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    expect(app.shows("id:jump-latest")).toBe(false);
    // Only a list that opened at its end has anywhere to go up to.
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:jump-latest");
    await app.press("id:jump-latest");
    await app.waitUntil(() => !app.shows("id:jump-latest"));
    // Upward again from the end: the jump really moved the list there.
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:jump-latest");
  });

  test("a quiet scroll bar shows while scrolling, then fades away", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768 });
    await app.waitFor("text:Long session");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    await app.wait(350);
    expect(app.shows("id:timeline-scrollbar")).toBe(false);
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:timeline-scrollbar");
    await app.waitUntil(() => !app.shows("id:timeline-scrollbar"));
  });

  test("the Scroll bar setting can keep it off", async () => {
    const app = await open("./App.tsx", {
      network, width: 1024, height: 768,
      storage: { "nexus.prefs": JSON.stringify({ scrollBar: false }) },
    });
    await app.waitFor("text:Long session");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-appearance");
    await app.waitFor("id:setting-appearance.scrollbar");
    expect(app.shows("label:Scroll bar: off")).toBe(true);
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:settings"));
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    await app.wait(350);
    await app.scroll("id:timeline", -300);
    expect(app.shows("id:timeline-scrollbar")).toBe(false);
  });

  test("switching away and back restores where the reader was", async () => {
    const app = await open("./App.tsx", { network, width: 1024, height: 768 });
    await app.waitFor("text:Long session");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    await app.scroll("id:timeline", -600);
    await app.waitFor("id:jump-latest");
    await app.press("id:session-ses_2");
    await app.waitFor("text:Other session");
    await app.press("id:session-ses_1");
    await app.waitFor("id:timeline-row");
    // It reopens at the saved offset, not forced back to the end.
    await app.waitFor("id:jump-latest");
    expect(app.shows("id:jump-latest")).toBe(true);
  });

  test("streaming content never pulls a reader who scrolled up back down", () => {
    const base = { y: 1000, lastY: 1000, gap: 800, following: false, contentH: 5000, viewH: 768 };
    // Content grew while the offset stayed: the reader must stay where they are.
    expect(followsAfterScroll({ ...base, gap: 1600 })).toBe(false);
    // A one-pixel upward move leaves follow too; only returning near the end resumes it.
    expect(followsAfterScroll({ ...base, lastY: 1001 })).toBe(false);
    expect(followsAfterScroll({ ...base, y: 4900, lastY: 4800, gap: 10 })).toBe(true);
    // Short content is always at its end.
    expect(followsAfterScroll({ ...base, following: true, gap: 0, contentH: 400 })).toBe(true);
  });

  test("unchanged rows keep their objects, so the list keeps their measured heights", () => {
    const a = { key: "a", kind: "notice", text: "same" } as any;
    const b = { key: "b", kind: "activity", items: [{ key: "t", text: "x" }] } as any;
    const c = { key: "c", kind: "notice", text: "old" } as any;
    const next = keepIdentity([a, b, c], [{ ...a }, { ...b, items: [{ key: "t", text: "x" }] }, { ...c, text: "new" }, { key: "d" } as any]);
    expect(next[0]).toBe(a);
    expect(next[1]).toBe(b);
    expect(next[2]).not.toBe(c);
    expect((next[2] as any).text).toBe("new");
    expect(next[3].key).toBe("d");
  });

  test("a session's first message is pinned to the top: the view stays there while the reply is empty", async () => {
    const created = { ...session, id: "ses_new", title: "First", created: 3, updated: 3 };
    const first = [{ id: "msg_first", session_id: "ses_new", seq: 1, role: "user", parts: [{ type: "text", text: "Pin this message" }], created: 1 }];
    const app = await open("./App.tsx", {
      network: {
        "POST */api/session": { body: created },
        "GET */api/session/ses_new": { body: created },
        "GET */api/session/ses_new/message*": { body: first },
        "GET */api/session/ses_new/snapshot": { body: { starting: {} } },
        "GET */api/session/ses_new/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
        "GET */api/session/ses_new/worktrees": { body: { worktrees: [] } },
        "POST */api/session/ses_new/prompt": { status: 204, body: null },
        ...network,
      },
      width: 1024, height: 768, platform: "web",
    });
    await app.waitFor("id:rail-project-prj_1");
    await app.press("id:new-session-row");
    await app.type("id:composer-input", "Pin this message");
    await app.press("id:send");
    await app.waitFor("id:first-pin");
    await app.wait(400);
    expect(app.texts().some((t) => t.includes("Pin this message"))).toBe(true);
    // A pinned view is not "scrolled up": no jump pill, and nothing pulls it to the bottom.
    expect(app.shows("id:jump-latest")).toBe(false);
    // The pin holds while nothing else happens, and ends when the reader scrolls.
    expect(app.shows("id:first-pin")).toBe(true);
    await app.scroll("id:timeline", -200);
    await app.waitUntil(() => !app.shows("id:first-pin"));
  });

  test("the first-send spacer fills what the reply leaves of the view, and goes once the reply fills it", () => {
    expect(pinSpacer(100, 700)).toBe(600);
    expect(pinSpacer(700, 700)).toBe(0);
    expect(pinSpacer(900, 700)).toBe(0);
  });

  test("the pin ends when the reader moves the view, not when it settles", () => {
    expect(pinReleased({ y: 300, target: 300, sinceApplied: 1000 })).toBe(false);
    expect(pinReleased({ y: 100, target: 300, sinceApplied: 50 })).toBe(false);
    expect(pinReleased({ y: 100, target: 300, sinceApplied: 1000 })).toBe(true);
  });
});
