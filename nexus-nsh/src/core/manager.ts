// The Manager's controller (docs/manager-design.md 10.1 to 10.7): the call, the exchange, voice in and out,
// cards, navigation and the engaged keys. The UI (ui/manager/*) only reads these signals.
//
// Idle is free: with no call and nothing on screen there are no timers, no animation frames and no
// key bindings from here. The frame loop runs only while the orb is visible; the engaged keys are
// bound only while the Manager is engaged.
import { Storage, effect, keymap, signal } from "nsh";
import { ApiError, request } from "./transport";
import { managerHooks } from "./manager-hooks";
import type { CoreEvent } from "./api";
import {
  Card, CARD_COLLAPSE_MS, CAPTION_QUIET_MS, DevOrb, ManagerError, ORB_MS, OrbMode, PRINTABLE_KEYS, Phase, Row,
  SpeechItem, SpeechQueue, WORDS, followLevel, moved, orbMode, parseCard, parseDevOrb, parseManagerSlash, printableOf, readingMs, rowCount, spokenEnd, syntheticLevel,
} from "../manager";
import {
  config, connection, currentClientView, notificationSoundsOn, drawer, isNarrow, leftOpen, leftTab, live, loadFiles, model, phone, selectProject,
  selectSession, sync, bump, syncView, toast, viewReporter,
} from "../store";
import type { ClientView } from "./view";
import { playbackLevel, speakText, stopSpeaking } from "../speech";
import { dictationLevel, dictationPhase } from "../voice-state";
import { playNotificationCue } from "../cues";
import { gptLiveLevels, gptLiveOpen, interruptGptLive, startGptLive, stopGptLive } from "./gptlive";

/**
 * The microphone path (the client's dictation): injected by the app, so this module loads without the
 * native crate (tests) and so the Manager reuses exactly what the composer's dictation uses.
 */
export interface VoiceIn {
  available(): boolean;
  start(insert: (transcript: string) => void): void;
  stop(): Promise<void>;
  cancel(): void;
}
let voiceIn: VoiceIn = { available: () => false, start() {}, stop: async () => {}, cancel() {} };
const microphoneAvailable = () => voiceIn.available();
const startDictation = (insert: (transcript: string) => void) => voiceIn.start(insert);
const stopDictation = () => voiceIn.stop();
const cancelDictation = () => voiceIn.cancel();

// --- state ---------------------------------------------------------------------------------------------

/** The call this client holds, or null. `holder` is the name Core gave it. */
export const call = signal<{ id: string; holder: string; mode?: string } | null>(null);
/** The call is open but out of the way: a small orb in the corner, and the app has its keys back. */
export const minimised = signal(false);
export const connecting = signal(false);
export const dropdownOpen = signal(false);
export const dropdownInteractive = signal(false);
export const audioRequested = signal(false);
export const voiceEnabled = signal(true);
/** The GPT-Live voice is opening or open. Its indicator hover offers Stop; the call itself stays. */
export const liveVoice = signal(false);
export const debugSessionId = signal<string | null>(null);
export function toggleManagerDropdown() {
  if (dropdownOpen()) { closeManagerDropdown(); return; }
  dropdownOpen.set(true); dropdownInteractive.set(true);
  minimised.set(!dropdownOpen());
  if (dropdownOpen()) { if (fieldText() === null) fieldText.set(""); begin({ orb: true }); }
  if (voiceEnabled() && !audioRequested()) void start(true);
}
export function closeManagerDropdown() {
  dropdownOpen.set(false); dropdownInteractive.set(false); minimised.set(true);
  if (!call() && !connecting()) { orbOn.set(false); exchange.set(false); stopLoop(); }
}
export async function toggleManagerAudio() {
  await setManagerAudio(!voiceEnabled());
}
/** The Voice toggle and the indicator's Stop share this one path. */
export async function setManagerAudio(on: boolean) {
  voiceEnabled.set(on);
  if (!on) {
    audioRequested.set(false); hangUpLive(); skipSpeech(); cancelListening();
    if (call()) await request("POST", "/api/manager/live/stop", { body: { stream_id: streamId } }).catch(() => {});
  } else if (!audioRequested()) { await talk(); }
}
/** Stop the live voice from its header indicator: the control call and typed field stay. */
export async function stopManagerVoice() {
  if (!liveVoice()) return;
  await setManagerAudio(false);
}

let debugRefreshAt = 0;
let debugRefreshTimer: ReturnType<typeof setTimeout> | null = null;
async function refreshManagerDebug() {
  if (!debugSessionId() || model.selected?.sessionId !== debugSessionId()) return;
  if (Date.now() - debugRefreshAt < 750) {
    if (!debugRefreshTimer) debugRefreshTimer = setTimeout(() => { debugRefreshTimer = null; void refreshManagerDebug(); }, 750 - (Date.now() - debugRefreshAt));
    return;
  }
  debugRefreshAt = Date.now();
  try {
    const session = await request<any>("GET", "/api/manager/session");
    if (!session?.id) return;
    model.sessions.set(session.id, session); bump(); debugSessionId.set(session.id);
    if (model.selected?.sessionId !== session.id) await selectSession(session.id); else await sync.loadSelected();
  } catch {}
}
export const detailsScroll = signal(0);
export function scrollDetails(delta: number) { detailsScroll.set(detailsScroll() + delta); }
/** The header button's read-only view of the Manager's hidden context. */
export const modelPreview = signal<{ source: string; lines: string[]; clipped: boolean; text?: string; details_clipped?: boolean; tool: { name: string; arguments: string; status: string; clipped: boolean } | null } | null>(null);
export const managerContext = signal<{ title: string; text: string } | null>(null);
export const phase = signal<Phase>("idle");
export const managerError = signal<ManagerError | null>(null);
export const listening = signal(false);
export const speaking = signal(false);
/** The recording is being turned into words: the orb breathes as it does when thinking. */
export const transcribing = signal(false);
/** The typed field's text, or null when it is closed. */
export const fieldText = signal<string | null>(null);
export const card = signal<Card | null>(null);
/** The user focused a card that arrived unfocused (a click or the shortcut). Local only. */
export const cardUnlocked = signal(false);
/** An unfocused card that nobody engaged for 12 s: only its caption remains. */
export const cardCollapsed = signal(false);
export const userCaption = signal("");
export const managerCaption = signal("");
/** A one-off line: "Moved to <holder>", "That session no longer exists.", a voice notice. */
export const notice = signal("");
/** Something to show (a caption, an orb): true from a user's call or a spoken line until it goes quiet. */
export const exchange = signal(false);
/** The orb is wanted: the user called for the Manager and the exchange has not settled. */
export const orbOn = signal(false);
/** Bumped once per animation frame while the orb is visible; the canvas reads it to redraw. */
export const frame = signal(0);
/** The caption pill is fading out after the exchange went quiet. */
export const captionGone = signal(false);
/** The height of the composer dock (its top edge is where the orb sits); the composer reports it. */
export const dockHeight = signal(0);

let streamId: string | null = null;
export const hasStream = () => streamId !== null;

// --- terms the keymap reads --------------------------------------------------------------------------------

export const inCall = () => call() !== null;
export const callFocused = () => dropdownOpen() && dropdownInteractive();
export const callMinimised = () => inCall() && minimised();
export const cardFocused = () => { const c = card(); return !minimised() && !!c && !cardCollapsed() && (c.focused || cardUnlocked()); };
export const fieldOpen = () => fieldText() !== null;
export const consentOpen = () => consent() !== null;
export const engaged = () => callFocused() || consentOpen();

export function managerTerm(name: string): boolean {
  switch (name) {
    case "manager.in_call": return inCall() || connecting();
    case "manager.focused": return callFocused();
    case "manager.minimised": return callMinimised();
    case "manager.listening": return listening();
    case "manager.speaking": return speaking();
    case "manager.field": return fieldOpen();
    case "manager.card_focused": return cardFocused();
    case "manager.engaged": return engaged();
    default: return false;
  }
}

const reduced = () => !!config()?.values?.ui?.reduced_motion;
export const reducedMotion = reduced;

// --- the orb's visual state (plain data: read by the canvas each frame, not tracked) -----------------------------

export const visual = {
  level: 0, presence: 0, blend: 1, mode: "settling" as OrbMode, from: "settling" as OrbMode,
  last: 0, startedAt: 0, running: false, still: false,
};
let dev: DevOrb | null = null;
let frameHandle: number | null = null;

function targetLevel(mode: OrbMode, ts: number): number {
  if (visual.still) return 0;
  if (!dev && gptLiveOpen()) { const l = gptLiveLevels(); return mode === "listening" ? l.local : mode === "speaking" ? l.remote : 0; }
  if (mode === "listening") return dev ? dev.level : Math.min(1, dictationLevel() * 3.4);
  if (mode === "speaking") return dev ? dev.level : (playbackLevel() ?? syntheticLevel(ts));
  return 0;
}

function currentMode(): OrbMode {
  if (connecting()) return "thinking";
  const settling = !orbOn() && visual.presence > 0;
  return orbMode({ listening: listening(), speaking: speaking(), phase: phase() !== "idle" || transcribing() ? "thinking" : "idle", settling }) ?? "settling";
}

function tick(ts: number) {
  frameHandle = null;
  const dt = visual.last ? Math.min(64, ts - visual.last) : 16;
  visual.last = ts;
  const on = orbOn();
  visual.presence = Math.min(1, Math.max(0, visual.presence + (on ? dt : -dt) / ORB_MS));
  const mode = currentMode();
  if (mode !== visual.mode) { visual.from = visual.mode; visual.mode = mode; visual.blend = 0; }
  visual.blend = Math.min(1, visual.blend + dt / 200);
  visual.still = !!managerError() || (inCall() && !live());
  visual.level = followLevel(visual.level, targetLevel(mode, ts), dt);
  frame.update((n) => n + 1);
  if (on || visual.presence > 0) frameHandle = requestAnimationFrame(tick);
  else { visual.running = false; visual.last = 0; visual.level = 0; }
}

function startLoop() {
  if (reduced() || visual.running) return;
  visual.running = true;
  visual.startedAt = Date.now();
  visual.last = 0;
  frameHandle = requestAnimationFrame(tick);
}

/** The call ended: no frame is left scheduled. */
function stopLoop() {
  if (frameHandle !== null) cancelAnimationFrame(frameHandle);
  frameHandle = null;
  visual.running = false; visual.last = 0; visual.level = 0; visual.presence = 0;
}

/** True while an animation frame is scheduled: the zero-idle-cost test reads this. */
export const loopRunning = () => visual.running;

// --- quiet: when an exchange is over -----------------------------------------------------------------------

let quietTimer: ReturnType<typeof setTimeout> | null = null;
let collapseTimer: ReturnType<typeof setTimeout> | null = null;
let clearTimer: ReturnType<typeof setTimeout> | null = null;
const clearTimers = () => {
  for (const t of [quietTimer, collapseTimer, clearTimer]) if (t) clearTimeout(t);
  quietTimer = collapseTimer = clearTimer = null;
};

/** The exchange went quiet: the orb settles and the caption fades. */
function settle() {
  quietTimer = null;
  orbOn.set(false);
  captionGone.set(true);
  if (clearTimer) clearTimeout(clearTimer);
  clearTimer = setTimeout(() => {
    clearTimer = null;
    userCaption.set(""); managerCaption.set(""); notice.set("");
    captionGone.set(false);
    exchange.set(false);
  }, ORB_MS + 60);
  if (reduced()) visual.presence = 0;
}

function isQuiet(): boolean {
  return exchange() && !listening() && !speaking() && !transcribing() && phase() === "idle" && !playing() && !fieldOpen() && !card() && !managerError();
}

function begin(opts: { orb: boolean }) {
  if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
  captionGone.set(false);
  exchange.set(true);
  if (opts.orb) { orbOn.set(true); startLoop(); }
}

// --- the call -----------------------------------------------------------------------------------------------

let starting = false;
let startEpoch = 0;
let held: CoreEvent[] = [];

/** The privacy notice Core asks the user to accept before the first call (null when none is pending). */
export const consent = signal<{ provider: string; version: string; notice: string } | null>(null);
let consentWaiter: ((accepted: boolean) => void) | null = null;

/** Show the notice and wait for enter (accept) or esc (cancel). Nothing is sent before it is accepted. */
function askConsent(c: { provider: string; version: string; notice: string }): Promise<boolean> {
  consent.set(c);
  begin({ orb: false });
  return new Promise<boolean>((resolve) => { consentWaiter = resolve; });
}
export async function answerConsent(accept: boolean) {
  const c = consent();
  const waiter = consentWaiter;
  consentWaiter = null;
  consent.set(null);
  if (!c || !waiter) return;
  if (!accept) { exchange.set(false); waiter(false); return; }
  try {
    await request("POST", "/api/manager/consent", { body: { accept: true, version: c.version, ...(c.provider === "codex" ? { provider: "codex" } : {}) } });
    waiter(true);
  } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); waiter(false); }
}

/**
 * The Manager's part of the Esc chain (docs/manager-design.md 10.6): the
 * context modal closes, consent cancels, and an open call (focused or
 * minimised) ends. It never minimises and never reaches the session interrupt.
 * Shared with commands.ts.
 */
export function managerEscapeRule(): string | null {
  if (managerContext()) { managerContext.set(null); return "manager_context"; }
  if (consentOpen()) { void answerConsent(false); return "manager_consent"; }
  if (dropdownOpen()) { closeManagerDropdown(); return "manager_dropdown"; }
  return null;
}

let startupWaiters: ((ok: boolean) => void)[] = [];
async function start(audio = true): Promise<boolean> {
  if (!streamId) { toast("warning", "Core is not connected: the Manager is unavailable."); return false; }
  if (starting) return new Promise<boolean>((resolve) => startupWaiters.push(resolve));
  audioRequested.set(audio);
  const epoch = ++startEpoch;
  starting = true; held = [];
  connecting.set(true); minimised.set(false);
  visual.presence = 1; visual.mode = "thinking"; visual.from = "thinking"; visual.blend = 1;
  begin({ orb: true });
  if (notificationSoundsOn()) playNotificationCue("manager_start");
  try {
    // Before a call: if the privacy notice is required and not yet accepted, ask first.
    try {
      type Notice = { provider: string; version: string; required: boolean; accepted_at: unknown; notice: string };
      const status = await request<{ consent?: Notice; live_consent?: Notice | null }>("GET", "/api/manager");
      for (const c of [status?.consent, ...(audio ? [status?.live_consent] : [])]) {
        if (c?.required && !c.accepted_at && !(await askConsent(c))) return false;
      }
    } catch { /* Core answers start itself */ }
    if (epoch !== startEpoch) return false;
    const r = await request<{ call_id: string; holder: string; mode?: string; preview?: any }>("POST", "/api/manager/start", { body: { stream_id: streamId, client_id: viewReporter.clientId, audio } });
    if (epoch !== startEpoch) {
      if (!call() && !starting) void request("POST", "/api/manager/stop", { body: { stream_id: streamId } }).catch(() => {});
      return false;
    }
    call.set({ id: r.call_id, holder: r.holder ?? "", mode: r.mode });
    modelPreview.set(r.preview ?? null);
    lost = null;
    const buffered = held; held = [];
    starting = false;
    for (const e of buffered) handleEvent(e);
    if (r.mode === "live" && audioRequested()) await openLive(r.call_id);
    return true;
  } catch (e) {
    if (e instanceof ApiError && e.code === "consent_required" && e.body?.consent && !consent()) {
      starting = false;
      if (await askConsent(e.body.consent)) return start(audio);
      return false;
    }
    if (e instanceof ApiError && e.code === "manager_disabled") toast("info", WORDS.disabled);
    else toast("error", e instanceof ApiError ? e.message : String(e));
    return false;
  } finally {
    if (epoch === startEpoch) {
      starting = false; held = []; connecting.set(false);
      for (const resolve of startupWaiters.splice(0)) resolve(call() !== null);
      if (!call() && !consent()) { orbOn.set(false); exchange.set(false); }
    }
  }
}

let liveTimer: ReturnType<typeof setInterval> | null = null;

/**
 * GPT-Live mode: the browser opens the call and OpenAI's voice listens and speaks. The call stays up if this fails, so
 * the typed field still works and the reason is shown. The orb follows the call's own levels.
 */
async function openLive(callId: string) {
  begin({ orb: true });
  liveVoice.set(true);
  try {
    await request("POST", "/api/manager/live/start", { body: { stream_id: streamId } });
    if (call()?.id !== callId || !audioRequested()) { hangUpLive(); return; }
    await startGptLive(streamId ?? "", (why) => { hangUpLive(); managerError.set({ code: "live_lost", message: why, fix: "Start the call again." }); });
  } catch (e) {
    liveVoice.set(false);
    const message = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
    managerError.set({ code: e instanceof ApiError && e.code ? e.code : "live_failed", message, fix: "The call is still open for typing; start it again to retry the voice." });
    return;
  }
  if (call()?.id !== callId || !audioRequested()) { hangUpLive(); return; }
  if (notificationSoundsOn()) playNotificationCue("live_connected");
  listening.set(true);
  let lastSpeechAt = 0;
  liveTimer = setInterval(() => {
    const l = gptLiveLevels();
    if (l.remote > 0.03) lastSpeechAt = Date.now();
    const talking = Date.now() - lastSpeechAt < 350;
    if (talking !== speaking()) { speaking.set(talking); listening.set(!talking); }
  }, 100);
}

function hangUpLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  liveVoice.set(false);
  stopGptLive();
  speaking.set(false); listening.set(false);
}

/** Clear everything the Manager shows or holds, without telling Core. */
function endLocal() {
  ++startEpoch;
  starting = false; held = [];
  for (const resolve of startupWaiters.splice(0)) resolve(false);
  connecting.set(false); detailsScroll.set(0); audioRequested.set(false);
  hangUpLive();
  micOwned = false;
  if (dictationPhase() !== "idle") cancelDictation();
  skipSpeech();
  clearTimers();
  call.set(null);
  dropdownOpen.set(false); dropdownInteractive.set(false);
  minimised.set(false);
  managerContext.set(null);
  modelPreview.set(null);
  phase.set("idle"); managerError.set(null); notice.set("");
  listening.set(false); speaking.set(false); transcribing.set(false);
  fieldText.set(null);
  card.set(null); cardUnlocked.set(false); cardCollapsed.set(false);
  userCaption.set(""); managerCaption.set(""); captionGone.set(false);
  orbOn.set(false); exchange.set(false);
  stopLoop();
  backStack.length = 0;
}

export async function stopCall(): Promise<void> {
  const id = streamId;
  const had = call();
  lost = null;
  endLocal();
  if (!had || !id) return;
  try { await request("POST", "/api/manager/stop", { body: { stream_id: id } }); } catch { /* the call ends with the stream anyway */ }
}

function movedAway(holder: string) {
  endLocal();
  begin({ orb: false });
  notice.set(moved(holder));
}

// --- events ----------------------------------------------------------------------------------------------------

function onEvent(e: CoreEvent) {
  if (starting) { held.push(e); return; }
  handleEvent(e);
}

/** The call this client held that Core ended (not by this client's stop): a new call right after it means "moved". */
let lost: { id: string; at: number } | null = null;

export function handleEvent(e: CoreEvent) {
  void refreshManagerDebug();
  if (e.type === "manager.session") return;
  const d = e.data ?? {};
  const mine = call()?.id;
  if (!mine) {
    // Core ends the old call, then announces the new one: that pair is the move.
    if (e.type === "manager.state" && lost && d.active !== false && d.call_id !== lost.id && Date.now() - lost.at < 5000) {
      lost = null;
      movedAway(String(d.holder ?? "another client"));
    }
    return;
  }
  if (e.type === "manager.state") {
    if (d.call_id !== mine) { movedAway(String(d.holder ?? "another client")); return; }
    if (d.active === false) { lost = { id: mine, at: Date.now() }; endLocal(); return; }
    phase.set(d.phase === "thinking" || d.phase === "replying" ? d.phase : "idle");
    managerError.set(d.error ?? null);
    if ("preview" in d) modelPreview.set(d.preview ?? null);
    if (d.error) { begin({ orb: false }); }
    return;
  }
  if (d.call_id !== mine) return;
  switch (e.type) {
    case "manager.preview": modelPreview.set(d.preview ?? null); return;
    case "manager.caption": {
      const text = String(d.text ?? "");
      if (d.role === "user") { userCaption.set(text); begin({ orb: false }); }
      else if (text) { managerCaption.set(text); begin({ orb: false }); }
      return;
    }
    case "manager.speech": if (!audioRequested()) { managerCaption.set(String(d.text ?? "")); return; } enqueue({ turn_id: String(d.turn_id ?? ""), seq: Number(d.seq ?? 0), text: String(d.text ?? ""), urgent: !!d.urgent, proactive: !!d.proactive }); return;
    case "manager.card": showCard(d.card == null ? null : parseCard(d.card, dev?.unfocused)); return;
    case "manager.navigate": void navigate(d); return;
  }
}

function showCard(next: Card | null) {
  if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null; }
  cardUnlocked.set(false); cardCollapsed.set(false);
  card.set(next);
  if (!next) return;
  dropdownOpen.set(true);
  if (next.focused) {
    // A card that follows the user's words keeps the orb up with it.
    begin({ orb: true });
  } else {
    begin({ orb: false });
    // Nobody engaged it: it folds into its caption, and the item stays pending.
    collapseTimer = setTimeout(() => {
      collapseTimer = null;
      if (card() && !cardFocused()) cardCollapsed.set(true);
    }, CARD_COLLAPSE_MS);
  }
}

/** Dismiss the card (the shortcut or the scrim): it closes and Core is told, so the next turn's delta says so. */
export function dismissCard() {
  const c = card();
  if (!c) return;
  card.set(null); cardUnlocked.set(false); cardCollapsed.set(false);
  if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null; }
  if (streamId && call()) void request("POST", "/api/manager/card/dismiss", { body: { stream_id: streamId, card_id: c.id } }).catch(() => {});
}

/** The shortcut or a click on a collapsed or unfocused card: it takes focus (locally). */
export function focusCard() {
  dropdownOpen.set(true); dropdownInteractive.set(true);
  if (!card()) return;
  minimised.set(false);
  cardUnlocked.set(true); cardCollapsed.set(false);
  if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null; }
  begin({ orb: true });
}

// --- the live store: rows draw from it -----------------------------------------------------------------------------

/** A session's title and unified state from this client's own store, or null when it does not have it. */
export function liveSession(id: string): { title?: string; state: string } | null {
  const s = model.sessions.get(id);
  if (!s) return null;
  const tree = model.tree(id);
  const asks = (list: Iterable<{ session_id: string }>) => [...list].some((x) => tree.includes(x.session_id));
  let state = "idle";
  if (asks(model.approvals.values())) state = "needs_approval";
  else if (asks(model.questions.values())) state = "needs_answer";
  else if (tree.some((t) => model.running.has(t) || model.sessions.get(t)?.status === "busy")) state = "working";
  else if (model.failedSessions.has(id)) state = "failed";
  else if (s.archived) state = "archived";
  return { title: s.title, state };
}

/** Pick a row: open the session as a click would and close the card; if it is gone, say so and stay. */
export function pickRow(n: number) {
  const row = rowOf(n);
  if (!row) return;
  const known = model.sessions.has(row.session_id);
  if (!known) { notice.set(WORDS.gone); begin({ orb: true }); return; }
  dismissCard();
  void selectSession(row.session_id);
}

export function rowOf(n: number): Row | undefined {
  const c = card();
  if (!c) return undefined;
  const rows = c.type === "board" ? [...c.columns.needs_you, ...c.columns.running, ...c.columns.finished] : c.type === "sessions" ? c.rows : [];
  return rows.find((row) => row.n === n);
}

// --- navigation (manager.navigate) -----------------------------------------------------------------------------------

const backStack: ClientView[] = [];

async function navigate(d: { request_id: string; project?: string; session?: string; panel?: string | null; back?: boolean }) {
  const requestId = d.request_id;
  const answer = async (ok: boolean, reason?: string) => {
    if (!streamId) return;
    if (ok) { syncView(); await viewReporter.settled(); }
    try { await request("POST", "/api/manager/navigate/result", { body: { stream_id: streamId, request_id: requestId, ok, ...(reason ? { reason } : {}) } }); } catch { /* Core times out */ }
  };
  // A navigation closes any open card.
  card.set(null); cardUnlocked.set(false); cardCollapsed.set(false);
  let target: { project?: string | null; session?: string | null; panel?: string | null } = { project: d.project, session: d.session, panel: d.panel };
  if (d.back) {
    const previous = backStack.pop();
    if (previous) target = { project: previous.project, session: previous.session, panel: previous.panel };
  } else {
    backStack.push(currentClientView());
    if (backStack.length > 8) backStack.shift();
  }
  if ((target.panel === "files" || target.panel === "file") && phone) { await answer(false, "The file explorer is not available on this device."); return; }
  if (target.session && !model.sessions.has(target.session)) { notice.set(WORDS.gone); await answer(false, WORDS.gone); return; }
  try {
    if (target.session) await selectSession(target.session);
    else if (target.project) {
      if (!model.projects.has(target.project)) { await answer(false, "That project no longer exists."); return; }
      selectProject(target.project);
    }
    if (target.panel === "files" || target.panel === "file") {
      leftTab.set("files");
      if (isNarrow()) drawer.set("left"); else leftOpen.set(true);
      void loadFiles();
    } else if (target.panel === null && leftTab() === "files") leftTab.set("sessions");
  } catch (e) { await answer(false, e instanceof Error ? e.message : String(e)); return; }
  await answer(true);
}

/** For tests: the views the Manager moved this client through. */
export const backStackSize = () => backStack.length;

// --- voice in ------------------------------------------------------------------------------------------------------

let micOwned = false;
let recorded = false;
let delivered = false;

async function voiceReady(): Promise<boolean> {
  if (dev?.simulate) return true;
  if (!microphoneAvailable()) return false;
  try {
    const status = await request<{ stt?: { state?: string } }>("GET", "/api/voice");
    return status?.stt?.state === "ready" || status?.stt?.state === "loaded";
  } catch { return false; }
}

/** The mic could not open: the typed field takes over and the caption says why. */
function voiceUnavailable() {
  micOwned = false;
  listening.set(false); transcribing.set(false);
  openField("");
  notice.set(WORDS.noVoice);
}

async function openMic() {
  if (!(await voiceReady())) { voiceUnavailable(); return; }
  if (dictationPhase() !== "idle" && !dev?.simulate) cancelDictation();
  listening.set(true);
  recorded = false; delivered = false;
  if (dev?.simulate) return;
  micOwned = true;
  startDictation((transcript) => {
    delivered = true;
    micOwned = false;
    listening.set(false);
    if (spokenEnd(transcript)) { void stopCall(); return; }
    void say(transcript, "speech");
  });
}

export async function sendListening() {
  if (!listening()) return;
  if (dev?.simulate) {
    listening.set(false);
    transcribing.set(true);
    void say(dev.say ?? "what's going on?", "speech");
    return;
  }
  if (dictationPhase() !== "recording") { cancelListening(); return; }
  transcribing.set(true);
  micOwned = false;
  delivered = false;
  await stopDictation();
  if (!delivered) { transcribing.set(false); listening.set(false); }
}

/** Drop the recording; nothing is sent. The call stays open. Esc ends the whole call. */
export function cancelListening() {
  micOwned = false;
  if (dictationPhase() !== "idle") cancelDictation();
  listening.set(false); transcribing.set(false);
}

/** `manager.minimise`: out of the way, not ended. A recording and the field close. Esc ends the call. */
export function minimise() {
  closeManagerDropdown();
  if (!call() || consent()) return;
  cancelListening();
  closeField();
  minimised.set(true);
}

// --- voice out -------------------------------------------------------------------------------------------------------

const queue = new SpeechQueue();
export const playing = signal(false);
let playToken = 0;
let mutedTurns = new Set<string>();
let wake: (() => void) | null = null;

function enqueue(item: SpeechItem) {
  if (!item.text.trim() || mutedTurns.has(item.turn_id)) return;
  queue.push(item);
  void drain();
}

async function drain() {
  if (playing()) return;
  playing.set(true);
  const token = playToken;
  // Sentences that arrive in one flush are ordered by seq before the first one plays.
  await Promise.resolve();
  try {
    for (;;) {
      if (token !== playToken) break;
      const item = queue.next();
      if (!item) break;
      await playOne(item, token);
    }
  } finally {
    if (token === playToken) { playing.set(false); speaking.set(false); }
  }
}

async function playOne(item: SpeechItem, token: number) {
  // A line with no user utterance behind it is proactive: spoken and shown as a caption, no orb.
  // A proactive line has no user utterance behind it: caption alone, no orb unless the user called.
  begin({ orb: item.proactive ? orbOn() : orbOn() || exchangeOwned });
  managerCaption.set(item.text);
  if (dev?.simulate) {
    speaking.set(true);
    await nap(Math.max(1800, readingMs(item.text)));
    return;
  }
  try {
    speaking.set(true);
    await speakText(item.text.slice(0, 1200), streamId ?? "");
    speaking.set(false);
  } catch (e) {
    speaking.set(false);
    if (token !== playToken) return;
    // Speech failed or TTS is not installed: the text is the caption for as long as it takes to read.
    await nap(readingMs(item.text));
  }
}

/** A sleep that a barge-in or stop can cut short. */
function nap(ms: number): Promise<void> {
  return new Promise<void>((resolve) => {
    const t = setTimeout(() => { wake = null; resolve(); }, ms);
    wake = () => { clearTimeout(t); wake = null; resolve(); };
  });
}

/** Drop everything queued and stop what is playing. */
function skipSpeech() {
  playToken++;
  queue.clear();
  playing.set(false);
  speaking.set(false);
  wake?.();
  void stopSpeaking().catch(() => {});
}

/** `manager.speech_stop`: the speech stops; the turn's work goes on. Its later sentences are muted. */
export function stopSpeech() {
  for (const turn of currentTurns) mutedTurns.add(turn);
  skipSpeech();
}
const currentTurns = new Set<string>();

/** `manager.turn_cancel`: stop the speech and ask Core to cancel the turn, distinct from ending the call. */
export function cancelTurn() {
  if (!call()) return;
  if (gptLiveOpen()) interruptGptLive();
  skipSpeech();
  if (streamId) void request("POST", "/api/manager/cancel", { body: { stream_id: streamId } }).catch(() => {});
}

// --- activity: client truth for Core --------------------------------------------------------------------------------------

let reported = { listening: false, speaking: false };
let activityTimer: ReturnType<typeof setTimeout> | null = null;
function reportActivity() {
  if (activityTimer) clearTimeout(activityTimer);
  activityTimer = setTimeout(() => {
    activityTimer = null;
    const next = { listening: listening(), speaking: speaking() };
    if (!call() || !streamId || (next.listening === reported.listening && next.speaking === reported.speaking)) return;
    reported = next;
    void request("POST", "/api/manager/activity", { body: { stream_id: streamId, ...next } }).catch(() => {});
  }, 60);
}

// --- talking ---------------------------------------------------------------------------------------------------------------

let exchangeOwned = false;

/** `manager.talk`: the shortcut, or a click on the orb. */
export async function talk() {
  dropdownOpen.set(true); dropdownInteractive.set(true); audioRequested.set(true); voiceEnabled.set(true);
  if (!live()) { toast("warning", "Core is not connected: the Manager is unavailable."); return; }
  const fresh = !call();
  if (fresh) {
    if (!(await start(true))) return;
  }
  // Talking brings a minimised call back to the stage. An unfocused or
  // waiting card is opened first, as the shortcut always has.
  minimised.set(false);
  if (!fresh && card() && !cardFocused()) { focusCard(); return; }
  exchangeOwned = true;
  if (call()?.mode === "live") {
    if (!gptLiveOpen()) { await start(true); return; }
    // GPT-Live listens by itself: the shortcut only interrupts what it is saying.
    begin({ orb: true });
    interruptGptLive();
    if (streamId) void request("POST", "/api/manager/cancel", { body: { stream_id: streamId } }).catch(() => {});
    return;
  }
  if (listening()) { await sendListening(); return; }
  if (!fresh && (phase() !== "idle" || speaking() || playing())) {
    // Barge-in: stop speech, cancel the turn if it is safe, open the mic.
    skipSpeech();
    if (streamId) void request("POST", "/api/manager/cancel", { body: { stream_id: streamId } }).catch(() => {});
  }
  userCaption.set(""); managerCaption.set(""); notice.set("");
  currentTurns.clear(); mutedTurns = new Set();
  begin({ orb: true });
  await openMic();
}

/** One utterance: typed, or the transcript of speech. */
export async function say(text: string, source: "typed" | "speech") {
  try { await sayWords(text, source); } finally { transcribing.set(false); }
}

async function sayWords(text: string, source: "typed" | "speech") {
  const words = text.trim();
  if (!words) return;
  if (!live()) { toast("warning", "Core is not connected: the Manager is unavailable."); return; }
  if (!call()) { if (!(await start(source === "speech"))) return; }
  minimised.set(false);
  exchangeOwned = true;
  skipSpeech();
  currentTurns.clear(); mutedTurns = new Set();
  userCaption.set(words); managerCaption.set(""); notice.set("");
  begin({ orb: true });
  try {
    const r = await request<{ turn_id?: string }>("POST", "/api/manager/say", { body: { stream_id: streamId, text: words, source } });
    if (r?.turn_id) currentTurns.add(r.turn_id);
  } catch (e) {
    if (e instanceof ApiError && e.code === "consent_required" && e.body?.consent && !consent()) {
      if (await askConsent(e.body.consent)) { await sayWords(text, source); }
      return;
    }
    toast("error", e instanceof ApiError ? e.message : String(e));
  }
}

/**
 * `manager.open_session`: the header's Manager button. The Manager has no timeline, so its own session (the current
 * episode) opens in the inspector at the Requests level, which shows the full context the model is given.
 * On the web the host proxy adds Core's token to every call, so the page only needs a placeholder.
 */
export async function openManagerSession() {
  if (!live()) { toast("warning", "Core is not connected: the Manager is unavailable."); return; }
  try {
    const session = await request<any>("POST", "/api/manager/session");
    model.sessions.set(session.id, session); bump(); debugSessionId.set(session.id);
    closeManagerDropdown(); await selectSession(session.id);
  } catch (e) {
    toast("error", e instanceof ApiError ? e.message : "Could not open the Manager's session.");
  }
}

/**
 * `manager.context`: the header's Manager button. Opens a read-only modal with
 * the current hidden context (episodes, recap, state snapshots and deltas, and
 * the recent log). Core formats the text so both clients read the same words.
 */
export async function openManagerContext() {
  if (!live()) { toast("warning", "Core is not connected: the Manager is unavailable."); return; }
  try {
    const body = await request<{ title?: string; document?: string }>("GET", "/api/manager/context");
    managerContext.set({
      title: body?.title || "Manager context",
      text: body?.document || "The Manager has no context yet.",
    });
  } catch (e) {
    toast("error", e instanceof ApiError ? e.message : "Could not read the Manager's context.");
  }
}

/** `manager.start`: a call opens quietly, with nothing drawn and no mic; the orb comes with the shortcut. */
export async function startQuietly() {
  if (!live()) { toast("warning", "Core is not connected: the Manager is unavailable."); return; }
  if (!call()) {
    await start(false);
    if (call()) minimised.set(true);
  }
}

/** `/manager`, `/manager stop`, `/manager <text>`. */
export async function managerSlash(arg: string) {
  const parsed = parseManagerSlash(arg);
  if (parsed.kind === "stop") { await stopCall(); return; }
  if (parsed.kind === "say") { await say(parsed.text, "typed"); return; }
  await startQuietly();
}

// --- the typed field --------------------------------------------------------------------------------------------------------

export function openField(initial: string) {
  dropdownOpen.set(true); dropdownInteractive.set(true);
  if (listening()) cancelListening();
  minimised.set(false);
  begin({ orb: true });
  fieldText.set(initial);
}
export function closeField() { fieldText.set(null); }
export function submitField() {
  const text = fieldText() ?? "";
  fieldText.set("");
  if (text.trim()) void say(text, "typed");
}

// --- the engaged keys ------------------------------------------------------------------------------------------------------------

/** Keys the Manager takes only while engaged and no field is open. Returns the unbind function. */
function bindEngagedKeys(): () => void {
  const offs: (() => void)[] = [];
  const bind = (key: string, fn: (keystroke: string) => void) => {
    try { offs.push(keymap.bind(key, (e: { keystroke: string }) => fn(e.keystroke), { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  };
  bind("pageup", () => scrollDetails(-200));
  bind("pagedown", () => scrollDetails(200));
  bind("space", () => { if (listening()) void sendListening(); else void talk(); });
  bind("enter", () => { if (listening()) void sendListening(); });
  for (const key of PRINTABLE_KEYS) {
    bind(key, (stroke) => {
      const typed = printableOf(stroke);
      if (typed === null) return;
      if (cardFocused() && /^[1-9]$/.test(typed) && rowCount(card()) > 0) {
        if (rowOf(Number(typed))) pickRow(Number(typed));
        return;
      }
      openField(typed);
    });
  }
  return () => offs.forEach((off) => off());
}

export function managerDetailsText(): string {
  const sections: string[] = [];
  if (connecting()) sections.push("Connecting…");
  const error = managerError(); if (error) sections.push([error.message, error.fix].filter(Boolean).join("\n"));
  if (userCaption()) sections.push(`You\n${userCaption()}`);
  if (managerCaption()) sections.push(`Manager\n${managerCaption()}`);
  const p = modelPreview();
  if (p) {
    sections.push(`Model context · ${p.source}\n${p.details_clipped ? "Earlier content omitted.\n" : ""}${p.text ?? p.lines.join("\n")}`);
    if (p.tool) sections.push(`Tool · ${p.tool.name} · ${p.tool.status}\n${p.tool.arguments}${p.tool.clipped ? "\nArguments clipped; full details in Manager context." : ""}`);
  }
  return sections.join("\n\n") || "Ready";
}

// --- install -------------------------------------------------------------------------------------------------------------------

async function restore() {
  if (!call()) return;
  try {
    const s = await request<{ call: null | { call_id: string; holder: string; phase: Phase; error: ManagerError | null; card: unknown; preview?: any } }>("GET", "/api/manager");
    const mine = call();
    if (!mine) return;
    if (!s.call) { endLocal(); return; }
    if (s.call.call_id !== mine.id) { movedAway(String(s.call.holder)); return; }
    phase.set(s.call.phase ?? "idle");
    managerError.set(s.call.error ?? null);
    modelPreview.set(s.call.preview ?? null);
    if (s.call.card && !card()) showCard(parseCard(s.call.card));
  } catch { /* the next event or reconnect restores it */ }
}

/** Wire the Manager into the app: events, streams, the engaged keys, the frame loop, the activity report. */
export function installManager(voice?: VoiceIn) {
  if (voice) voiceIn = voice;
  managerHooks.event = onEvent;
  managerHooks.connected = (id) => { streamId = id; void restore(); };
  managerHooks.closed = (id) => { if (streamId === id) streamId = null; };
  void Storage.getItem("nexus.manager.dev").then((raw) => {
    dev = parseDevOrb(raw);
    // Dev only: a card shown without a call, for screenshots and the shared card tests.
    if (dev?.card) { call.set({ id: "preview", holder: "preview" }); showCard(parseCard(dev.card)); }
    // Dev only: the live voice's header indicator and its Stop tooltip, for screenshots and tests.
    if (dev?.live) { call.set({ id: "preview-live", holder: "preview" }); liveVoice.set(true); }
  }).catch(() => {});
  // Keys: bound only while engaged and no field is open.
  effect(() => {
    if (!engaged() || fieldOpen()) return;
    return bindEngagedKeys();
  });
  // The mic can end by itself (denied, no model, a limit): follow it.
  effect(() => {
    const phaseNow = dictationPhase();
    if (!micOwned) return;
    if (phaseNow === "recording") recorded = true;
    else if (phaseNow === "idle" && listening() && !delivered) {
      if (recorded) { listening.set(false); transcribing.set(false); micOwned = false; }
      else voiceUnavailable();
    }
  });
  // Quiet: five seconds after an exchange ends, the orb settles and the caption fades.
  effect(() => {
    const quiet = isQuiet();
    if (quietTimer) { clearTimeout(quietTimer); quietTimer = null; }
    if (quiet) quietTimer = setTimeout(settle, CAPTION_QUIET_MS);
  });
  // The privacy notice: enter accepts, esc cancels (see escape() in commands.ts).
  effect(() => {
    if (!consentOpen()) return;
    try { const off = keymap.bind("enter", () => void answerConsent(true), { when: "always" }); return off; } catch { return; }
  });
  effect(() => { listening(); speaking(); call(); reportActivity(); });
  effect(() => { if (orbOn() && !reducedMotion()) startLoop(); });
  effect(() => { if (!orbOn()) exchangeOwned = false; });
  void connection;
}

/** For tests and the zero-idle check: everything the Manager keeps alive. */
export function idleCost(): { frame: boolean; timers: boolean } {
  return { frame: visual.running || frameHandle !== null, timers: !!(quietTimer || collapseTimer || clearTimer || activityTimer || liveTimer) };
}

/** For tests: put the controller in a known state without a stream. */
export function setStreamForTest(id: string | null) { streamId = id; }
