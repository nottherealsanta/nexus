import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/inspector.json";

// One handler answers every inspector route from the shared scenario; tests steer it through `core`.
const core = { head: scenario.head as Record<string, unknown>, headFails: false, slowView: "" };
const reset = () => { core.head = scenario.head; core.headFails = false; core.slowView = ""; };
const answer = (r: { url: string }) => {
  const url = new URL(r.url, "http://x");
  if (url.pathname.endsWith("/inspect/head")) return core.headFails ? { error: "offline" } : { body: core.head };
  if (url.pathname.endsWith("/inspect/record")) return { body: scenario.record };
  const q = url.searchParams;
  const view = q.get("view") ?? "Timing";
  const pick = () => {
    if (view === "Conversation") return q.get("before") ? scenario.conversation_older : q.get("q") ? scenario.conversation_filtered : scenario.conversation;
    if (view === "Steps") return scenario.steps;
    if (view === "Questions") return scenario.questions;
    if (view === "Raw" || view === "Requests") return { ...scenario.raw, view };
    return scenario.timing;
  };
  return view === core.slowView ? { body: pick(), delay: 400 } : { body: pick() };
};
const network = { "GET */api/session/*/inspect*": answer };
const calls = (app: { requests: () => { method: string; url: string }[] }, part = "/inspect") => app.requests().filter((r) => r.method === "GET" && new URL(r.url, "http://x").pathname.endsWith(part));
const params = (r: { url: string }) => Object.fromEntries(new URL(r.url, "http://x").searchParams);
const text = (app: { texts: () => string[] }) => app.texts().join("\n");
/** Wait until some shown text contains `sub` (`waitFor("text:")` matches a whole text). */
const see = async (app: { texts: () => string[] }, sub: string, ms = 4000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (app.texts().some((t) => t.includes(sub))) return; await new Promise((r) => setTimeout(r, 25)); }
  throw new Error(`never showed ${JSON.stringify(sub)}; on screen: ${JSON.stringify(app.texts()).slice(0, 1500)}`);
};
const tick = async (app: { clock: { fastForward: (ms: number) => Promise<unknown> } | null; waitFor: (s: string) => Promise<unknown> }, ms = 2100) => { await app.clock!.fastForward(ms); await app.waitFor("id:inspector"); await new Promise((r) => setTimeout(r, 80)); await app.waitFor("id:inspector"); };
const SHOTS = "../reports/section14/inspector";

const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const session = { id: "ses_inspector", project_id: "prj_1", directory: "/work/demo", title: "Review", model: "scripted/m", permission: "allow", created: 1, updated: 2, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const appNetwork = {
  ...network,
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/config": { body: { revision: "t", values: { defaults: {}, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
};
const fixture = async (o: Record<string, unknown> = {}) => { reset(); const app = await open("./InspectorFixture.tsx", { width: 1280, height: 900, colorScheme: "dark", network, ...o }); await app.waitFor("id:inspector"); await see(app, o.width === 390 ? "Live · updated" : "Session lifetime"); return app; };

describe("native inspector (API v2)", () => {
  test("the app palette opens the native inspector with Core data", async () => {
    reset();
    const app = await open("./App.tsx", { width: 1280, height: 900, network: appNetwork });
    await app.waitFor("id:header");
    await app.keyboard.press("mod+p");
    await app.waitFor("id:palette-input");
    await app.type("id:palette-input", "Open inspector");
    await app.press("id:cmd-app.open_inspector");
    await app.waitFor("id:inspector");
    await see(app, "Session lifetime · Input 12,000 · Cached 8,400 · Uncached 3,600");
    const first = params(calls(app)[0]!);
    expect(first.view).toBe("Timing");
    expect(first.limit).toBe("50");
    expect(first.page).toBeUndefined();
    await app.keyboard.press("Escape");
    expect(app.shows("id:inspector")).toBe(false);
  });

  test("cache labels lifetime and displayed page separately, with coverage, models and unknown writes", async () => {
    const app = await fixture();
    const t = text(app);
    for (const fact of [
      "Session lifetime · Input 12,000 · Cached 8,400 · Uncached 3,600",
      "Hit 70% of input tokens reported by provider · Writes unknown",
      "11 of 12 requests reported usage (partial)",
      "scripted/m · 11 requests · input 12,000 · cached 8,400 · hit 70%",
      "Displayed page · Input 1,000 · Cached 750 · Uncached 250",
      "Hit 75% · 1 of 1 requests reported usage",
    ]) expect(t).toContain(fact);
    expect(t).not.toContain("not a prediction");
    await app.press("id:inspect-info");
    expect(text(app)).toContain("not a prediction");
  });

  test("one shared axis, grouped lanes, first-byte marker only when recorded, end unknown, fit and zoom", async () => {
    const app = await fixture();
    await see(app, "Review · 3 operations");
    const t = text(app);
    expect(t).toContain("Scout · 2 operations");
    expect(t).toContain("end unknown");
    expect(t).toContain("1× · view +0.00s to +10.00s of 10.00s");
    expect(t).toContain("+5.00s");
    expect(app.shows("id:ttfb-ses_inspector:model:req_1")).toBe(true);
    expect(app.shows("id:ttfb-ses_child:model:req_2")).toBe(false);
    await app.press("id:lane-ses_inspector:model:req_1");
    await see(app, "Model request: 1000 input, 750 cached");
    await app.screenshot(`${SHOTS}/nsh-timing.png`);
    await app.press("id:inspect-fit");
    expect(text(app)).toMatch(/[2-9]× · view/);
    await app.press("id:inspect-fit");
    expect(text(app)).toContain("1× · view +0.00s");
    await app.press("id:inspect-zoom-in");
    expect(text(app)).toContain("2× · view");
  });

  test("cursor paging uses before=next_before, never a page number, and keeps expansion", async () => {
    const app = await fixture();
    await app.press("id:inspect-Conversation");
    await see(app, "▸ assistant · 3 parts");
    await app.press("id:row-msg_2");
    await see(app, "Clipped · showing the first part of 30.5 KB · e loads the full record");
    expect(text(app)).toContain("Omitted · detail ×1");
    await app.press("id:inspect-older");
    await see(app, "Older records · p newer · oldest retained");
    const older = params(calls(app).at(-1)!);
    expect(older.before).toBe("WyJjIiwxXQ");
    expect(older.page).toBeUndefined();
    expect(text(app)).toContain("Paused · viewing older records");
    await app.press("id:inspect-newer");
    await see(app, "Newest records");
    expect(params(calls(app).at(-1)!).before).toBeUndefined();
    // Expansion and selection survive a refresh and a trip to another view.
    await app.press("id:inspect-refresh");
    await app.press("id:inspect-Timing");
    await see(app, "Review · 3 operations");
    await app.press("id:inspect-Conversation");
    await app.waitFor("id:detail-msg_2");
    await app.screenshot(`${SHOTS}/nsh-conversation.png`);
  });

  test("search and filters go to Core; the full record loads on demand", async () => {
    const app = await fixture();
    await app.press("id:inspect-Conversation");
    await see(app, "▸ assistant · 3 parts");
    await app.type("id:inspect-q", "bash");
    await app.keyboard.press("Enter");
    await see(app, "search \"bash\"");
    expect(params(calls(app).at(-1)!).q).toBe("bash");
    await app.press("id:inspect-kind");
    await see(app, "kind user");
    expect(params(calls(app).at(-1)!)).toMatchObject({ q: "bash", kind: "user" });
    await app.press("id:inspect-clear");
    await see(app, "search none");
    await see(app, "▸ assistant · 3 parts");
    const cleared = params(calls(app).at(-1)!);
    expect(cleared.q).toBeUndefined();
    expect(cleared.kind).toBeUndefined();
    await app.press("id:row-msg_2");
    await app.press("id:inspect-full");
    await see(app, "FULL RECORD TEXT");
    expect(text(app)).toContain("Full record loaded");
    expect(calls(app, "/inspect/record").at(-1)!.url).toContain("kind=message");
  });

  test("the session tree selector inspects a child without switching the chat session", async () => {
    const app = await fixture();
    await app.press("id:inspect-session-ses_child");
    await see(app, "ses_inspector → inspecting Scout (ses_child)");
    expect(params(calls(app).at(-1)!).session).toBe("ses_child");
    await app.press("id:inspect-Questions");
    await see(app, "question pending · 1 item · tool call call_9");
    await app.press("id:row-q_1");
    await see(app, "Links · tool call_9 · turn turn_1");
    expect(text(app)).toContain("question replied");
    await app.screenshot(`${SHOTS}/nsh-questions.png`);
  });

  test("a stale response after navigation is ignored", async () => {
    const app = await fixture();
    core.slowView = "Conversation";
    await app.press("id:inspect-Conversation");
    await app.press("id:inspect-Steps");
    await see(app, "turn completed · 1 requests · 1 tools");
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(text(app)).toContain("turn completed · 1 requests");
    expect(text(app)).not.toContain("assistant · 3 parts");
  });

  test("live state polls the head, refetches only on change, shows disconnected and stops when closed", async () => {
    const app = await fixture({ clock: {} });
    await see(app, "Live · updated");
    const heads = () => calls(app, "/inspect/head").length;
    const pages = () => calls(app).length;
    /** Move the manual clock in half-second steps until `done` holds (a poll's answer lands a step later). */
    const until = async (done: () => boolean) => { for (let i = 0; i < 20 && !done(); i++) await tick(app, 500); expect(done()).toBe(true); };
    const base = pages();
    await until(() => heads() === 1);
    await tick(app, 500);
    expect(pages()).toBe(base); // unchanged head: no refetch
    core.head = scenario.head_changed;
    await until(() => heads() === 2);
    await until(() => pages() === base + 1); // changed head: exactly one refetch
    await tick(app, 500);
    expect(pages()).toBe(base + 1);
    // Offline never looks current, and Refresh remains.
    core.headFails = true;
    await until(() => text(app).includes("Disconnected"));
    expect(text(app)).toContain("not current");
    expect(text(app)).not.toContain("● Live");
    expect(app.shows("id:inspect-refresh")).toBe(true);
    core.headFails = false;
    await app.press("id:inspect-refresh");
    await see(app, "● Live");
    // Paused: no polling.
    await app.press("id:inspect-live");
    await see(app, "Paused");
    const polls = heads();
    for (let i = 0; i < 8; i++) await tick(app, 1000);
    expect(heads()).toBe(polls);
    await app.press("id:inspect-live");
    // Closed: no requests at all.
    await app.keyboard.press("Escape");
    const total = app.requests().length;
    for (let i = 0; i < 8; i++) await app.clock!.fastForward(1000);
    expect(app.requests().length).toBe(total);
  });

  test("the manifest keys drive the same operations as the TUI", async () => {
    const app = await fixture();
    await app.keyboard.press("2");
    await see(app, "▸ assistant · 3 parts");
    await app.keyboard.press("j");
    await app.keyboard.press("Enter");
    await see(app, "Clipped · showing the first part of 30.5 KB · e loads the full record");
    await app.keyboard.press("e");
    await see(app, "Full record loaded");
    await app.keyboard.press("n");
    await see(app, "Older records · p newer · oldest retained");
    await app.keyboard.press("p");
    await see(app, "Newest records");
    await app.keyboard.press("]");
    await see(app, "ses_inspector → inspecting Scout (ses_child)");
    await app.keyboard.press("w");
    await see(app, "Paused · updated");
    await app.keyboard.press("i");
    await see(app, "Cache: lifetime provider reports");
  });

  test("j/k keeps the selected row in view in a long list", async () => {
    const records = Array.from({ length: 45 }, (_, n) => ({ id: `r${n}`, kind: "message", summary: `user · row number ${n}`, detail: "{}" }));
    const app = await open("./InspectorFixture.tsx", { width: 1280, height: 600, colorScheme: "dark", network: { "GET */api/session/*/inspect*": (r: { url: string }) => (r.url.includes("/inspect/head") ? { body: scenario.head } : { body: { ...scenario.conversation, more: false, next_before: null, omitted: [], records } }) } });
    await app.waitFor("id:inspector");
    await app.press("id:inspect-Conversation");
    await see(app, "row number 0");
    for (let n = 0; n < 40; n++) { await app.keyboard.press("j"); await new Promise((r) => setTimeout(r, 15)); }
    await new Promise((r) => setTimeout(r, 150));
    const top = () => Number((app.texts().find((t) => t.startsWith("scroll ")) ?? "scroll 0").slice(7));
    expect(top()).toBeGreaterThan(1000); // the list followed the selection down
    const down = top();
    for (let n = 0; n < 40; n++) { await app.keyboard.press("k"); await new Promise((r) => setTimeout(r, 15)); }
    await new Promise((r) => setTimeout(r, 150));
    expect(top()).toBeLessThan(down / 2); // and back up
  });

  test("narrow inspector keeps navigation and refresh reachable", async () => {
    const app = await fixture({ width: 390, height: 844 });
    expect(app.shows("id:inspect-refresh")).toBe(true);
    expect(app.shows("id:inspect-Raw")).toBe(true);
    await app.screenshot(`${SHOTS}/nsh-narrow.png`);
    await app.keyboard.press("Escape");
    expect(app.shows("id:inspector")).toBe(false);
  });
});
