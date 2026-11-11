import { fileWatchStatus } from "./file-watch";
import { receiveBtw } from "./btw";
import { mobileClient } from "./mobile";
// Reactive app state: the model (a mutable cache, versioned by `rev`), connection, banners,
// alerts (toasts + banner history), and view state (layout, tabs, centre view, overlays).
import { Storage, computed, platform, signal, windowSize } from "nsh";
import { Model, normalizeSession } from "./core/reduce";
import { Banner, ConnectionState, MESSAGE_PAGE, Sync, SyncHost } from "./core/sync";
import { Compat, Info, Message, Session } from "./core/api";
import { ApiError, apiBase, localCore, request, setApiBase } from "./core/transport";
import { ClientView, ViewReporter, sameClientView } from "./core/view";
import type { ProjectMcpSummary } from "./project-mcp";
import { emptyGlobalToastState, globalEventToast } from "./skills-mcp";
import { receiveVoiceProgress } from "./voice-state";
import { managerHooks } from "./core/manager-hooks";
import { inspectorHooks } from "./core/inspector-hooks";
import { playNotificationCue } from "./cues";
import { splashDismiss, splashFinish, splashReach, splashStart } from "./ui/splash";

export type CentreView = "session" | "new_session" | "settings" | "editor" | "file";
export type Toast = { id: number; level: "success" | "info" | "warning" | "error"; text: string; at: number };

export const model = new Model();
/** A tool, skill or server in the context modal: its description, and its full definition once opened. */
export type ContextEntry = { key: string; name: string; description: string; full?: string; load?: () => Promise<string>; markdown?: boolean };
export const contextDetail = signal<{ title: string; detail: string; markdown?: boolean; entries?: ContextEntry[]; focus?: string; skillRows?: InventorySkillRows } | null>(null);
type InventorySkillRows = { rows: import("./skills-mcp").InventoryRow[]; toggle?: (kind: import("./skills-mcp").InventoryKind, name: string, locked: boolean) => void };
/** The attachment open in the full-size preview modal (composer or timeline). */
export const imagePreview = signal<{ name: string; src: string } | null>(null);
/** The composer image open in the drawing modal: its draft and id. */
export const annotating = signal<{ key: string; id: string } | null>(null);
export const rev = signal(0);
/** Bumped by text/reasoning deltas; the settled timeline does not depend on it. */
export const streamRev = signal(0);
let streamFrame: ReturnType<typeof setTimeout> | null = null;
/** Coalesce a burst of deltas into one live-row render (about one frame). */
function bumpStream() {
  if (streamFrame !== null) return;
  streamFrame = setTimeout(() => { streamFrame = null; streamRev.update((n) => n + 1); }, 16);
}
export const connection = signal<ConnectionState>({ kind: "connecting" });
export const info = signal<Info | null>(null);
export const userHome = signal("");
export const modelCatalog = signal<any[]>([]);
export const catalogSource = signal("");
/** What a new session starts with, by `project|agent`: the user's last selection from `/api/model/last`. There is no default model. */
const remembered = signal<Record<string, string>>({});
export const config = signal<any | null>(null);
export type LearnChecklist = {
  fetch_id: string;
  source: string;
  scope: "project" | "global";
  project: string;
  requested: string[];
  problems: any[];
  found: { name: string; description?: string; installed?: boolean; warnings?: string[] }[];
  busy?: boolean;
};
export const learnChecklist = signal<LearnChecklist | null>(null);
export type StartingInventory = {
  project: string;
  skills: any[];
  skillProblems: any[];
  servers: any[];
  loading: boolean;
  error?: string;
};
export const startingInventory = signal<StartingInventory | null>(null);
export type PendingInput = { id: string; session_id: string; mode: "steer" | "queue"; parts: any[]; held?: boolean };
export const pendingInputs = signal<Record<string, { inputs: PendingInput[]; held: boolean }>>({});
const inputSeq = new Map<string, number>();
export async function loadInputs(id: string) {
  const seq = (inputSeq.get(id) ?? 0) + 1; inputSeq.set(id, seq);
  const data = await request<{inputs: PendingInput[]; held: boolean}>("GET", `/api/session/${id}/input`);
  if (inputSeq.get(id) === seq) {
    pendingInputs.update((all) => ({ ...all, [id]: data }));
    bump();
  }
}
// Prompt history: this project's sent prompts from Core, newest first (Up in the composer).
export const promptHistory = signal<{ project: string; entries: string[] }>({ project: "", entries: [] });
export async function loadPromptHistory(project: string) {
  const data = await request<{ prompts: { text: string; image_count: number }[] }>("GET", `/api/project/${project}/prompts`, { query: { limit: 200 } });
  const entries = data.prompts.map((p) => p.image_count ? `${p.text}${p.text ? "\n" : ""}[${p.image_count} image${p.image_count === 1 ? "" : "s"}]` : p.text).filter(Boolean);
  promptHistory.set({ project, entries });
}
export const contextBudget = signal<Record<string, { used: number; window: number | null; exact: boolean }>>({});
// Core builds the whole request body for a context read: one runs per session at a time, and
// requests made meanwhile coalesce into one more read, so replies also land in order.
const contextReads = new Map<string, boolean>(); // session -> another read is wanted
export async function loadContext(id: string) {
  if (contextReads.has(id)) { contextReads.set(id, true); return; }
  contextReads.set(id, false);
  try {
    do {
      contextReads.set(id, false);
      const data = await request<any>("GET", `/api/session/${id}/context`).catch(() => null);
      if (data) contextBudget.update((all) => ({ ...all, [id]: data }));
    } while (contextReads.get(id));
  } finally { contextReads.delete(id); }
}
function applyConfig(c: any) {
  config.set(c);
  splashReach(3);
  const detail = c?.values?.ui?.detail;
  if (detail === "low" || detail === "medium" || detail === "high") timelineDetail.set(detail);
}
export async function loadConfig() { applyConfig(await request("GET", "/api/config")); }
export async function patchConfig(path: string, value: unknown) {
  try { const c = config(); if (!c) throw new Error("Configuration is not loaded"); applyConfig(await request("PATCH", "/api/config", { body: { expected_revision: c.revision, set: { [path]: value }, unset: [] } })); }
  catch (e) { void loadConfig().catch(() => {}); toast("error", e instanceof Error ? e.message : String(e)); }
}
export const agentCatalog = signal<{ project: string | null; agents: any[] }>({ project: null, agents: [] });
export const sessionWorktrees = signal<Record<string, any | null>>({});
export const projectWorktrees = signal<Record<string, any | null>>({});
/** Live kernels (compact) and background bash jobs for the location strip and its modal. */
export const kernels = signal<any[]>([]);
export const bashJobs = signal<any[]>([]);
export const resourcesError = signal<string | null>(null);
export const resourcesOpen = signal(false);
export const resourcesKind = signal<"kernels" | "shells" | "all">("all");
let resourcesInFlight = false;
let resourcesAgain = false;
let resourcesSeq = 0;
/** Read both lists together; a burst of kernel/shell events coalesces into one more read. */
export async function loadResources(): Promise<void> {
  if (resourcesInFlight) { resourcesAgain = true; return; }
  resourcesInFlight = true;
  try {
    do {
      resourcesAgain = false;
      const seq = ++resourcesSeq;
      const [k, j] = await Promise.all([
        request<any[]>("GET", "/api/kernel", { query: { summary: "1" } }),
        request<any[]>("GET", "/api/bash"),
      ]);
      if (seq === resourcesSeq) { kernels.set(k); bashJobs.set(j); resourcesError.set(null); }
    } while (resourcesAgain);
  } catch (e) {
    resourcesError.set(e instanceof Error ? e.message : String(e));
  } finally {
    resourcesInFlight = false;
  }
}
export function rootSessionId(id: string): string | null {
  let session = model.sessions.get(id);
  for (let i = 0; session && i < model.sessions.size; i++) {
    if (!session.parent_id) return session.id;
    session = model.sessions.get(session.parent_id);
  }
  return null;
}
export function sessionTreeIds(id: string): string[] {
  const root = rootSessionId(id);
  return root ? model.tree(root) : [];
}
/** Kernels created by or used in, and shells started by, the viewed session tree. */
export function viewResources(): { kernels: any[]; jobs: any[] } {
  m();
  const id = viewed()?.id;
  if (!id) return { kernels: [], jobs: [] };
  const tree = new Set(sessionTreeIds(id));
  return {
    kernels: kernels().filter((k) => tree.has(k.created_by) || (Array.isArray(k.users) && k.users.some((user: string) => tree.has(user)))),
    jobs: bashJobs().filter((j) => tree.has(j.root) || tree.has(j.session_id)),
  };
}
/** `(live kernels, running background shells)` for the strip. */
export function resourceCounts(): { kernels: number; shells: number } {
  const r = viewResources();
  return { kernels: r.kernels.length, shells: r.jobs.filter((j) => j.status === "running").length };
}
export const compat = signal<Compat>({ kind: "unreported" });
export const banners = signal<Banner[]>([]);
export const dismissedBanners = signal<string[]>([]);
export const toasts = signal<Toast[]>([]);
export const notifications = signal<Toast[]>([]);
export const unread = signal(0);

// --- view state ---------------------------------------------------------------------------
export const leftOpen = signal(true);
/** The project rail stays beside the sidebar, open or closed, wherever the window has room for it. */
export const railShown = () => !phone && width() >= 560;
export const rightOpen = signal<boolean | null>(null); // null = default by width
export const leftTab = signal<"sessions" | "files" | "git" | "projects">("sessions");
export const centre = signal<CentreView>("session");
export const previousCentre = signal<CentreView>("session");
export const currentProject = signal<string | null>(null);
/** Sessions opened in the workspace header, in the order they were opened. */
export const openedTabs = signal<string[]>([]);
export const scopeAll = signal(false);
export const archivedFilter = signal(false);
export const drawer = signal<"left" | "right" | "notifications" | null>(null);
export const paletteOpen = signal(false);
/** The session switcher (ctrl/cmd+k): every root session, or the current project's when `scopeAll` is off. */
export const switcherOpen = signal(false);
/** The query `/search <text>` asks the switcher to open with; the switcher takes it when it mounts. */
export const switcherQuery = signal("");
/** The worktree details modal (`session.worktree_info`). */
export const worktreeInfoOpen = signal(false);
/** A simple list picker for catalog-backed choices (model, effort, agent). */
export type PickerItem = { id: string; label: string; hint?: string; detail?: string[]; run: () => void | Promise<void> };
/** The projects overview (PLAN 15.4): the selected row and the projects whose archived sessions are shown. */
export const projectsOverview = signal<null | { cursor: number; archived: string[] }>(null);
/** The keyboard flow from an empty composer: the session list, then the project list, which holds the keys. */
/** The composer has the keyboard focus. */
export const composerFocused = signal(false);
export const flowStage = signal<null | "sessions" | "projects">(null);
/** The project the flow's Up/Down has selected in the project list. */
/** The session the flow's Up/Down has selected in the session list. */
export const flowSession = signal<string | null>(null);
export const flowProject = signal<string | null>(null);
export const picker = signal<null | { title: string; items: PickerItem[]; note?: string }>(null);
export const confirmDialog = signal<null | { title: string; body: string; confirm: string; destructive: boolean; run: () => Promise<void> | void; busy?: boolean; error?: string; bracketActions?: boolean; scrollBody?: boolean }>(null);
export const renaming = signal(false);
export const addProjectOpen = signal(false);
/** Choices made before a session exists; consumed by its creation request. */
export const sessionDraft = signal<{ agent?: string; model?: string; effort?: string; disabled_tools?: string[] }>({});
export const firstPrompts = signal<Record<string, { text: string; images: number; session?: string; choices: ReturnType<typeof sessionDraft> }>>({});
/** The session whose first message just settled: its timeline pins that message to the top. */
export const firstPin = signal<string | null>(null);
export function pendingFirstPrompt() {
  m();
  const pending = firstPrompts()[currentProject() ?? ""];
  return pending && pending.session === model.selected?.sessionId && !(model.selected?.loaded && model.selected.messages.some((m) => m.role === "user")) ? pending : null;
}
function finishFirstPrompt() {
  const project = currentProject() ?? "";
  const pending = firstPrompts()[project];
  if (pending?.session && pending.session === model.selected?.sessionId && model.selected.loaded && model.selected.messages.some((m) => m.role === "user")) {
    firstPrompts.update((all) => { const next = { ...all }; delete next[project]; return next; });
    firstPin.set(pending.session);
    if (sessionDraft() === pending.choices) sessionDraft.set({});
  }
}
export const draft = signal<Record<string, string>>({});
/** Core's starting context (PLAN K4) for the draft in `key`; a newer draft ignores a late older answer. */
export const draftContext = signal<{ key: string; planKey: string; data: { used: number; window: number | null; parts: { instructions: number; tools: number; instruction_file: number }; tools: string[]; skills: any[]; mcp: any[]; session?: { effort: string | null } } } | null>(null);
let draftSeq = 0;
export const draftContextKey = (project: string, d = sessionDraft()) => JSON.stringify([project, d.agent ?? null, d.model ?? null, d.effort ?? null, d.disabled_tools ?? null]);
/** The draft fields that change the context itself. Effort steers the model; it is not context. */
export const draftContextPlanKey = (project: string, d = sessionDraft()) => JSON.stringify([project, d.agent ?? null, d.model ?? null, d.disabled_tools ?? null]);
/** Core's plan for the current draft's context. An effort change keeps it while Core re-plans. */
export function draftPlanContext(project: string) {
  const answer = draftContext();
  return answer && answer.planKey === draftContextPlanKey(project) ? answer.data : null;
}
/** The draft key last asked for: the same draft is asked once, until `force` or a failure. */
let draftAsked: string | null = null;
export async function loadDraftContext(project: string, force = false) {
  const d = sessionDraft(), key = draftContextKey(project, d);
  if (!force && key === draftAsked) return;
  draftAsked = key;
  const seq = ++draftSeq;
  try {
    const data = await request<any>("POST", "/api/draft/context", { body: { project, agent: d.agent, model: d.model || undefined, effort: d.effort, disabled_tools: d.disabled_tools } });
    if (seq === draftSeq) draftContext.set({ key, planKey: draftContextPlanKey(project, d), data });
  } catch {
    if (seq === draftSeq) draftAsked = null; // the client-side estimate stays; the next change asks again
  }
}
/** A project-rail drag in progress: the dragged project, its insertion index among the rail's projects, and whether Esc cancelled it. */
export const railDrag = signal<{ id: string; to: number; cancelled: boolean } | null>(null);
export const waitIndex = signal(0);
export const tick = signal(Date.now());
export const settingsSection = signal<string | null>(null);
/** Whether the Settings section rail has focus (the pane is entered with Enter or a section press). */
export const settingsNavFocused = signal(true);
/** Whether a page section (Providers, Agents) is open in the pane; Esc from it returns to the rail. */
export const settingsPageOpen = signal(false);
/** The Settings search text; it filters the section rail and the selected pane. */
export const settingsQuery = signal("");
/** The agent detail page drilled into (`agents/<name>`); the section rail and page content come from `settingsSection`. */
export const settingsPage = signal<string | null>(null);
/** The provider whose connection/sign-in modal is open. */
export const providerDetail = signal<string | null>(null);
/** The last `provider.login` state per provider: pending sign-ins show their code and link on the provider's row. */
export const providerLogins = signal<Record<string, { status: string; message?: string | null; verification_uri?: string | null; user_code?: string | null }>>({});
/** Bumped by `agents.changed` and provider changes so open Settings pages reload. */
export const settingsReload = signal(0);
export const themeChoice = signal<"system" | "light" | "dark">("system");
export const prefs = signal<Record<string, boolean | number | string>>({});
export type TimelineDetail = "low" | "medium" | "high";
export const timelineDetail = signal<TimelineDetail>("medium");
/** The timeline shows a subdued scroll bar while scrolling (`appearance.scrollbar`). */
export const scrollBarOn = signal(true);
/** Explicit open/closed state for timeline groups and tool details. */
export const timelineOpen = signal<Record<string, boolean>>({});
export const scrolledUp = signal(false);
export type SidebarRowTarget = { kind: "session" | "project"; id: string };
export const focusedSidebarRow = signal<SidebarRowTarget | null>(null);
export const sidebarRowMenu = signal<SidebarRowTarget | null>(null);
export const focusedTool = signal<{ key: string; clipped: boolean; toggleRaw: () => void } | null>(null);
export const toolOutputAll = signal<Record<string, boolean>>({});

/** The native builds talk to a Core on the network; the web build's host proxies the same-origin API. */
export const native = platform.os !== "web";
/** A phone or tablet: hover-only affordances have to stay visible, and targets stay finger-sized. */
export const touch = platform.idiom === "phone" || platform.idiom === "pad";
export const phone = mobileClient(native, platform.os, platform.idiom);
export const phonePage = signal<import("./mobile").PhonePage>("sessions");
/** The saved Core address; null until the Connect screen has one. */
export const coreUrl = signal<string | null>(null);
/** True once the saved address has been read (immediately on the web, which has none). */
export const coreReady = signal(!native);
const CORE_KEY = "nexus.core";
/** The local Core's default address; macOS retries it without a Connect screen. */
const LOCAL_CORE = "http://127.0.0.1:4747";

/** `host:port`, with `http://` assumed when no scheme is given; a path prefix is kept. */
export function normalizeCoreUrl(raw: string): string {
  const text = raw.trim();
  if (!text) throw new Error("Enter the Core address");
  let url: URL;
  try { url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `http://${text}`); }
  catch { throw new Error("That is not a valid address"); }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Use an http:// or https:// address");
  return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, "")}`;
}

/** Check that the address answers, then save it; the Connect screen shows the reason when it does not. */
export async function connectCore(raw: string): Promise<void> {
  const url = normalizeCoreUrl(raw);
  const previous = apiBase();
  setApiBase(url);
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 8000);
  try {
    await request("GET", "/api/info", { signal: abort.signal });
  } catch (e) {
    setApiBase(previous);
    if (e instanceof ApiError && e.status === 401) {
      throw new Error(platform.os === "macos"
        ? "This Core asks for a token and ~/.nexus/daemon.json did not match it. Start Core from this Mac account, or use a LAN address."
        : "This Core asks for a token. Restart it with NEXUS_HOST=0.0.0.0 so a native app can connect without one.");
    }
    const reason = e instanceof Error ? e.message : String(e);
    const hint = /transport security|secure connection/i.test(reason)
      ? "iOS blocked plain HTTP here: use an https:// address."
      : "Check the address, that Core is running with NEXUS_HOST=0.0.0.0, and that this device can reach it.";
    throw new Error(`Nexus Core did not answer at ${url}. ${hint} (${reason})`);
  } finally {
    clearTimeout(timer);
  }
  coreUrl.set(url);
  coreReady.set(true);
  try { await Storage.setItem(CORE_KEY, url); } catch {}
  splashStart();
  sync.restart();
  loadComposerData();
}

/** A loopback address, where the macOS app uses the token in daemon.json. */
function isLoopbackUrl(url: string): boolean {
  try { const host = new URL(url).hostname; return host === "127.0.0.1" || host === "::1" || host === "[::1]" || host === "localhost"; }
  catch { return false; }
}

/**
 * Read the saved address at startup. On macOS a running local Core's daemon.json wins
 * when there is no saved address or the saved one is loopback: its token is then read
 * by the transport, so the app never asks for one. A saved LAN address stays chosen.
 */
export async function loadCoreUrl(): Promise<void> {
  if (!native) { coreReady.set(true); return; }
  let saved = "";
  try { saved = (await Storage.getItem(CORE_KEY)) ?? ""; } catch {}
  const daemon = await localCore();
  if (daemon && (!saved || isLoopbackUrl(saved))) { setApiBase(daemon.url); coreUrl.set(daemon.url); }
  else if (saved) { setApiBase(saved); coreUrl.set(saved); }
  else if (platform.os === "macos") { setApiBase(LOCAL_CORE); coreUrl.set(LOCAL_CORE); }
  coreReady.set(true);
  if (!coreUrl()) splashDismiss();
}

export const width = () => windowSize().width;
export const NARROW = 800;
export const isNarrow = computed(() => windowSize().width < NARROW);
/** At or above this width an open file or diff panel sits beside the timeline; below it, it slides over the body. */

// Per-client view settings (Settings rows with scope = "client") persist in NSH Storage.
export async function loadPrefs() {
  try {
    const raw = await Storage.getItem("nexus.prefs");
    if (raw) {
      const p = JSON.parse(raw);
      prefs.set(p.prefs ?? {});
      if (p.theme) themeChoice.set(p.theme);
      if (typeof p.leftOpen === "boolean") leftOpen.set(p.leftOpen);
      if (typeof p.rightOpen === "boolean") rightOpen.set(p.rightOpen);
      if (p.leftTab) leftTab.set(p.leftTab);
      const detail = p.timelineDetail ?? p.prefs?.["appearance.timeline_detail"];
      if (detail === "low" || detail === "medium" || detail === "high") timelineDetail.set(detail);
      if (typeof p.scrollBar === "boolean") scrollBarOn.set(p.scrollBar);
    }
  } catch {}
}
export function savePrefs() {
  try { void Storage.setItem("nexus.prefs", JSON.stringify({ prefs: prefs(), theme: themeChoice(), leftOpen: leftOpen(), rightOpen: rightOpen(), leftTab: leftTab(), timelineDetail: timelineDetail(), scrollBar: scrollBarOn() })); } catch {}
}

/** Notification sounds are on unless Settings turned them off. */
export function notificationSoundsOn(): boolean {
  return prefs()["notifications.sound"] !== false;
}
export function setNotificationSounds(on: boolean) {
  prefs.update((p) => ({ ...p, "notifications.sound": on }));
  savePrefs();
}

export function setScrollBar(on: boolean) {
  scrollBarOn.set(on);
  savePrefs();
}

export function setTimelineDetail(level: TimelineDetail) {
  timelineDetail.set(level);
  if (config()) void patchConfig("ui.detail", level);
  timelineOpen.update((open) => Object.fromEntries(Object.entries(open).filter(([key]) => !key.startsWith("group:") && !key.startsWith("tool:"))));
  toolOutputAll.set({});
  savePrefs();
  toast("info", `Detail: ${level}`);
}

export function bump() { rev.update((n) => n + 1); }
/** Read the model reactively: components call `m()`. */
export const m = (): Model => { rev(); return model; };

// --- catalog and location data ---------------------------------------------------------------
let modelsAsked = false;
const rememberedAsked = new Set<string>();
const agentCache = new Map<string, any[]>();
const agentRequests = new Set<string>();
const sessionWorktreeRequests = new Set<string>();
const projectWorktreeRequests = new Set<string>();
const sessionWorktreeSeq = new Map<string, number>();
const projectWorktreeSeq = new Map<string, number>();
/** The session directory each session's worktree listing was requested for. */
const worktreeDirectories = new Map<string, string>();

/** The model a new session for this project and agent starts with, or "" when none was ever selected. Asks Core once per key; `refresh` asks again. */
export function rememberedModel(project: string | null | undefined, agent: string, refresh = false): string {
  const key = `${project ?? ""}|${agent}`;
  if (refresh || !rememberedAsked.has(key)) {
    rememberedAsked.add(key);
    const query = new URLSearchParams({ agent, ...(project ? { project } : {}) });
    void request<any>("GET", `/api/model/last?${query}`)
      .then((r) => remembered.update((all) => ({ ...all, [key]: r.model ?? "" })))
      .catch(() => { rememberedAsked.delete(key); });
  }
  return remembered()[key] ?? "";
}

export function loadComposerData(project = currentProject()) {
  if (!modelsAsked) {
    modelsAsked = true;
    void reloadModelCatalog().catch(() => { modelsAsked = false; });
  }
  const key = project ?? "";
  if (agentCache.has(key)) {
    if (agentCatalog().project !== project) agentCatalog.set({ project, agents: agentCache.get(key)! });
  } else if (!agentRequests.has(key)) {
    agentRequests.add(key);
    void request<any>("GET", "/api/agent", { query: project ? { project } : undefined }).then((r) => {
      const agents = r.agents ?? [];
      agentCache.set(key, agents);
      if ((currentProject() ?? "") === key) agentCatalog.set({ project, agents });
    }).catch(() => { agentRequests.delete(key); });
  }
}

/** Refresh the Core-backed catalog and keep the composer/settings views in sync. */
export async function reloadModelCatalog() {
  const response = await request<any>("GET", "/api/model");
  modelCatalog.set(response.models ?? []);
  catalogSource.set(response.source ?? "");
  return response;
}

export function loadSessionWorktrees(id: string, refresh = false) {
  if (!refresh && (sessionWorktreeRequests.has(id) || Object.prototype.hasOwnProperty.call(sessionWorktrees(), id))) return;
  sessionWorktreeRequests.add(id);
  const directory = model.sessions.get(id)?.directory;
  if (directory) worktreeDirectories.set(id, directory);
  const seq = (sessionWorktreeSeq.get(id) ?? 0) + 1;
  sessionWorktreeSeq.set(id, seq);
  void request<any>("GET", `/api/session/${id}/worktrees`).then((data) => {
    if (sessionWorktreeSeq.get(id) === seq) sessionWorktrees.update((all) => ({ ...all, [id]: data }));
  }).catch(() => {
    if (sessionWorktreeSeq.get(id) === seq) sessionWorktrees.update((all) => ({ ...all, [id]: null }));
  }).finally(() => {
    if (sessionWorktreeSeq.get(id) === seq) sessionWorktreeRequests.delete(id);
  });
}

export function loadProjectWorktrees(id: string, refresh = false) {
  if (!refresh && (projectWorktreeRequests.has(id) || Object.prototype.hasOwnProperty.call(projectWorktrees(), id))) return;
  projectWorktreeRequests.add(id);
  const seq = (projectWorktreeSeq.get(id) ?? 0) + 1;
  projectWorktreeSeq.set(id, seq);
  void request<any>("GET", `/api/project/${id}/worktrees`).then((data) => {
    if (projectWorktreeSeq.get(id) === seq) projectWorktrees.update((all) => ({ ...all, [id]: data }));
  }).catch(() => {
    if (projectWorktreeSeq.get(id) === seq) projectWorktrees.update((all) => ({ ...all, [id]: null }));
  }).finally(() => {
    if (projectWorktreeSeq.get(id) === seq) projectWorktreeRequests.delete(id);
  });
}

// --- project MCP files (.mcp.json, .agents/mcp.json) ------------------------------------------
/** One project's MCP summary; enabled servers start with a new session, never from this read. */
export type ProjectMcpState = { data: ProjectMcpSummary | null; loading: boolean; error: string | null };
export const projectMcp = signal<Record<string, ProjectMcpState>>({});
const projectMcpRequests = new Set<string>();
const projectMcpSeq = new Map<string, number>();

/**
 * Read `GET /api/project/{id}/mcp` (never starts a server). `refresh` asks again even when a
 * cached summary exists, so a manual Refresh or an `mcp.changed` shows the current file.
 */
export async function loadProjectMcp(project: string, refresh = false): Promise<ProjectMcpSummary | null> {
  const cached = projectMcp()[project];
  if (!refresh && (projectMcpRequests.has(project) || cached?.data)) return cached?.data ?? null;
  const seq = (projectMcpSeq.get(project) ?? 0) + 1;
  projectMcpSeq.set(project, seq);
  projectMcpRequests.add(project);
  projectMcp.update((all) => ({ ...all, [project]: { data: cached?.data ?? null, loading: true, error: null } }));
  try {
    const data = await request<ProjectMcpSummary>("GET", `/api/project/${project}/mcp`);
    if (projectMcpSeq.get(project) === seq) projectMcp.update((all) => ({ ...all, [project]: { data, loading: false, error: null } }));
    return data;
  } catch (e) {
    const error = e instanceof ApiError ? e.message : String(e);
    if (projectMcpSeq.get(project) === seq) projectMcp.update((all) => ({ ...all, [project]: { data: cached?.data ?? null, loading: false, error } }));
    return null;
  } finally {
    if (projectMcpSeq.get(project) === seq) projectMcpRequests.delete(project);
  }
}

/** The most-specific checkout containing a directory (path-component aware). */
export function worktreeForDirectory(directory: string, listing: any | null | undefined): any | null {
  const trees = listing?.worktrees;
  if (!Array.isArray(trees)) return null;
  return trees
    .filter((tree: any) => {
      const root = typeof tree?.path === "string" ? tree.path.replace(/\/$/, "") : "";
      return root && (directory === root || directory.startsWith(`${root}/`));
    })
    .sort((a: any, b: any) => String(b.path).length - String(a.path).length)[0] ?? null;
}

/**
 * The listing the viewed session's directory is in: its own session listing when that contains
 * the directory, else the project's listing (a relocation can leave the session read behind).
 * One lookup feeds the strip, the linked-worktree predicate and the details modal.
 */
export function worktreeListingForSession(): any | null {
  m();
  const s = viewed();
  const p = currentProject();
  const directory = s?.directory ?? (p ? model.projects.get(p)?.path : undefined);
  const cached = s ? sessionWorktrees()[s.id] ?? null : null;
  const mine = cached && (!cached.session_directory || cached.session_directory === directory) ? cached : null;
  const project = p ? projectWorktrees()[p] ?? null : null;
  if (directory && worktreeForDirectory(directory, mine)) return mine;
  if (directory && worktreeForDirectory(directory, project)) return project;
  return null;
}

export function currentWorktree() {
  m();
  const s = viewed();
  const p = currentProject();
  const directory = s?.directory ?? (p ? model.projects.get(p)?.path : undefined);
  if (!directory) return null;
  return worktreeForDirectory(directory, worktreeListingForSession());
}

// --- alerts -----------------------------------------------------------------------------
let toastId = 1;
/** Toasts drawn at once; the rest queue behind `+N more` and start their timer when first drawn. */
export const TOAST_VISIBLE = 3;
const TOAST_QUEUE = 20;
const timed = new Set<number>();
/** The alert whose full text is open in the detail modal (D15). */
export const alertDetail = signal<Toast | null>(null);
/** One row of `GET /api/usage` (K11): a connected provider with its plan and limit windows, notes, or why it could not be read. */
export interface UsageProvider { id: string; label?: string; plan?: string; error?: string; notes?: string[]; windows?: { label?: string; used_percent?: number | null; resets_at?: number | null; period_seconds?: number | null; kind?: string; detail?: string }[] }
/** The open provider-usage dialog (`app.usage`); `null` when closed. */
export const usageDialog = signal<{ providers: UsageProvider[]; loading: boolean; missing?: boolean; fetched?: number | null } | null>(null);
/**
 * The toast's `text` is the whole message and stays on the record (notifications, detail modal). The
 * card shows a title and an optional body: the first line, or the text up to an early `: `.
 */
export function splitAlert(text: string): { title: string; body: string; inline: boolean } {
  const lines = text.split("\n");
  if (lines.length > 1) return { title: lines[0], body: lines.slice(1).join(" ").trim(), inline: false };
  const i = text.indexOf(": ");
  // An early `: ` ends a bold lead-in; the text stays one run, so it reads whole to screen readers and search.
  if (i > 0 && i <= 40) return { title: text.slice(0, i), body: text.slice(i), inline: true };
  return { title: text, body: "", inline: false };
}
function startTimers() {
  for (const t of toasts().slice(0, TOAST_VISIBLE)) {
    if (timed.has(t.id)) continue;
    timed.add(t.id);
    setTimeout(() => dismissToast(t.id), t.level === "error" ? 8000 : 4000);
  }
}
export function toast(level: Toast["level"], text: string) {
  const t: Toast = { id: toastId++, level, text, at: Date.now() };
  toasts.update((l) => [...l, t].slice(-TOAST_QUEUE));
  notifications.update((l) => [t, ...l].slice(0, 100));
  unread.update((n) => n + 1);
  startTimers();
}
export const openAlert = (t: Toast) => { alertDetail.set(t); dismissToast(t.id); };
export const dismissToast = (id: number) => { toasts.update((l) => l.filter((x) => x.id !== id)); timed.delete(id); startTimers(); };

// --- sync ---------------------------------------------------------------------------------
let loadingOlder = false;
const olderFailed = new Set<string>();
/** The page of messages before the oldest loaded one (`turn.load_older`); `manual` retries a session that failed. */
export async function loadOlderMessages(manual = false) {
  const sel = model.selected;
  const first = sel?.messages[0];
  if (!sel || !first || loadingOlder) return;
  loadingOlder = true;
  try {
    const page = await request<{ messages: Message[]; more: boolean }>("GET", `/api/session/${sel.sessionId}/message`, { query: { limit: MESSAGE_PAGE, before: first.seq } });
    olderFailed.delete(sel.sessionId);
    model.prependMessages(sel.sessionId, page.messages, page.more);
    bump();
  } catch (e) {
    olderFailed.add(sel.sessionId);
    toast("error", `Older messages failed to load: ${e instanceof ApiError ? e.message : String(e)}`);
  }
  loadingOlder = false;
  if (!manual) autoLoadOlder();
}
/** A running session shows everything: its older pages load by themselves (an idle one waits for the button). */
function autoLoadOlder() {
  const sel = model.selected;
  if (!sel || !sel.loaded || !sel.messagesMore || loadingOlder || olderFailed.has(sel.sessionId)) return;
  if (!model.running.has(sel.sessionId)) return;
  void loadOlderMessages();
}

const host: SyncHost = {
  model,
  canStart: true,
  setConnection(c) {
    connection.set(c);
    if (c.kind === "live") {
      splashReach(4);
      splashFinish();
    } else if (c.kind === "offline" || c.kind === "auth_failed" || c.kind === "incompatible"
      || (c.kind === "reconnecting" && c.attempt >= 2)) {
      splashDismiss();
    }
  },
  setInfo(i, c) { info.set(i); compat.set(c); splashReach(1); },
  addBanner(b) { banners.update((l) => [...l.filter((x) => x.kind !== b.kind), b]); },
  changed() { finishFirstPrompt(); bump(); reconcileSelection(); autoLoadOlder(); },
  streamed() { bumpStream(); },
  inspectorEvent(e) { inspectorHooks.event?.(e); receiveBtw(e); },
  snapshotApplied() { inspectorHooks.invalidate?.(); splashReach(2); reconcileSelection(); void loadConfig().catch(() => {}); void loadResources(); },
  configChanged() { void loadConfig().catch(() => {}); },
  resourcesChanged() { void loadResources(); },
  settingsEvent(e) {
    if (e.type === "provider.login") {
      const d = e.data as { provider: string; status: string };
      providerLogins.update((all) => ({ ...all, [d.provider]: d }));
      if (d.status === "pending") return;
    }
    settingsReload.update((n) => n + 1);
  },
  contextChanged(id) { void loadContext(id).catch(() => {}); },
  inputsChanged(id) { void loadInputs(id).catch(() => {}); },
  sessionFinished() { if (notificationSoundsOn()) playNotificationCue("finished"); },
  sessionInput() { if (notificationSoundsOn()) playNotificationCue("input"); },
  handoffEvent(e) {
    if (e.type === "session.handoff_failed") { toast("warning", `Automatic handoff failed: ${(e.data as any)?.error ?? "unknown error"}`); return; }
    const d = e.data as { reason?: string; to?: string };
    if (d?.reason === "auto" && d.to && e.session_id === viewed()?.id) {
      toast("info", "Context was nearly full: continued in a new session");
      void selectSession(d.to);
    }
  },
  globalEvent(e) {
    if (e.type === "voice.progress") receiveVoiceProgress(e.data);
    else if (e.type.startsWith("manager.")) managerHooks.event?.(e);
    else handleGlobalEvent(e);
  },
  streamConnected(streamId) { viewReporter.connected(streamId); managerHooks.connected?.(streamId); },
  streamClosed(streamId) { viewReporter.closed(streamId); managerHooks.closed?.(streamId); },
};
export const sync = new Sync(host);

/** Core's ephemeral view registry; a rejected client id asks for a fresh stream. */
export const viewReporter = new ViewReporter({ onMismatch: () => sync.forceSnapshot() });

/**
 * What this client is showing, as ids plus the shared panel label. A selected
 * session implies its project, so the reported pair is never transiently mixed.
 */
export function currentClientView(): ClientView {
  m();
  const session = viewed()?.id ?? null;
  const project = session
    ? model.sessions.get(session)?.project_id ?? currentProject()
    : currentProject();
  // NSH has no Git diff panel (the Git sidebar is a status list); the editor's
  // draft-vs-disk diff is still the file panel, never the TUI's `diff` panel.
  const panel = phone || centre() === "settings" ? null
    : openFile() !== null ? "file" as const
    : leftTab() === "files" && (isNarrow() ? drawer() === "left" : leftOpen()) ? "files" as const
    : null;
  return { project: project ?? null, session, panel };
}

let viewSent: ClientView | null = null;
/** Tell the reporter when the view changed. The stream id and retries stay its business. */
export function syncView() {
  const view = currentClientView();
  if (sameClientView(viewSent, view)) return;
  viewSent = view;
  viewReporter.setView(view);
}

function reconcileSelection() {
  closeArchivedTabs();
  if (model.selectedDeleted) {
    toast("info", "That session was deleted.");
    model.selectedDeleted = null;
    centre.set("new_session");
  }
  if (currentProject() && !model.projects.has(currentProject()!)) currentProject.set(null);
  if (!currentProject()) {
    const sel = model.selected && model.sessions.get(model.selected.sessionId);
    const p = sel?.project_id ?? (model.projectsRecent().find((x) => x.status !== "inactive") ?? model.projectsRecent()[0])?.id ?? null;
    if (p) currentProject.set(p);
  }
  const selected = model.selected?.sessionId;
  const project = selected ? model.sessions.get(selected)?.project_id : currentProject();
  if (project) loadComposerData(project);
  if (selected) {
    loadSessionWorktrees(selected);
    // A directory move updates the session view (`session.updated` after the move): reload the
    // worktrees when its directory differs from the one they were read for.
    const directory = model.sessions.get(selected)?.directory;
    const seen = worktreeDirectories.get(selected);
    if (directory && seen !== undefined && seen !== directory) loadSessionWorktrees(selected, true);
  } else if (project) {
    loadProjectWorktrees(project);
  }
}

export function startApp() {
  if (native) {
    // A native app reaches Core directly and cannot start it; the Connect screen owns the rest.
    host.canStart = false;
    void loadCoreUrl().then(() => {
      if (!coreUrl()) return;
      sync.start();
      loadComposerData();
    });
  } else {
    void fetch(`${apiBase()}/__nexus/host`).then((r) => r.json()).then((h) => {
      host.canStart = !!h.canStart;
      if (typeof h.userHome === "string") userHome.set(h.userHome);
    }).catch(() => {});
    sync.start();
    loadComposerData();
  }
  setInterval(() => { const c = connection(); if (c.kind === "reconnecting") tick.set(Date.now()); }, 1000);
}

let inventorySeq = 0;
/** Load the new-session skill and MCP choices for the currently selected project. */
export function loadStartingInventory(project: string, refresh = false) {
  const cached = startingInventory();
  if (!refresh && cached?.project === project) return;
  const seq = ++inventorySeq;
  startingInventory.set({ project, skills: [], skillProblems: [], servers: [], loading: true });
  const skills = request<any>("GET", "/api/skill", { query: { project } }).catch((e) => ({ _error: String(e), skills: [], problems: [] }));
  const mcp = request<any>("GET", "/api/mcp").catch((e) => ({ _error: String(e), servers: [] }));
  void Promise.all([skills, mcp]).then(([skillData, mcpData]) => {
    if (seq !== inventorySeq) return;
    const errors = [skillData._error, mcpData._error].filter(Boolean);
    startingInventory.set({
      project,
      skills: skillData.skills ?? [],
      skillProblems: skillData.problems ?? [],
      servers: mcpData.servers ?? [],
      loading: false,
      error: errors.join(" · ") || undefined,
    });
  });
}

let pendingSkillInstalls = 0;
let deferredSkillsChanged = false;
const globalToastState = emptyGlobalToastState();
export function beginOwnSkillInstall() {
  pendingSkillInstalls++;
  globalToastState.skillInstallPending = true;
}
export function finishOwnSkillInstall() {
  pendingSkillInstalls = Math.max(0, pendingSkillInstalls - 1);
  globalToastState.skillInstallPending = pendingSkillInstalls > 0;
  globalToastState.muteSkillsUntil = Date.now() + 5000;
  deferredSkillsChanged = false;
}
export function failOwnSkillInstall() {
  pendingSkillInstalls = Math.max(0, pendingSkillInstalls - 1);
  globalToastState.skillInstallPending = pendingSkillInstalls > 0;
  if (!pendingSkillInstalls && deferredSkillsChanged) {
    if (globalToastState.muteSkillsUntil <= Date.now()) toast("info", "Skills changed. New sessions will use them.");
    deferredSkillsChanged = false;
  }
}
function handleGlobalEvent(e: import("./core/api").CoreEvent) {
  if (e.type === "skills.changed" || e.type === "mcp.changed") {
    const project = currentProject();
    if (project) loadStartingInventory(project, true);
  }
  if (e.type === "mcp.changed") {
    const eventProject = typeof (e.data as { project_id?: unknown })?.project_id === "string" ? (e.data as { project_id: string }).project_id : currentProject();
    if (eventProject) void loadProjectMcp(eventProject, true);
  }
  if (["agents.changed", "skills.changed", "mcp.changed", "extensions.changed", "config.changed"].includes(e.type) && draftContext()) {
    const project = currentProject();
    if (project) void loadDraftContext(project, true);
  }
  if (e.type === "files.watch") {
    const d = e.data as { project_id?: string; workspace?: string; status?: string };
    if (d.project_id === currentProject() && d.workspace === model.projects.get(d.project_id!)?.path) fileWatchStatus.set(d.status ?? "");
  }
  if (e.type === "git.changed" && leftTab() === "git") void loadGitPage();
  if (e.type === "files.changed") {
    const f = openFile();
    const d = e.data as { project_id?: string; workspace?: string; path?: string; paths?: string[]; revision?: string; polling?: boolean; all?: boolean; omitted?: number };
    if (d.project_id === currentProject() && (!d.workspace || d.workspace === model.projects.get(d.project_id!)?.path)
        && currentClientView().panel === "files") {
      void loadFiles();
      for (const path of filesExpanded()) void loadFiles(path);
    }
    const root = f?.session ? model.sessions.get(f.session)?.directory : model.projects.get(f?.project ?? "")?.path;
    if (f && !f.saving && d.project_id === f.project && (!d.workspace || d.workspace === root)
        && (d.path === f.path || d.paths?.includes(f.path) || d.polling || d.all || (d.omitted ?? 0) > 0)
        && (!d.revision || d.revision !== f.revision)) void refreshChangedFile();
  }
  if (e.type === "skills.changed" && pendingSkillInstalls) deferredSkillsChanged = true;
  const alert = globalEventToast(e.type, e.data, globalToastState);
  if (alert) toast(alert.level, alert.text);
}

export type FileEntry = { name: string; kind: string; git_status?: string | null; ignored: boolean };
/** One directory of the Files tree, read from Core; `""` is the project root. */
export type FileDir = { entries: FileEntry[]; error?: string; loading?: boolean };
export const selectedFileEntry = signal<{ path: string; kind: string } | null>(null);
export const fileOperation = signal<{ action: string; path: string } | null>(null);
export const fileTree = signal<{ project: string | null; dirs: Record<string, FileDir> }>({ project: null, dirs: {} });
/** Expanded directories of the Files tree, by project-relative path. Kept while the tab is open. */
export const filesExpanded = signal<ReadonlySet<string>>(new Set());
/** The open project file. `draft` is set while editing (K10): the text in the editor, against `revision` as read. */
export type OpenFile = { path: string; content?: string; revision?: string; error?: string; draft?: string; saving?: boolean; stale?: boolean; project?: string; session?: string };
export const openFile = signal<OpenFile | null>(null);
/** A file is open in the right-hand panel. */
export const panelOpen = computed(() => openFile() !== null);
/** The file panel's find bar is open (cmd+f / editor.find). */
export const fileFindOpen = signal(false);
/** The source of a Markdown file is shown instead of its rendered preview. */
export const fileSource = signal(false);
export const isMarkdownPath = (path?: string) => !!path && /\.(md|markdown)$/i.test(path);
/** The open file is Markdown, so it can be read as prose. */
export const markdownFile = () => isMarkdownPath(openFile()?.path);
export const fileDirty = () => { const f = openFile(); return !!f && f.draft !== undefined && f.draft !== f.content; };
export const gitStatus = signal<{ session: string; data?: any; error?: string } | null>(null);
export type GitChange = { path: string; status: string; old_path?: string; staged: boolean; unstaged: boolean };
export type GitPageData = { worktree: string; branch: string; changes: GitChange[]; branches: { name: string; local: boolean; current: boolean; upstream?: string }[] };
export const gitPage = signal<{ project: string; worktree?: string; data?: GitPageData; error?: string } | null>(null);
export const selectedGitFile = signal<{ path: string; group: "Staged" | "Unstaged" | "Untracked" } | null>(null);
export const gitDiff = signal<{ path: string; group: string; text?: string; clipped?: boolean; error?: string } | null>(null);
export async function openGitDiff() {
  const project = currentProject(), selected = selectedGitFile(); if (!project || !selected) return;
  const { path, group } = selected;
  gitDiff.set({ path, group });
  if (group === "Untracked") { gitDiff.set({ path, group, text: "Untracked file: Git has no diff for it yet." }); return; }
  try {
    const data = await request<{ staged: string; unstaged: string; staged_clipped?: boolean; unstaged_clipped?: boolean }>("GET", `/api/project/${project}/git/diff`, { query: { path, worktree: currentWorktree()?.path } });
    if (gitDiff()?.path === path && gitDiff()?.group === group) gitDiff.set({ path, group, text: group === "Staged" ? data.staged : data.unstaged, clipped: group === "Staged" ? data.staged_clipped : data.unstaged_clipped });
  } catch (e) { if (gitDiff()?.path === path && gitDiff()?.group === group) gitDiff.set({ path, group, error: String(e) }); }
}
export const gitInput = signal<{ title: string; submit: (value: string) => void } | null>(null);
let gitPageRequest = 0;
export async function loadGitPage() {
  const project = currentProject(), worktree = currentWorktree()?.path as string | undefined;
  if (!project) { gitPage.set(null); return; }
  const seq = ++gitPageRequest;
  try {
    const data = await request<GitPageData>("GET", `/api/project/${project}/git`, { query: { worktree } });
    if (seq === gitPageRequest && currentProject() === project && currentWorktree()?.path === worktree) gitPage.set({ project, worktree, data });
  } catch (e) { if (seq === gitPageRequest && currentProject() === project) gitPage.set({ project, worktree, error: String(e) }); }
}
const fileRequests = new Map<string, number>();
/** Read one directory of the Files tree; expanding a folder keeps the ones already read. */
export async function loadFiles(path = "") {
  const project = currentProject(), session = undefined;
  if (!project) { fileTree.set({ project: null, dirs: {} }); filesExpanded.set(new Set()); return; }
  if (fileTree().project !== project) { selectedFileEntry.set(null); fileTree.set({ project, dirs: {} }); filesExpanded.set(new Set()); }
  const seq = (fileRequests.get(path) ?? 0) + 1;
  fileRequests.set(path, seq);
  const put = (dir: FileDir) => {
    if (currentProject() !== project || fileRequests.get(path) !== seq) return;
    fileTree.update((tree) => ({ project, dirs: { ...tree.dirs, [path]: dir } }));
  };
  try {
    const data = await request<{ path: string; entries: FileEntry[] }>("GET", `/api/project/${project}/files`, { query: { path, session } });
    put({ entries: data.entries ?? [] });
  } catch (e) { put({ entries: [], error: e instanceof Error ? e.message : String(e) }); }
}
/** Expand or collapse one Files tree folder; expanding reads it the first time. */
export function toggleFileDir(path: string) {
  const open = filesExpanded();
  if (open.has(path)) { filesExpanded.set(new Set([...open].filter((p) => p !== path))); return; }
  filesExpanded.set(new Set([...open, path]));
  if (!fileTree().dirs[path]) void loadFiles(path);
}
let fileOpenRequest = 0;
export async function showFile(path: string) {
  const project = currentProject(), session = undefined;
  if (!project) return;
  const seq = ++fileOpenRequest;
  openFile.set({ path, project, session }); centre.set("file"); drawer.set(null); fileFindOpen.set(false); fileSource.set(true);
  try {
    const data = await request<{ content: string; revision?: string }>("GET", `/api/project/${project}/file`, { query: { path, session } });
    if (seq === fileOpenRequest) openFile.set({ path, project, session, content: data.content, revision: data.revision, draft: data.content });
  } catch (e) { if (seq === fileOpenRequest) openFile.set({ path, project, session, error: String(e) }); }
}
let fileRefreshPending = false, fileRefreshAgain = false;
/** A disk invalidation refreshes clean text; it never replaces an unsaved draft. */
async function refreshChangedFile() {
  fileRefreshAgain = true;
  if (fileRefreshPending) return;
  fileRefreshPending = true;
  try {
    while (fileRefreshAgain) {
      fileRefreshAgain = false;
      const f = openFile(); if (!f?.project || f.saving) continue;
      try {
        const data = await request<{ content: string; revision?: string }>("GET", `/api/project/${f.project}/file`, { query: { path: f.path, session: f.session } });
        const cur = openFile();
        if (!cur || cur.project !== f.project || cur.session !== f.session || cur.path !== f.path || cur.saving || cur.revision !== f.revision) continue;
        if (data.revision === cur.revision) continue;
        if (cur.draft !== undefined && cur.draft !== cur.content) {
          if (!cur.stale) toast("warning", "The file changed on disk: reload before saving");
          openFile.set({ ...cur, stale: true });
        } else openFile.set({ ...cur, content: data.content, revision: data.revision, draft: cur.draft !== undefined ? data.content : undefined, stale: false, error: undefined });
      } catch (error) {
        const cur = openFile();
        if (cur?.project === f.project && cur?.path === f.path && cur?.session === f.session) openFile.set({ ...cur, stale: true, error: String(error) });
      }
    }
  } finally { fileRefreshPending = false; }
}

/** Read the open file again; `keepEdit` leaves the editor open on the fresh text (reload), else a clean editor follows the file. */
export async function reloadFile() {
  const f = openFile(); if (!f?.project) return;
  const editing = f.draft !== undefined;
  try {
    const data = await request<{ content: string; revision?: string }>("GET", `/api/project/${f.project}/file`, { query: { path: f.path, session: f.session } });
    if (openFile()?.path === f.path) openFile.set({ ...f, content: data.content, revision: data.revision, draft: editing ? data.content : undefined, stale: false, error: undefined });
  } catch (e) { toast("error", `Reload failed: ${e instanceof Error ? e.message : String(e)}`); }
}
export function setDraft(text: string) { const f = openFile(); if (f && f.draft !== undefined) openFile.set({ ...f, draft: text }); }
/** `PUT …/file` with the revision the file was read at; a stale write keeps the text and says so. */
export async function saveFile() {
  const f = openFile(); if (!f || f.draft === undefined || f.saving || !f.project || !fileDirty()) return;
  openFile.set({ ...f, saving: true });
  try {
    const done = await request<{ revision: string }>("PUT", `/api/project/${f.project}/file`, { query: { path: f.path, session: f.session }, body: { content: f.draft, expected_revision: f.revision } });
    const cur = openFile();
    if (cur?.path === f.path) openFile.set({ ...cur, content: f.draft, revision: done.revision, saving: false, stale: false });
    toast("success", "Saved");
  } catch (e) {
    const cur = openFile();
    const stale = e instanceof ApiError && e.code === "stale_file";
    if (cur?.path === f.path) openFile.set({ ...cur, saving: false, stale: stale || cur.stale });
    toast(stale ? "warning" : "error", stale ? "The file changed on disk. Reload it; your text stays." : `Save failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
export async function loadGit(session: string) {
  gitStatus.set(null);
  try { const data = await request("GET", `/api/session/${session}/git`); if (viewed()?.id === session) gitStatus.set({ session, data }); }
  catch (e) { if (viewed()?.id === session) gitStatus.set({ session, error: String(e) }); }
}

// --- derived ------------------------------------------------------------------------------
export const viewed = (): Session | null => {
  m();
  const id = model.selected?.sessionId;
  return id ? model.sessions.get(id) ?? null : null;
};
export const live = () => connection().kind === "live";

/**
 * A session and its parents, root last. A missing parent or a cycle stops the walk and
 * treats the viewed session as its own root, so malformed data cannot hide its tab.
 */
function lineage(id: string | null | undefined): Session[] {
  const start = id ? model.sessions.get(id) : undefined;
  if (!start) return [];
  const chain: Session[] = [];
  const seen = new Set<string>();
  let current = start;
  while (true) {
    seen.add(current.id);
    chain.push(current);
    if (!current.parent_id) break;
    const parent = model.sessions.get(current.parent_id);
    if (!parent || seen.has(parent.id)) return [start];
    current = parent;
  }
  return chain;
}

/** The root of a session's tree (itself when it has no parent or its chain is malformed). */
export function rootOf(id: string | null | undefined): string | null {
  return lineage(id).at(-1)?.id ?? null;
}

/** A session's ancestors and itself, root first. */
export function ancestorsOf(id: string | null | undefined): Session[] {
  return lineage(id).reverse();
}

/** Every session below `root`, oldest first: the subagent strip's entries. */
export function subagentsOf(root: string): Session[] {
  const tree = model.tree(root);
  return [...model.sessions.values()]
    .filter((s) => s.id !== root && tree.includes(s.id))
    .sort((a, b) => a.created - b.created || (a.id < b.id ? -1 : 1));
}

const lastProjectSession = new Map<string, string>();

/** Archived sessions the user explicitly reopened after a snapshot; a reconnect must not close them again. */
const reopenedArchived = new Set<string>();

/** The header's tab ids: root sessions of the current project, plus the viewed root. */
export function visibleTabIds(): string[] {
  m();
  const ids = openedTabs().filter((id) => {
    const s = model.sessions.get(id);
    return s && !s.parent_id && s.project_id === currentProject();
  });
  const root = rootOf(model.selected?.sessionId);
  return root && model.sessions.get(root)?.project_id === currentProject() && !ids.includes(root) ? [...ids, root] : ids;
}

/**
 * An archive event closes its sessions' tabs, from every source. Desktop focus moves to the
 * neighbour as `session.tab_close` does; a phone goes back to its Sessions tab.
 */
function closeArchivedTabs() {
  const event = model.archivedTabs;
  const snapshot = model.archivedTabsSnapshot;
  if (!event.size && !snapshot.size) return;
  const archived = new Set(event);
  for (const id of snapshot) {
    if (reopenedArchived.has(id)) continue;
    archived.add(id);
  }
  for (const id of event) reopenedArchived.delete(id);
  for (const id of [...reopenedArchived]) {
    const s = model.sessions.get(id);
    if (!s || !s.archived) reopenedArchived.delete(id);
  }
  model.archivedTabs = new Set();
  model.archivedTabsSnapshot = new Set();
  if (!archived.size) return;
  const before = visibleTabIds();
  // Remove every archived stored tab, including background projects; focus only
  // moves when the viewed tab was one of them.
  openedTabs.update((tabs) => tabs.filter((tab) => !archived.has(tab)));
  const selected = rootOf(model.selected?.sessionId);
  if (!selected || !archived.has(selected)) return;
  const at = before.indexOf(selected);
  const next = before.slice(at + 1).find((id) => !archived.has(id)) ?? before.slice(0, Math.max(0, at)).reverse().find((id) => !archived.has(id));
  if (phone) {
    phonePage.set("sessions");
    if (next) { model.select(next); centre.set("session"); }
    else { rememberNewSession(currentProject()); model.select(null); centre.set("new_session"); }
    return;
  }
  if (next) void selectSession(next);
  else {
    rememberNewSession(currentProject());
    model.select(null);
    centre.set("new_session");
    bump();
  }
}

export function rememberNewSession(project: string | null | undefined) {
  if (project) lastProjectSession.set(project, "");
}

export async function selectSession(id: string) {
  if (phone) phonePage.set("conversation");
  const ses = model.sessions.get(id);
  // Subagents have no tab: opening one keeps its root's tab (TUI parity).
  const tab = rootOf(id) ?? id;
  openedTabs.update((tabs) => tabs.includes(tab) ? tabs : [...tabs, tab]);
  if (ses?.archived) reopenedArchived.add(tab);
  if (id !== tab && model.sessions.get(id)?.archived) reopenedArchived.add(id);
  model.unreadSessions.delete(id);
  if (ses) {
    lastProjectSession.set(ses.project_id, id);
    currentProject.set(ses.project_id);
    loadComposerData(ses.project_id);
  }
  model.select(id);
  centre.set("session");
  scrolledUp.set(false);
  bump();
  void loadGit(id);
  loadSessionWorktrees(id);
  if (isNarrow()) drawer.set(null);
  void request<any>("GET", `/api/session/${id}`).then((full) => {
    const cur = model.sessions.get(id);
    if (cur && full.revision >= cur.revision) { model.sessions.set(id, { ...cur, ...normalizeSession(full), status: full.status ?? cur.status }); host.changed(); }
  }).catch(() => {});
  // The complete history (messages, turn summaries, starting context); no global snapshot needed.
  try {
    await sync.loadSelected();
  } catch (e) {
    if (e instanceof ApiError && e.status === 404 && model.selected?.sessionId === id) { toast("warning", "That session no longer exists."); model.select(null); centre.set("new_session"); bump(); }
  }
}
export function selectProject(id: string) {
  currentProject.set(id);
  loadComposerData(id);
  loadProjectWorktrees(id);
  const remembered = model.sessions.get(lastProjectSession.get(id) ?? "");
  const first = remembered && remembered.project_id === id && !remembered.archived
    ? remembered : lastProjectSession.has(id) ? undefined : model.sessionsRecent().find((s) => s.project_id === id && !s.parent_id && !s.archived);
  if (first) void selectSession(first.id);
  else { model.select(null); centre.set("new_session"); bump(); }
}
