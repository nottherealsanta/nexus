// The message rail (appearance.turn_rail) and turn.prev/turn.next: the NSH half of the TUI's
// draw_turn_rail, rail_jump, rail_scrub_to and adjacent_turn, with the same rules.
import { describe, expect, test } from "nsh/test";
import { open } from "./open-bundle";
import { timelineKeysActive } from "./src/commands";
import { centre, composerFocused, focusedSidebarRow, paletteOpen, picker, resourcesOpen, settingsEdit, settingsSearchActive } from "./src/store";
import { terminalFocused } from "./src/terminal-panel";
import { adjacentUser, railLabel, railLayout, railScrubTop, revealTop, rowGeometry } from "./src/turn-rail";

const row = (key: string, kind: string, n?: number, text?: string) => ({ key, kind, n, text });

describe("turn key routing", () => {
  test("the turn keys stand down for the sidebar, composer, terminal and Settings", () => {
    const saved = {
      centre: centre(), composer: composerFocused(), sidebar: focusedSidebarRow(), terminal: terminalFocused(), search: settingsSearchActive(),
      palette: paletteOpen(), picker: picker(), resources: resourcesOpen(), edit: settingsEdit(),
    };
    try {
      centre.set("session"); composerFocused.set(false); focusedSidebarRow.set(null); terminalFocused.set(false); settingsSearchActive.set(false);
      paletteOpen.set(false); picker.set(null); resourcesOpen.set(false); settingsEdit.set(null);
      expect(timelineKeysActive()).toBe(true);
      focusedSidebarRow.set({ kind: "project", id: "prj_1" });
      expect(timelineKeysActive()).toBe(false);
      focusedSidebarRow.set(null);
      composerFocused.set(true);
      expect(timelineKeysActive()).toBe(false);
      composerFocused.set(false);
      terminalFocused.set(true);
      expect(timelineKeysActive()).toBe(false);
      terminalFocused.set(false);
      settingsSearchActive.set(true);
      expect(timelineKeysActive()).toBe(false);
      settingsSearchActive.set(false);
      centre.set("settings");
      expect(timelineKeysActive()).toBe(false);
      centre.set("session");
      paletteOpen.set(true);
      expect(timelineKeysActive()).toBe(false);
      paletteOpen.set(false);
      picker.set({ title: "Pick", items: [] });
      expect(timelineKeysActive()).toBe(false);
      picker.set(null);
      resourcesOpen.set(true);
      expect(timelineKeysActive()).toBe(false);
      resourcesOpen.set(false);
      settingsEdit.set({ id: "appearance.theme", draft: "dark" });
      expect(timelineKeysActive()).toBe(false);
    } finally {
      centre.set(saved.centre);
      composerFocused.set(saved.composer);
      focusedSidebarRow.set(saved.sidebar);
      terminalFocused.set(saved.terminal);
      settingsSearchActive.set(saved.search);
      paletteOpen.set(saved.palette);
      picker.set(saved.picker);
      resourcesOpen.set(saved.resources);
      settingsEdit.set(saved.edit);
    }
  });
});

describe("message rail geometry", () => {
  test("needs two user messages and a track at least three pixels high", () => {
    const one = [row("c", "context"), row("u", "user", 1, "one"), row("a", "assistant")];
    expect(railLayout({ rows: one, heights: {}, sid: "s", scrollY: 0, viewH: 300, trackH: 100 })).toBe(null);
    const two = [...one, row("u2", "user", 2, "two")];
    expect(railLayout({ rows: two, heights: {}, sid: "s", scrollY: 0, viewH: 300, trackH: 2 })).toBe(null);
    expect(railLayout({ rows: two, heights: {}, sid: "s", scrollY: 0, viewH: 300, trackH: 100 })).not.toBe(null);
  });

  test("only delivered user messages mark: a pending or queued pending row does not", () => {
    const rows = [row("context", "context"), row("a", "user", 1, "first"), row("p", "pending"), row("q", "user", 2, "queued"), row("b", "user", 3, "third")];
    const layout = railLayout({ rows, heights: {}, sid: "s", scrollY: 0, viewH: 300, trackH: 100 })!;
    expect(layout.marks.map((m) => m.key)).toEqual(["a", "q", "b"]);
  });

  test("marks follow each row's offset, and seen matches the viewport", () => {
    const rows = [row("context", "context"), row("a", "user", 1, "first"), row("x", "assistant"), row("b", "user", 2, "second")];
    const heights = { "s\u0000context": 20, "s\u0000a": 100, "s\u0000x": 200, "s\u0000b": 100 };
    const total = rowGeometry({ rows, heights, sid: "s" }).total;
    expect(total).toBe(432);
    const layout = railLayout({ rows, heights, sid: "s", scrollY: 0, viewH: 300, trackH: 100 })!;
    expect(layout.marks.map((m) => m.key)).toEqual(["a", "b"]);
    expect(layout.marks[0].top).toBe(Math.floor((32 * 100) / 432));
    expect(layout.marks[1].top).toBe(Math.floor((332 * 100) / 432));
    expect(layout.marks.map((m) => m.seen)).toEqual([true, false]);
    const lower = railLayout({ rows, heights, sid: "s", scrollY: 300, viewH: 300, trackH: 100 })!;
    expect(lower.marks.map((m) => m.seen)).toEqual([false, true]);
  });

  test("a fully visible conversation fills the band; a long one gets a minimum band", () => {
    const rows = [row("a", "user", 1, "first"), row("b", "user", 2, "second")];
    const heights = { "s\u0000a": 20, "s\u0000b": 20 };
    const full = railLayout({ rows, heights, sid: "s", scrollY: 0, viewH: 500, trackH: 100 })!;
    expect(full.band).toEqual({ top: 0, height: 100 });
    const long = railLayout({ rows, heights, sid: "s", scrollY: 1000, viewH: 100, trackH: 100 })!;
    expect(long.band.height).toBe(8);
  });

  test("the track scrub centres the conversation and clamps to the ends", () => {
    expect(railScrubTop(0, 100, 1000, 300)).toBe(0);
    expect(railScrubTop(50, 100, 1000, 300)).toBe(350);
    expect(railScrubTop(99, 100, 1000, 300)).toBe(700);
    expect(railScrubTop(130, 100, 1000, 300)).toBe(700);
    expect(railScrubTop(50, 100, 200, 300)).toBe(null);
    expect(railScrubTop(50, 0, 1000, 300)).toBe(null);
  });

  test("previous and next start at the first message with no selection", () => {
    const rows = [row("context", "context"), row("a", "user", 1, "first"), row("x", "assistant"), row("b", "user", 2, "second")];
    expect(adjacentUser(rows, null, 1)).toBe("a");
    expect(adjacentUser(rows, null, -1)).toBe(null);
    expect(adjacentUser(rows, "a", 1)).toBe("b");
    expect(adjacentUser(rows, "b", 1)).toBe(null);
    expect(adjacentUser(rows, "b", -1)).toBe("a");
    expect(adjacentUser(rows, "missing", 1)).toBe("a");
  });

  test("revealing a message scrolls the least distance", () => {
    expect(revealTop(100, 50, 500, 300, 1000)).toBe(100);
    expect(revealTop(800, 100, 0, 300, 1000)).toBe(600);
    expect(revealTop(800, 500, 0, 300, 1000)).toBe(700);
    expect(revealTop(400, 50, 100, 300, 1000)).toBe(150);
    expect(revealTop(950, 100, 0, 300, 1000)).toBe(700);
  });

  test("the label is the first non-empty line, truncated to 48 characters", () => {
    expect(railLabel(2, "\n\nsecond question\nmore")).toBe("#2 · second question");
    const long = railLabel(12, "x".repeat(80));
    expect([...long].length).toBe(48);
    expect(long.endsWith("…")).toBe(true);
    expect(long.startsWith("#12 · xxx")).toBe(true);
  });
});

const newCount = (app: any) => app.texts().find((text: string) => /^\d+ new$/.test(text));

describe("message rail in the timeline", () => {
  test("shows one mark per user message, with a tooltip, and no scroll bar", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    expect(app.count("id:turn-rail-mark")).toBe(10);
    expect(app.shows("id:timeline-scrollbar")).toBe(false);
    expect(app.accessibility("id:turn-rail-mark >> nth=0")?.label).toBe("#1 · Message 0");
  });

  test("pressing the track scrubs the conversation", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    await app.press("id:turn-rail-track", { position: { x: 6, y: 120 } });
    await app.waitFor("text:scrolled");
  });

  test("dragging the band scrubs the conversation", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    await app.drag("id:turn-rail-band", { y: -400 });
    await app.waitFor("text:scrolled");
  });

  test("a mark jumps to its message at the top and the turn keys walk from there", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    await app.press("id:turn-rail-mark >> nth=0");
    await app.waitFor("id:user-text-1");
    await app.waitFor("text:scrolled");
    const atFirst = newCount(app);
    expect(atFirst).toBeTruthy();
    await app.keyboard.press("alt+down");
    await app.waitUntil(() => newCount(app) !== atFirst);
    await app.keyboard.press("alt+up");
    await app.waitUntil(() => newCount(app) === atFirst);
  });

  test("the turn keys leave alt+up/down to the project rail", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    await app.press("id:turn-rail-mark >> nth=0");
    await app.waitFor("id:user-text-1");
    await app.waitFor("text:scrolled");
    const atFirst = newCount(app);
    await app.press("id:fixture-sidebar-focus");
    await app.keyboard.press("alt+down");
    await app.wait(200);
    expect(newCount(app)).toBe(atFirst);
    await app.press("id:fixture-sidebar-blur");
    await app.keyboard.press("alt+down");
    await app.waitUntil(() => newCount(app) !== atFirst);
  });

  test("the band and mark colours follow scrolling, and the geometry follows a resize", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    const bandAtEnd = app.layout("id:turn-rail-band");
    const markAtEnd = app.viewStyle("id:turn-rail-mark-bar-1")?.backgroundColor;
    expect(bandAtEnd).toBeTruthy();
    expect(markAtEnd).toBeTruthy();
    await app.press("id:turn-rail-mark >> nth=0");
    await app.waitFor("text:scrolled");
    await app.waitUntil(() => app.layout("id:turn-rail-band")?.y !== bandAtEnd?.y);
    expect(app.viewStyle("id:turn-rail-mark-bar-1")?.backgroundColor).not.toBe(markAtEnd);
    const bandAtTop = app.layout("id:turn-rail-band")!;
    const middleMark = app.layout("id:turn-rail-mark >> nth=4")?.y;
    await app.setViewport(1024, 520);
    await app.waitUntil(() => app.layout("id:turn-rail-band")?.height !== bandAtTop.height);
    expect(app.layout("id:turn-rail-mark >> nth=4")?.y).not.toBe(middleMark);
  });

  test("a session with one user message has no rail", async () => {
    const app = await open("./TurnRailSingleFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:timeline-row");
    expect(app.shows("id:turn-rail")).toBe(false);
    expect(app.count("id:turn-rail-mark")).toBe(0);
  });

  test("turning the setting off hides the rail live and brings the scroll bar back", async () => {
    const app = await open("./TurnRailFixture.tsx", { width: 1024, height: 768, platform: "web" });
    await app.waitFor("id:turn-rail");
    await app.press("id:fixture-rail-off");
    await app.waitUntil(() => !app.shows("id:turn-rail"));
    await app.scroll("id:timeline", -300);
    await app.waitFor("id:timeline-scrollbar");
  });
});

// Settings is the real overlay, so this part runs the app; it needs no session history.
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n`;
const settingsNetwork = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};

describe("Message rail setting", () => {
  test("Settings shows it on by default and stores the choice", async () => {
    const app = await open("./App.tsx", { network: settingsNetwork, width: 1280, height: 768 });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+,");
    await app.waitFor("id:settings");
    await app.press("id:section-appearance");
    await app.waitFor("id:setting-appearance.turn_rail");
    expect(app.shows("label:Message rail: on")).toBe(true);
    await app.press("id:seg-appearance.turn_rail-off");
    await app.waitUntil(() => JSON.parse(app.storage()["nexus.prefs"] ?? "{}").turnRail === false);
  });
});
