// The Manager's logic (docs/manager-design.md 10.1 to 10.7): wording, the indicator's priority and levels, the
// speech queue, `/manager` parsing, cards and rows, and the controller's state machine against a stubbed Core.
import { describe, expect, test } from "nsh/test";
import {
  SpeechQueue, WORDS, managerHint, rowLine, spokenEnd, summaryRepeats, titleAdds, boardColumns, cardRows, columnOf, followLevel, footerHints, liveRow,
  moved, orbMode, parseCard, parseDevOrb, parseManagerSlash, printableOf, readingMs, rowCount, stateWord, syntheticLevel,
} from "./src/manager";
import type { Card } from "./src/manager";
import cards from "../interface/scenarios/manager-cards.json";
import day from "../interface/scenarios/manager-day.json";
import liveFailure from "../interface/scenarios/manager-live-failure.json";
import liveSilence from "../interface/scenarios/manager-live-silence.json";
import bargeScenario from "../interface/scenarios/manager-barge-in.json";
import dropdownScenario from "../interface/scenarios/manager-dropdown.json";
import previewScenario from "../interface/scenarios/manager-preview.json";
import {
  voiceEnabled, toggleManagerAudio, orbOn, dropdownOpen, dropdownInteractive, toggleManagerDropdown, closeManagerDropdown, audioRequested, submitField, openManagerSession, debugSessionId, backStackSize, call, callFocused, callMinimised, cancelTurn, card, cardCollapsed, cardFocused, closeField, dismissCard, engaged, fieldText, focusCard,
  handleEvent, idleCost, inCall, listening, managerContext, managerEscapeRule, managerTerm, minimise, answerConsent, consent, notice,
  openManagerContext, phase, pickRow, say, setStreamForTest, speaking, stopCall, stopSpeech, talk, minimised, managerError, modelPreview, managerDetailsText, detailsScroll, scrollDetails, connecting,
  liveVoice, stopManagerVoice, micMuted, toggleManagerMute, voiceConnected,
} from "./src/core/manager";
import { BRAILLE_WIDTH, brailleCell, brailleField, waveBrightness, waveColor, waveSpeed } from "./src/ui/manager/braille";
import { manifest } from "./src/gen/manifest";
import { connection, model } from "./src/store";

type Sent = { method: string; path: string; body: any };

/** A Core that records every request and answers the Manager's routes. */
function core(extra: Record<string, (body: any) => { status?: number; body?: unknown }> = {}) {
  const sent: Sent[] = [];
  (globalThis as any).fetch = async (input: any, init: any = {}) => {
    const url = String(typeof input === "string" ? input : input.url);
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(init.body) : undefined;
    sent.push({ method, path, body });
    const answer = extra[`${method} ${path}`]?.(body);
    if (answer) return new Response(JSON.stringify(answer.body ?? null), { status: answer.status ?? 200, headers: { "content-type": "application/json" } });
    if (method === "POST" && path === "/api/manager/start") return new Response(JSON.stringify({ call_id: "call_1", holder: "nsh-web-test", mode: "pipeline", model: "m" }), { status: 200 });
    if (path.endsWith("/message")) return new Response(JSON.stringify({ messages: [], more: false }), { status: 200 });
    if (path.endsWith("/turn")) return new Response(JSON.stringify({ turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } }), { status: 200 });
    return new Response("null", { status: 200 });
  };
  return sent;
}
const ev = (type: string, data: Record<string, unknown>) => ({ id: 1, time: 0, type, session_id: null, data: { call_id: "call_1", ...data } }) as any;
const settleAsync = async (ms = 30) => { await new Promise((resolve) => setTimeout(resolve, ms)); };

async function inACall() {
  const sent = core();
  setStreamForTest("stream_t");
  connection.set({ kind: "live" });
  await talk();
  return sent;
}
async function reset() {
  voiceEnabled.set(false); // text-only cases explicitly opt out of Voice
  await stopCall();
  managerContext.set(null);
  setStreamForTest(null);
}

describe("wording", () => {
  test("is the shared wording, exactly", () => {
    expect([WORDS.listening, WORDS.thinking, WORDS.muted, WORDS.reconnecting]).toEqual(["Listening", "Thinking", "Muted", "Reconnecting"]);
    expect([WORDS.needsYou, WORDS.running, WORDS.finished]).toEqual(["Needs you", "Running", "Finished"]);
    expect(WORDS.placeholder).toBe("Tell the Manager…");
    expect(WORDS.disabled).toBe("Manager is off. Turn it on in Settings › Manager.");
    expect(WORDS.noVoice).toBe("Voice isn't set up. Type instead, or set it up in Settings › Dictation.");
    expect(WORDS.gone).toBe("That session no longer exists.");
    expect(WORDS.via).toBe("via Manager");
    expect(WORDS.cut).toBe("… cut");
    expect(moved("tui-1")).toBe("Moved to the TUI");
    expect(moved("nsh-web-ab12")).toBe("Moved to NSH");
    expect(moved("cli")).toBe("Moved to another client");
  });
});

describe("the indicator", () => {
  test("listening beats speaking beats thinking beats settling; idle draws nothing", () => {
    const base = { listening: false, speaking: false, phase: "idle" as const, settling: false };
    expect(orbMode(base)).toBe(null);
    expect(orbMode({ ...base, settling: true })).toBe("settling");
    expect(orbMode({ ...base, settling: true, phase: "thinking" })).toBe("thinking");
    expect(orbMode({ ...base, phase: "thinking", speaking: true })).toBe("speaking");
    expect(orbMode({ ...base, phase: "replying", speaking: true, listening: true })).toBe("listening");
  });

  test("a level attacks in about 60 ms and releases in about 240 ms", () => {
    const up = followLevel(0, 1, 60);
    expect(up > 0.6 && up < 0.7).toBe(true);
    const down = followLevel(1, 0, 60);
    expect(down > 0.75 && down < 0.8).toBe(true);
    expect(followLevel(0.5, 0.5, 16)).toBe(0.5);
  });

  test("the indicator is braille text: ten cells, a few dots lit, moving waves", () => {
    const frame = brailleField(0, "listening", 0.5);
    expect(Array.from(frame).length).toBe(BRAILLE_WIDTH);
    const dots = Array.from(frame).map((c) => (c.codePointAt(0) ?? 0x2800) - 0x2800);
    expect(dots.every((bits) => bits >= 0 && bits < 0x100)).toBe(true);
    expect(dots.reduce((n, bits) => n + bits.toString(2).replaceAll("0", "").length, 0)).toBeGreaterThan(4);
    expect(brailleField(1, "listening", 0.5)).not.toBe(frame);
    expect(waveSpeed("speaking") > waveSpeed("settling")).toBe(true);
    expect(waveBrightness("speaking") > waveBrightness("settling")).toBe(true);
  });

  test("the speaker sets the wave colour and the voice level sets the height", () => {
    for (const scheme of ["dark", "light"] as const) {
      const l = waveColor("listening", "listening", 1, scheme), sp = waveColor("speaking", "speaking", 1, scheme);
      const idle = waveColor("settling", "settling", 1, scheme), th = waveColor("thinking", "thinking", 1, scheme);
      expect(l).not.toBe(sp);
      expect(l).not.toBe(idle);
      expect(sp).not.toBe(idle);
      expect(th).toBe(idle);
      const half = waveColor("thinking", "listening", 0.5, scheme);
      expect(half).not.toBe(th);
      expect(half).not.toBe(l);
    }
    const field = (mode: "listening" | "speaking" | "thinking", level: number) => Array.from({ length: BRAILLE_WIDTH }, (_, i) => brailleCell(3.1, mode, level, i)).join("");
    expect(field("listening", 0)).not.toBe(field("listening", 1));
    expect(field("speaking", 0)).not.toBe(field("speaking", 1));
    expect(field("thinking", 0)).toBe(field("thinking", 1));
  });

  test("the synthetic envelope stays soft and in range", () => {
    for (let t = 0; t < 20000; t += 137) {
      const level = syntheticLevel(t);
      expect(level >= 0.1 && level <= 0.6).toBe(true);
    }
  });

  test("a dev level is read only from an explicit setting and is otherwise inert", () => {
    expect(parseDevOrb(null)).toBe(null);
    expect(parseDevOrb("not json")).toBe(null);
    expect(parseDevOrb("{}")).toBe(null);
    expect(parseDevOrb('{"level": 3}')?.level).toBe(1);
    expect(parseDevOrb('{"level": 0.4, "voice": "simulate"}')?.simulate).toBe(true);
  });

  test("reading a sentence takes about as long as it takes to read", () => {
    expect(readingMs("Hi.")).toBe(2200);
    expect(readingMs("x".repeat(100))).toBe(6400);
    expect(readingMs("x".repeat(1000))).toBe(9000);
  });
});

describe("the speech queue", () => {
  test("plays a turn in seq order and turns in arrival order, dropping repeats", () => {
    const q = new SpeechQueue();
    q.push({ turn_id: "t1", seq: 1, text: "b" });
    q.push({ turn_id: "t1", seq: 0, text: "a" });
    q.push({ turn_id: "t2", seq: 0, text: "c" });
    q.push({ turn_id: "t1", seq: 0, text: "a again" });
    expect([q.next()?.text, q.next()?.text, q.next()?.text, q.next()]).toEqual(["a", "b", "c", undefined]);
  });
  test("never waits for a missing seq", () => {
    const q = new SpeechQueue();
    q.push({ turn_id: "t1", seq: 3, text: "d" });
    expect(q.next()?.text).toBe("d");
  });
});

describe("/manager", () => {
  test("alone starts, `stop` stops, anything else is one typed line", () => {
    expect(parseManagerSlash("")).toEqual({ kind: "start" });
    expect(parseManagerSlash("  ")).toEqual({ kind: "start" });
    expect(parseManagerSlash("stop")).toEqual({ kind: "stop" });
    expect(parseManagerSlash("Stop")).toEqual({ kind: "stop" });
    expect(parseManagerSlash("what needs me?")).toEqual({ kind: "say", text: "what needs me?" });
    expect(parseManagerSlash("stop the build")).toEqual({ kind: "say", text: "stop the build" });
  });
});

describe("keys the field and rows take", () => {
  test("printable keystrokes become characters; other keys are not text", () => {
    expect(printableOf("a")).toBe("a");
    expect(printableOf("shift+a")).toBe("A");
    expect(printableOf("shift+/")).toBe("?");
    expect(printableOf("3")).toBe("3");
    expect(printableOf("mod+a")).toBe(null);
    expect(printableOf("enter")).toBe(null);
    expect(printableOf("space")).toBe(null);
  });
});

describe("cards", () => {
  const all = (cards as { cards: { name: string; card: Card }[] }).cards;
  const board = all.find((c) => c.name === "board")!.card as Extract<Card, { type: "board" }>;

  test("rows are numbered across the whole card, in column order", () => {
    expect(cardRows(board).map((r) => r.n)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(rowCount(board)).toBe(6);
    expect(rowCount(all.find((c) => c.name === "note")!.card)).toBe(0);
  });

  test("a row draws from the live store when the client has the session, and from the payload otherwise", () => {
    const row = board.columns.needs_you[0];
    expect(liveRow(row, null)).toEqual(row);
    const live = liveRow(row, { title: "Renamed", state: "working" });
    expect(live.title).toBe("Renamed");
    expect(live.state).toBe("working");
    expect(live.n).toBe(1);
    expect(live.handle).toBe("auth-refactor");
  });

  test("a session that finishes moves column while the user watches, and keeps its number", () => {
    const columns = boardColumns(board, (row) => (row.n === 1 ? { state: "idle" } : null));
    expect(columns.needs_you.map((r) => r.n)).toEqual([2]);
    expect(columns.finished.map((r) => r.n)).toEqual([1, 6]);
    expect(columnOf("needs_approval")).toBe("needs_you");
    expect(columnOf("working")).toBe("running");
    expect(columnOf("archived")).toBe("finished");
    expect(stateWord("needs_answer")).toBe("needs an answer");
  });

  test("the footer names only the hints that apply, in order", () => {
    expect(footerHints(board, { focused: true })).toBe("1-6 open · space talk · alt+shift+d dismiss");
    expect(footerHints(board, { focused: true, dismiss: "⌥⇧⌘D" })).toBe("1-6 open · space talk · ⌥⇧⌘D dismiss");
    expect(footerHints(all.find((c) => c.name === "note")!.card, { focused: true })).toBe("space talk · alt+shift+d dismiss");
    expect(footerHints(all.find((c) => c.name === "question")!.card, { focused: true })).toBe("space talk · alt+shift+d dismiss");
    expect(footerHints(board, { focused: false })).toBe("");
  });

  test("a card the client cannot read is not drawn", () => {
    expect(parseCard(null)).toBe(null);
    expect(parseCard({ id: "x", type: "bogus" })).toBe(null);
    expect(parseCard({ id: "x", type: "sessions" })).toEqual({ id: "x", type: "sessions", focused: true, rows: [] });
    expect(parseCard({ id: "x", type: "note", markdown: "hi", focused: false })?.focused).toBe(false);
  });
});

describe("the controller", () => {
  test("starts nothing and costs nothing at rest", async () => {
    expect(inCall()).toBe(false);
    expect(engaged()).toBe(false);
    expect(managerTerm("manager.in_call")).toBe(false);
    expect(managerTerm("manager.listening")).toBe(false);
    expect(managerTerm("manager.field")).toBe(false);
    expect(managerTerm("manager.card_focused")).toBe(false);
    expect(idleCost()).toEqual({ frame: false, timers: false });
  });

  test("talk starts a call, and with no voice set up opens the typed field and says why", async () => {
    const sent = await inACall();
    expect(sent.find((s) => s.path === "/api/manager/start")?.body.stream_id).toBe("stream_t");
    expect(inCall()).toBe(true);
    expect(fieldText()).toBe("");
    expect(notice()).toBe(WORDS.noVoice);
    expect(listening()).toBe(false);
    await reset();
    expect(inCall()).toBe(false);
    expect(fieldText()).toBe(null);
    expect(idleCost().frame).toBe(false);
    expect(sent.some((s) => s.path === "/api/manager/stop")).toBe(true);
  });

  test("stopping the live voice from the indicator keeps the call and the typed field", async () => {
    const sent = await inACall();
    liveVoice.set(true);
    await stopManagerVoice();
    await settleAsync();
    expect(sent.some((r) => r.path === "/api/manager/live/stop")).toBe(true);
    expect(liveVoice()).toBe(false);
    expect(voiceEnabled()).toBe(false);
    expect(inCall()).toBe(true);
    await reset();
  });

  test("Mute only silences the microphone of a connected voice; Disconnect clears it", async () => {
    const sent = await inACall();
    toggleManagerMute();
    expect(micMuted()).toBe(false);
    liveVoice.set(true);
    expect(managerTerm("manager.voice")).toBe(true);
    toggleManagerMute();
    expect(micMuted()).toBe(true);
    expect(inCall()).toBe(true);
    expect(sent.some((r) => r.path === "/api/manager/live/stop")).toBe(false);
    toggleManagerMute();
    expect(micMuted()).toBe(false);
    toggleManagerMute();
    expect(voiceConnected()).toBe(true);
    await toggleManagerAudio();
    await settleAsync();
    expect(sent.some((r) => r.path === "/api/manager/live/stop")).toBe(true);
    expect(voiceConnected()).toBe(false);
    expect(liveVoice()).toBe(false);
    expect(inCall()).toBe(true);
    await reset();
  });

  test("a manager that is off shows the toast and starts nothing", async () => {
    core({ "POST /api/manager/start": () => ({ status: 409, body: { error: "off", code: "manager_disabled" } }) });
    setStreamForTest("stream_t");
    connection.set({ kind: "live" });
    await talk();
    expect(inCall()).toBe(false);
    expect(fieldText()).toBe(null);
    setStreamForTest(null);
  });

  test("events for a call this client does not hold are ignored", async () => {
    await inACall();
    handleEvent(ev("manager.card", { call_id: "call_other", card: { id: "c", type: "note", focused: true, markdown: "x" } }));
    handleEvent(ev("manager.state", { call_id: "call_1", active: true, phase: "thinking", holder: "me" }));
    expect(card()).toBe(null);
    expect(phase()).toBe("thinking");
    await reset();
  });

  test("a call that moves clears everything and says where", async () => {
    await inACall();
    handleEvent(ev("manager.card", { card: { id: "c", type: "note", focused: true, markdown: "x" } }));
    expect(cardFocused()).toBe(true);
    handleEvent(ev("manager.state", { call_id: "call_2", active: true, phase: "idle", holder: "tui-9" }));
    expect(inCall()).toBe(false);
    expect(card()).toBe(null);
    expect(notice()).toBe("Moved to the TUI");
    await settleAsync();
    setStreamForTest(null);
  });

  test("Core ending the call and announcing another right after it is a move too", async () => {
    await inACall();
    handleEvent(ev("manager.state", { call_id: "call_1", active: false, phase: "idle", holder: "me" }));
    expect(inCall()).toBe(false);
    handleEvent(ev("manager.state", { call_id: "call_7", active: true, phase: "idle", holder: "tui-3" }));
    expect(notice()).toBe("Moved to the TUI");
    setStreamForTest(null);
  });

  test("a focused card takes keys; an unfocused one takes none until it is focused locally", async () => {
    await inACall();
    // A minimised call leaves the app's keys alone, so this checks the card's
    // own engagement rather than the call taking every key.
    minimise();
    expect(callMinimised()).toBe(true);
    handleEvent(ev("manager.card", { card: { id: "c", type: "approval", focused: false, session_id: "s", handle: "h", approval_id: "a", summary: "run", detail: "ls", clipped: false } }));
    expect(card()?.type).toBe("approval");
    expect(cardFocused()).toBe(false);
    expect(engaged()).toBe(false);
    expect(managerTerm("manager.card_focused")).toBe(false);
    focusCard();
    expect(cardFocused()).toBe(true);
    expect(engaged()).toBe(true);
    handleEvent(ev("manager.card", { card: null }));
    expect(card()).toBe(null);
    expect(cardFocused()).toBe(false);
    expect(cardCollapsed()).toBe(false);
    await reset();
  });

  test("dismissing a card tells Core which one", async () => {
    const sent = await inACall();
    handleEvent(ev("manager.card", { card: { id: "card_9", type: "note", focused: true, markdown: "x" } }));
    dismissCard();
    expect(card()).toBe(null);
    await settleAsync();
    expect(sent.find((s) => s.path === "/api/manager/card/dismiss")?.body).toEqual({ stream_id: "stream_t", card_id: "card_9" });
    await reset();
  });

  test("a spoken or typed line goes to Core with its source", async () => {
    const sent = await inACall();
    await say("what's going on?", "speech");
    await say("list sidebar", "typed");
    const says = sent.filter((s) => s.path === "/api/manager/say").map((s) => s.body);
    expect(says).toEqual([
      { stream_id: "stream_t", text: "what's going on?", source: "speech" },
      { stream_id: "stream_t", text: "list sidebar", source: "typed" },
    ]);
    await reset();
  });

  test("picking a row opens the session; a row that is gone says so and the card stays", async () => {
    const sent = await inACall();
    model.sessions.set("s_auth01", { id: "s_auth01", project_id: "p", directory: "/", title: "Refactor", agent: "build", parent_id: null, revision: 1, created: 1, updated: 1 } as any);
    const board = (cards as any).cards.find((c: any) => c.name === "board").card;
    handleEvent(ev("manager.card", { card: board }));
    pickRow(2);
    expect(notice()).toBe(WORDS.gone);
    expect(card()?.type).toBe("board");
    pickRow(1);
    expect(card()).toBe(null);
    await settleAsync(50);
    expect(sent.some((s) => s.path === "/api/session/s_auth01")).toBe(true);
    expect(sent.some((s) => s.path === "/api/manager/card/dismiss")).toBe(true);
    model.sessions.delete("s_auth01");
    await reset();
  });

  test("a move the Manager asks for is answered, and back follows the history", async () => {
    const sent = await inACall();
    for (const id of ["s_a", "s_b"]) {
      model.sessions.set(id, { id, project_id: "p", directory: "/", title: id, agent: "build", parent_id: null, revision: 1, created: 1, updated: 1 } as any);
    }
    handleEvent(ev("manager.navigate", { request_id: "r1", session: "s_a" }));
    await settleAsync(150);
    handleEvent(ev("manager.navigate", { request_id: "r2", session: "s_b" }));
    await settleAsync(150);
    handleEvent(ev("manager.navigate", { request_id: "r3", back: true, session: "s_zzz" }));
    await settleAsync(150);
    const results = sent.filter((s) => s.path === "/api/manager/navigate/result").map((s) => s.body);
    expect(results.map((r) => [r.request_id, r.ok])).toEqual([["r1", true], ["r2", true], ["r3", true]]);
    expect(backStackSize()).toBe(1);
    // A session that does not exist is refused with a reason, and the caption says so.
    handleEvent(ev("manager.navigate", { request_id: "r4", session: "s_missing" }));
    await settleAsync(150);
    const last = sent.filter((s) => s.path === "/api/manager/navigate/result").at(-1)!.body;
    expect(last).toEqual({ stream_id: "stream_t", request_id: "r4", ok: false, reason: WORDS.gone });
    model.sessions.delete("s_a"); model.sessions.delete("s_b");
    await reset();
  });

  test("speech is shown as the caption when it cannot be played, in order, and never blocks", async () => {
    core({ "POST /api/voice/speak": () => ({ status: 503, body: { error: "no tts", code: "tts_unavailable" } }) });
    setStreamForTest("stream_t");
    connection.set({ kind: "live" });
    await talk();
    const { managerCaption } = await import("./src/core/manager");
    handleEvent(ev("manager.speech", { turn_id: "t1", seq: 1, text: "Second.", urgent: false }));
    handleEvent(ev("manager.speech", { turn_id: "t1", seq: 0, text: "First.", urgent: false }));
    await settleAsync(60);
    expect(managerCaption()).toBe("First.");
    expect(speaking()).toBe(false);
    await reset();
  });

  test("a privacy notice is shown before the first call: esc sends nothing, enter accepts and then starts", async () => {
    const notice = { provider: "copilot", version: "v1", required: true, accepted_at: null, notice: "Your words go to the model." };
    const sent = core({ "GET /api/manager": () => ({ body: { enabled: true, call: null, consent: notice } }) });
    setStreamForTest("stream_t");
    connection.set({ kind: "live" });
    const first = talk();
    await settleAsync();
    expect(consent()?.notice).toBe("Your words go to the model.");
    expect(inCall()).toBe(false);
    await answerConsent(false);
    await first;
    expect(consent()).toBe(null);
    expect(sent.some((s) => s.path === "/api/manager/start" || s.path === "/api/manager/consent")).toBe(false);
    const second = talk();
    await settleAsync();
    await answerConsent(true);
    await second;
    expect(sent.find((s) => s.path === "/api/manager/consent")?.body).toEqual({ accept: true, version: "v1" });
    expect(inCall()).toBe(true);
    await reset();
  });

  test("a 409 consent_required from start asks for the notice too", async () => {
    let answered = false;
    const sent = core({
      "POST /api/manager/start": () => answered ? { body: { call_id: "call_1", holder: "h" } } : { status: 409, body: { error: "consent", code: "consent_required", consent: { provider: "p", version: "v2", required: true, notice: "Notice." } } },
      "POST /api/manager/consent": () => { answered = true; return { body: null }; },
    });
    setStreamForTest("stream_t");
    connection.set({ kind: "live" });
    const t = talk();
    await settleAsync();
    expect(consent()?.version).toBe("v2");
    await answerConsent(true);
    await t;
    expect(inCall()).toBe(true);
    expect(sent.filter((s) => s.path === "/api/manager/start").length).toBe(2);
    await reset();
  });

  test("an unfocused card nobody engages folds into its caption after 12 seconds; focusing it brings it back", async () => {
    await inACall();
    handleEvent(ev("manager.card", { card: { id: "c", type: "approval", focused: false, session_id: "s", handle: "h", approval_id: "a", summary: "run", detail: "ls", clipped: false } }));
    expect(cardCollapsed()).toBe(false);
    await settleAsync(12_300);
    expect(cardCollapsed()).toBe(true);
    expect(card()?.type).toBe("approval");
    focusCard();
    expect(cardCollapsed()).toBe(false);
    expect(cardFocused()).toBe(true);
    await reset();
  });

  test("a row says the why or the state, and the title only when it adds to the handle", () => {
    const row = { n: 1, session_id: "s", handle: "flaky-test", title: "Flaky test", state: "needs_answer" };
    expect(rowLine(row)).toBe("needs an answer");
    expect(rowLine({ ...row, title: "Find the flaky test" })).toBe("needs an answer · Find the flaky test");
    expect(rowLine({ ...row, why: 'matched "flaky"' })).toBe('matched "flaky"');
    expect(titleAdds("migration", "Migration")).toBe(false);
    expect(titleAdds("migration", undefined)).toBe(false);
  });

  test("an approval summary that only repeats its detail is hidden", () => {
    expect(summaryRepeats("run: alembic upgrade head", "alembic upgrade head")).toBe(true);
    expect(summaryRepeats("Wants to run a shell command", "alembic upgrade head")).toBe(false);
  });
});

describe("GPT-Live (audio socket to Core, stubbed)", () => {
  function stubBrowser() {
    const log: string[] = [];
    const sockets: any[] = [];
    const worklets: any[] = [];
    const starts: number[] = [];
    const track = { stop: () => log.push("track") };
    const stream = { getTracks: () => [track] };
    const analyser = { fftSize: 1024, connect() {}, getFloatTimeDomainData: (into: Float32Array) => into.fill(0) };
    const g = globalThis as any;
    const saved = { AudioContext: g.AudioContext, AudioWorkletNode: g.AudioWorkletNode, WebSocket: g.WebSocket, Blob: g.Blob, URL: g.URL, navigator: g.navigator };
    g.navigator = { mediaDevices: { getUserMedia: async () => stream } };
    g.Blob = class {};
    g.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
    g.AudioContext = class {
      currentTime = 0; destination = {}; audioWorklet = { addModule: async () => {} };
      createAnalyser() { return { ...analyser }; } createGain() { return { gain: { value: 1 }, connect() {} }; }
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      createBuffer() { return { copyToChannel() {} }; } createBufferSource() { return { buffer: null, onended: null, connect() {}, start(at: number) { starts.push(at); }, stop() {} }; }
      async resume() {} async close() { log.push("context"); }
    };
    g.AudioWorkletNode = class { port = { onmessage: null as any }; constructor() { worklets.push(this); } connect() {} disconnect() {} };
    g.WebSocket = class {
      binaryType = ""; readyState = 1; onopen: any = null; onclose: any = null; onerror: any = null; onmessage: any = null; sent: unknown[] = [];
      constructor(public url: string) { sockets.push(this); setTimeout(() => this.onopen?.(), 0); }
      send(d: unknown) { this.sent.push(d); } close() { log.push("socket"); }
    };
    return { log, sockets, worklets, starts, restore: () => Object.assign(g, saved) };
  }

  test("live mode starts Core's call, opens the audio socket for this stream, and hangs up with the call", async () => {
    const browser = stubBrowser();
    try {
      const sent = core({
        "POST /api/manager/start": () => ({ body: { call_id: "call_1", holder: "nsh-web-test", mode: "live", model: "m" } }),
        "POST /api/manager/live/start": () => ({ body: { call_id: "call_1", audio: { rate: 24000 } } }),
      });
      setStreamForTest("stream_t");
      connection.set({ kind: "live" });
      await talk();
      expect(sent.find((s) => s.path === "/api/manager/live/start")?.body).toEqual({ stream_id: "stream_t" });
      expect(browser.sockets[0].url).toContain("/api/manager/live/audio?stream_id=stream_t");
      expect(listening()).toBe(true);
      const socket = browser.sockets[0];
      for (let i = 0; i < liveSilence.silent_packets; i++) {
        socket.onmessage({ data: new Int16Array(liveSilence.samples_per_packet).buffer });
        browser.worklets[0].port.onmessage({ data: new Int16Array(liveSilence.samples_per_packet).fill(8000).buffer });
      }
      expect(socket.sent.filter((d: unknown) => d instanceof ArrayBuffer)).toHaveLength(liveSilence.silent_packets);
      socket.onmessage({ data: new Int16Array(liveSilence.samples_per_packet).fill(bargeScenario.speaker_rms * 32768).buffer });
      browser.worklets[0].port.onmessage({ data: new Int16Array(liveSilence.samples_per_packet).fill(bargeScenario.microphone_rms * 32768).buffer });
      expect(socket.sent.filter((d: unknown) => d instanceof ArrayBuffer)).toHaveLength(liveSilence.silent_packets + 1);
      // CUSHION_START is 120 ms (the planner starts 120 ms ahead); packets chain a frame apart.
      expect(Math.abs(browser.starts[0] - 0.12)).toBeLessThan(0.0001);
      expect(Math.abs(browser.starts[1] - browser.starts[0] - 0.02)).toBeLessThan(0.0001);
      await stopCall();
      for (const part of ["track", "socket", "context"]) expect(browser.log).toContain(part);
      expect(listening()).toBe(false);
      expect(idleCost().timers).toBe(false);
    } finally {
      browser.restore();
      await reset();
    }
  });

  test("a refused start keeps the call open for typing and opens no microphone socket", async () => {
    const browser = stubBrowser();
    try {
      core({
        "POST /api/manager/start": () => ({ body: { call_id: "call_1", holder: "nsh-web-test", mode: "live", model: "m" } }),
        "POST /api/manager/live/start": () => ({ status: 409, body: { error: liveFailure.error, code: liveFailure.code } }),
      });
      setStreamForTest("stream_t");
      connection.set({ kind: "live" });
      await talk();
      expect(inCall()).toBe(true);
      expect(browser.sockets).toHaveLength(0);
      expect(listening()).toBe(false);
      expect(dropdownOpen()).toBe(true);
      expect(notice()).not.toBe(WORDS.noVoice);
      expect(managerError()?.code).toBe("live_forbidden");
      closeField();
      await talk();
      expect(dropdownOpen()).toBe(true);
      expect(managerError()?.code).toBe("live_forbidden");
    } finally {
      browser.restore();
      await reset();
    }
  });
});

describe("Manager dropdown and independent audio", () => {
  test("Voice defaults on and starts immediately on Manager open; off preserves typing", async () => {
    await reset(); voiceEnabled.set(true); connection.set({kind:"live"}); setStreamForTest("stream_t");
    const sent=core(); toggleManagerDropdown(); expect(connecting()).toBe(true); await settleAsync();
    expect(sent.find(s=>s.path==="/api/manager/start")?.body.audio).toBe(true);
    await toggleManagerAudio(); expect(voiceEnabled()).toBe(false); expect(inCall()).toBe(true);
    expect(sent.some(s=>s.path==="/api/manager/live/stop")).toBe(true);
    expect(sent.some(s=>s.path==="/api/manager/stop")).toBe(false); await reset();
  });
  test("closing an unused dropdown releases its orb loop", async () => {
    await reset(); toggleManagerDropdown(); expect(orbOn()).toBe(true);
    toggleManagerDropdown(); expect(dropdownOpen()).toBe(false); expect(orbOn()).toBe(false);
  });
  test("opening the dropdown makes no request; first typed send does not start audio", async () => {
    await reset(); const sent = core(); connection.set({kind:"live"}); setStreamForTest("stream_t");
    toggleManagerDropdown(); expect(dropdownOpen()).toBe(true); expect(sent.length).toBe(0);
    fieldText.set(dropdownScenario.input); submitField(); await settleAsync();
    expect(sent.find(s=>s.path==="/api/manager/start")?.body.audio).toBe(dropdownScenario.start_audio);
    expect(sent.find(s=>s.path==="/api/manager/say")?.body.text).toBe(dropdownScenario.input);
    expect(audioRequested()).toBe(false); expect(fieldText()).toBe(""); await reset();
  });
  test("Esc closes only the dropdown in every work phase", async () => {
    for (const work of ["idle","thinking","replying"] as const) {
      await reset(); const sent = await inACall(); phase.set(work);
      expect(managerEscapeRule()).toBe("manager_dropdown");
      expect(inCall()).toBe(true); expect(dropdownOpen()).toBe(false);
      expect(sent.some(s=>s.path==="/api/manager/stop")).toBe(false);
    }
    await reset(); expect(managerEscapeRule()).toBe(null);
  });
  test("background audio leaves the session's Escape available", async () => {
    await reset(); await inACall(); closeManagerDropdown();
    expect(callFocused()).toBe(false); expect(managerEscapeRule()).toBe(null); expect(inCall()).toBe(true);
    const rule = manifest.keymap.escape.find(r=>r.id==="interrupt");
    expect(rule?.when).toEqual(["session.busy","!manager.focused"]); await reset();
  });
  test("auto notification opens a card without stealing text input", async () => {
    await reset(); await inACall(); closeManagerDropdown();
    handleEvent(ev("manager.card",{card:{id:"c",type:"note",focused:false,markdown:"Notification"}}));
    expect(dropdownOpen()).toBe(true); expect(dropdownInteractive()).toBe(false); expect(engaged()).toBe(false); await reset();
  });
  test("debug session opens inside Nexus without starting a call", async () => {
    await reset(); connection.set({kind:"live"});
    const sent=core({"POST /api/manager/session":()=>({body:{id:"ses_manager",project_id:"p",kind:"manager",title:"Manager",status:"idle"}})});
    await openManagerSession(); expect(debugSessionId()).toBe("ses_manager"); expect(model.selected?.sessionId).toBe("ses_manager");
    expect(inCall()).toBe(false); expect(audioRequested()).toBe(false);
    expect(sent.some(s=>s.path==="/api/manager/start")).toBe(false); model.select(null); await reset();
  });
});

describe("call mode: the four actions and the close phrase", () => {
  test("stop speaking, cancel the turn, dismiss the card and end the call are separate", async () => {
    const sent = await inACall();
    closeField();
    speaking.set(true);
    sent.length = 0;
    stopSpeech();
    expect(speaking()).toBe(false);
    expect(inCall()).toBe(true);
    expect(sent.some((s) => s.path === "/api/manager/cancel" || s.path === "/api/manager/stop")).toBe(false);
    sent.length = 0;
    cancelTurn();
    await settleAsync();
    expect(sent.some((s) => s.path === "/api/manager/cancel")).toBe(true);
    expect(sent.some((s) => s.path === "/api/manager/stop")).toBe(false);
    handleEvent(ev("manager.card", { card: { id: "c", type: "note", focused: true, markdown: "x" } }));
    sent.length = 0;
    dismissCard();
    await settleAsync();
    expect(sent.some((s) => s.path === "/api/manager/card/dismiss")).toBe(true);
    expect(inCall()).toBe(true);
    sent.length = 0;
    await stopCall();
    expect(inCall()).toBe(false);
    expect(sent.some((s) => s.path === "/api/manager/stop")).toBe(true);
    setStreamForTest(null);
  });

  test("'that's all' is the spoken End, and is never sent as words", () => {
    expect(spokenEnd("that's all")).toBe(true);
    expect(spokenEnd(" That’s all. ")).toBe(true);
    expect(spokenEnd("that's all for now")).toBe(true);
    expect(spokenEnd("end the call")).toBe(true);
    expect(spokenEnd("that's all the files")).toBe(false);
    expect(spokenEnd("open the second one")).toBe(false);
  });

  test("the hint names the state's keys and how to end, with the client's labels", () => {
    const key = (id: string) => ({ "manager.talk": "talk-key", "manager.stop": "end-key", "manager.speech_stop": "stop-key", "manager.turn_cancel": "cancel-key", "manager.card_dismiss": "dismiss-key" })[id] ?? "";
    const base = { minimised: false, field: false, listening: false, card: false, cardMax: 0, speaking: false, thinking: false };
    expect(managerHint(base, key)).toBe("space talk · esc end");
    expect(managerHint({ ...base, field: true }, key)).toBe("enter send · esc end");
    expect(managerHint({ ...base, listening: true }, key)).toBe("space send · type to type · esc end");
    expect(managerHint({ ...base, speaking: true }, key)).toBe("space talk · stop-key stop · esc end");
    expect(managerHint({ ...base, thinking: true }, key)).toBe("space talk · cancel-key cancel · esc end");
    expect(managerHint({ ...base, card: true, cardMax: 6 }, key)).toBe("1-6 open · space talk · dismiss-key dismiss · esc end");
    expect(managerHint({ ...base, minimised: true }, key)).toBe("talk-key talk · esc end");
    expect(managerHint({ ...base, minimised: true, card: true }, key)).toBe("talk-key open · esc end");
    expect(managerHint({ ...base, consent: true }, key)).toBe("enter accept · esc cancel");
  });
});


describe("Manager model excerpt", () => {
  test("receives the shared current excerpt and tool, and clears it on hangup", async () => {
    core(); setStreamForTest("stream_t"); connection.set({ kind: "live" });
    await talk();
    handleEvent(ev("manager.preview", { call_id: call()!.id, preview: previewScenario.preview }));
    expect(modelPreview()).toEqual(previewScenario.preview);
    await stopCall();
    expect(modelPreview()).toBe(null);
  });
});


describe("Manager details modal and immediate connection", () => {
  test("the full current context and tool remain available inside the scrollable modal", async () => {
    core(); setStreamForTest("stream_t"); connection.set({ kind: "live" }); await talk();
    handleEvent(ev("manager.preview", {call_id:call()!.id, preview:previewScenario.long_preview}));
    expect(managerDetailsText()).toContain("Context detail line 1");
    expect(managerDetailsText()).toContain("Final context detail remains available.");
    expect(managerDetailsText()).toContain("read_session");
    scrollDetails(200); expect(detailsScroll()).toBe(200);
    await stopCall(); expect(detailsScroll()).toBe(0);
  });
  test("connecting shows immediately and a cancelled pending start cannot reopen the call", async () => {
    core(); setStreamForTest("stream_t"); connection.set({ kind:"live" });
    const original = (globalThis as any).fetch;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    (globalThis as any).fetch = async (...args: any[]) => { await gate; return original(...args); };
    const pending = talk();
    expect(connecting()).toBe(true); expect(callFocused()).toBe(true);
    expect(managerDetailsText()).toContain("Connecting…");
    await stopCall(); release(); await pending;
    expect(call()).toBe(null); expect(connecting()).toBe(false);
    (globalThis as any).fetch = original;
  });
});


test("scripted day applies the shared cards and dismissal", async () => {
  const sent = await inACall();
  await say(day.overview, "speech");
  handleEvent(ev("manager.card", { card: day.excerpt_card }));
  expect((card() as any)?.markdown).toBe(day.session_answer);
  handleEvent(ev("manager.card", { card: day.approval_card }));
  expect((card() as any)?.detail).toBe(day.command);
  dismissCard();
  await settleAsync();
  expect(sent.find(s => s.path === "/api/manager/say")?.body.text).toBe(day.overview);
  expect(sent.find(s => s.path === "/api/manager/card/dismiss")?.body.card_id).toBe(day.approval_card.id);
  await reset();
});
