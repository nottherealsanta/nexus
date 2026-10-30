// Reactive app state: the model (a mutable cache, versioned by `rev`), connection, banners,
// alerts (toasts + banner history), and view state (layout, tabs, centre view, overlays).
import { Storage, computed, signal, windowSize } from "nsh";
import { Model, normalizeSession } from "./core/reduce";
import { Banner, ConnectionState, MESSAGE_PAGE, Sync, SyncHost } from "./core/sync";
import { Compat, Info, Message, Session } from "./core/api";
import { ApiError, request } from "./core/transport";
import { emptyGlobalToastState, globalEventToast } from "./skills-mcp";
import { receiveVoiceProgress } from "./voice-state";

export type CentreView = "session" | "new_session" | "settings" | "editor" | "file";
export type Toast = { id: number; level: "success" | "info" | "warning" | "error"; text: string; at: number };

export const model = new Model();
export const contextDetail = signal<{ title: string; detail: string } | null>(null);
export const rev = signal(0);
export const connection = signal<ConnectionState>({ kind: "connecting" });
export const info = signal<Info | null>(null);
export const userHome = signal("");
export const modelCatalog = signal<any[]>([]);
export const catalogSource = signal("");
export const defaultModel = signal("");
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
  if (inputSeq.get(id) === seq) pendingInputs.update((all) => ({ ...all, [id]: data }));
}
// Prompt history: this project's sent prompts from Core, newest first (Up in the composer).
export const promptHistory = signal<{ project: string; entries: string[] }>({ project: "", entries: [] });
export async function loadPromptHistory(project: string) {
  const data = await request<{ prompts: { text: string; image_count: number }[] }>("GET", `/api/project/${project}/prompts`, { query: { limit: 200 } });
  const entries = data.prompts.map((p) => p.image_count ? `${p.text}${p.text ? "\n" : ""}[${p.image_count} image${p.image_count === 1 ? "" : "s"}]` : p.text).filter(Boolean);
  promptHistory.set({ project, entries });
}
export const contextBudget = signal<Record<string, { used: number; window: number | null; exact: boolean }>>({});
export async function loadContext(id: string) { const data = await request<any>("GET", `/api/session/${id}/context`); contextBudget.update((all) => ({ ...all, [id]: data })); }
function applyConfig(c: any) {
  config.set(c);
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
export const compat = signal<Compat>({ kind: "unreported" });
export const banners = signal<Banner[]>([]);
export const dismissedBanners = signal<string[]>([]);
export const toasts = signal<Toast[]>([]);
export const notifications = signal<Toast[]>([]);
export const unread = signal(0);

// --- view state ---------------------------------------------------------------------------
export const leftOpen = signal(true);
export const rightOpen = signal<boolean | null>(null); // null = default by width
export const leftTab = signal<"sessions" | "files" | "projects">("sessions");
export const centre = signal<CentreView>("session");
export const previousCentre = signal<CentreView>("session");
export const currentProject = signal<string | null>(null);
/** Sessions opened in the workspace header, in the order they were opened. */
export const openedTabs = signal<string[]>([]);
export const scopeAll = signal(false);
export const archivedFilter = signal(false);
export const drawer = signal<"left" | "right" | "notifications" | null>(null);
export const paletteOpen = signal(false);
/** A simple list picker for catalog-backed choices (model, effort, agent). */
export type PickerItem = { id: string; label: string; hint?: string; run: () => void | Promise<void> };
export const picker = signal<null | { title: string; items: PickerItem[]; note?: string }>(null);
export const confirmDialog = signal<null | { title: string; body: string; confirm: string; destructive: boolean; run: () => Promise<void> | void; busy?: boolean; error?: string }>(null);
export const renaming = signal(false);
export const addProjectOpen = signal(false);
/** Choices made before a session exists; consumed by its creation request. */
export const sessionDraft = signal<{ agent?: string; model?: string; effort?: string; disabled_tools?: string[] }>({});
export const draft = signal<Record<string, string>>({});
/** Core's starting context (PLAN K4) for the draft in `key`; a newer draft ignores a late older answer. */
export const draftContext = signal<{ key: string; data: { used: number; window: number | null; parts: { instructions: number; tools: number; instruction_file: number }; tools: string[]; skills: any[]; mcp: any[] } } | null>(null);
let draftSeq = 0;
export const draftContextKey = (project: string, d = sessionDraft()) => JSON.stringify([project, d.agent ?? null, d.model ?? null, d.effort ?? null, d.disabled_tools ?? null]);
export async function loadDraftContext(project: string) {
  const d = sessionDraft(), key = draftContextKey(project, d), seq = ++draftSeq;
  try {
    const data = await request<any>("POST", "/api/draft/context", { body: { project, agent: d.agent, model: d.model || undefined, effort: d.effort || undefined, disabled_tools: d.disabled_tools } });
    if (seq === draftSeq) draftContext.set({ key, data });
  } catch { /* the client-side estimate stays */ }
}
export const waitIndex = signal(0);
export const tick = signal(Date.now());
export const settingsSection = signal<string | null>(null);
export const themeChoice = signal<"system" | "light" | "dark">("system");
export const prefs = signal<Record<string, boolean | number | string>>({});
export type TimelineDetail = "low" | "medium" | "high";
export const timelineDetail = signal<TimelineDetail>("medium");
/** Explicit open/closed state for timeline groups and tool details. */
export const timelineOpen = signal<Record<string, boolean>>({});
export const scrolledUp = signal(false);
export type SidebarRowTarget = { kind: "session" | "project"; id: string };
export const focusedSidebarRow = signal<SidebarRowTarget | null>(null);
export const sidebarRowMenu = signal<SidebarRowTarget | null>(null);
export const focusedTool = signal<{ key: string; clipped: boolean; toggleRaw: () => void } | null>(null);
export const toolOutputAll = signal<Record<string, boolean>>({});

export const width = () => windowSize().width;
export const NARROW = 820;
export const isNarrow = computed(() => windowSize().width < NARROW);

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
    }
  } catch {}
}
export function savePrefs() {
  try { void Storage.setItem("nexus.prefs", JSON.stringify({ prefs: prefs(), theme: themeChoice(), leftOpen: leftOpen(), rightOpen: rightOpen(), leftTab: leftTab(), timelineDetail: timelineDetail() })); } catch {}
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
let defaultAsked = false;
const agentCache = new Map<string, any[]>();
const agentRequests = new Set<string>();
const sessionWorktreeRequests = new Set<string>();
const projectWorktreeRequests = new Set<string>();
const sessionWorktreeSeq = new Map<string, number>();
const projectWorktreeSeq = new Map<string, number>();
/** The session directory each session's worktree listing was requested for. */
const worktreeDirectories = new Map<string, string>();

export function loadComposerData(project = currentProject()) {
  if (!modelsAsked) {
    modelsAsked = true;
    void reloadModelCatalog().catch(() => { modelsAsked = false; });
  }
  if (!defaultAsked) {
    defaultAsked = true;
    void request<any>("GET", "/api/model/default").then((r) => defaultModel.set(r.model ?? "")).catch(() => { defaultAsked = false; });
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

export function currentWorktree() {
  m();
  const s = viewed();
  const p = currentProject();
  const directory = s?.directory ?? (p ? model.projects.get(p)?.path : undefined);
  if (!directory) return null;
  const listing = s ? sessionWorktrees()[s.id] : p ? projectWorktrees()[p] : null;
  return worktreeForDirectory(directory, listing);
}

// --- alerts -----------------------------------------------------------------------------
let toastId = 1;
/** Toasts drawn at once; the rest queue behind `+N more` and start their timer when first drawn. */
export const TOAST_VISIBLE = 3;
const TOAST_QUEUE = 20;
const timed = new Set<number>();
/** The alert whose full text is open in the detail modal (D15). */
export const alertDetail = signal<Toast | null>(null);
/** One row of `GET /api/usage` (K11): a provider with its plan and limit windows, or the reason it could not be read. */
export interface UsageProvider { id: string; label?: string; plan?: string; error?: string; notes?: string[]; windows?: { label?: string; used_percent?: number | null; resets_at?: number | null; detail?: string }[] }
/** The open plan-limits dialog (`app.usage`); `null` when closed. */
export const usageDialog = signal<{ providers: UsageProvider[]; loading: boolean } | null>(null);
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
  setConnection(c) { connection.set(c); },
  setInfo(i, c) { info.set(i); compat.set(c); },
  addBanner(b) { banners.update((l) => [...l.filter((x) => x.kind !== b.kind), b]); },
  changed() { bump(); reconcileSelection(); autoLoadOlder(); },
  snapshotApplied() { reconcileSelection(); void loadConfig().catch(() => {}); },
  configChanged() { void loadConfig().catch(() => {}); },
  contextChanged(id) { void loadContext(id).catch(() => {}); },
  inputsChanged(id) { void loadInputs(id).catch(() => {}); },
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
    else handleGlobalEvent(e);
  },
};
export const sync = new Sync(host);

function reconcileSelection() {
  if (model.selectedDeleted) {
    toast("info", "That session was deleted.");
    model.selectedDeleted = null;
    centre.set("new_session");
  }
  if (currentProject() && !model.projects.has(currentProject()!)) currentProject.set(null);
  if (!currentProject()) {
    const sel = model.selected && model.sessions.get(model.selected.sessionId);
    const p = sel?.project_id ?? model.projectsRecent()[0]?.id ?? null;
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
  void fetch("/__nexus/host").then((r) => r.json()).then((h) => {
    host.canStart = !!h.canStart;
    if (typeof h.userHome === "string") userHome.set(h.userHome);
  }).catch(() => {});
  sync.start();
  loadComposerData();
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
  if (e.type === "files.changed") {
    const f = openFile();
    const d = e.data as { project_id?: string; path?: string; revision?: string };
    if (f && !f.saving && d.project_id === f.project && d.path === f.path && d.revision !== f.revision) {
      if (!fileDirty()) void reloadFile();
      else { openFile.set({ ...f, stale: true }); toast("warning", "The file changed on disk: reload before saving"); }
    }
  }
  if (e.type === "skills.changed" && pendingSkillInstalls) deferredSkillsChanged = true;
  const alert = globalEventToast(e.type, e.data, globalToastState);
  if (alert) toast(alert.level, alert.text);
}

export type FileEntry = { name: string; kind: string; git_status?: string | null; ignored: boolean };
export const fileListing = signal<{ path: string; entries: FileEntry[]; error?: string } | null>(null);
/** The open project file. `draft` is set while editing (K10): the text in the editor, against `revision` as read. */
export type OpenFile = { path: string; content?: string; revision?: string; error?: string; draft?: string; saving?: boolean; stale?: boolean; project?: string; session?: string };
export const openFile = signal<OpenFile | null>(null);
export const fileDirty = () => { const f = openFile(); return !!f && f.draft !== undefined && f.draft !== f.content; };
export const gitStatus = signal<{ session: string; data?: any; error?: string } | null>(null);
let filesRequest = 0;
export async function loadFiles(path = "") {
  const project = currentProject(), session = viewed()?.id;
  if (!project) { fileListing.set(null); return; }
  const seq = ++filesRequest;
  fileListing.set(null);
  try {
    const data = await request<{ path: string; entries: FileEntry[] }>("GET", `/api/project/${project}/files`, { query: { path, session } });
    if (seq === filesRequest) fileListing.set(data);
  } catch (e) { if (seq === filesRequest) fileListing.set({ path, entries: [], error: String(e) }); }
}
export async function showFile(path: string) {
  const project = currentProject(), session = viewed()?.id;
  if (!project) return;
  const seq = ++filesRequest;
  openFile.set({ path, project, session }); centre.set("file");
  try {
    const data = await request<{ content: string; revision?: string }>("GET", `/api/project/${project}/file`, { query: { path, session } });
    if (seq === filesRequest) openFile.set({ path, project, session, content: data.content, revision: data.revision });
  } catch (e) { if (seq === filesRequest) openFile.set({ path, project, session, error: String(e) }); }
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
export function editFile() {
  const f = openFile(); if (!f || f.content === undefined || f.draft !== undefined) return;
  if (!f.revision) { toast("warning", "Core did not report a revision for this file"); return; }
  openFile.set({ ...f, draft: f.content });
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

export async function selectSession(id: string) {
  const ses = model.sessions.get(id);
  openedTabs.update((tabs) => tabs.includes(id) ? tabs : [...tabs, id]);
  model.unreadSessions.delete(id);
  if (ses) {
    currentProject.set(ses.project_id);
    loadComposerData(ses.project_id);
  }
  model.select(id);
  centre.set("session");
  scrolledUp.set(false);
  bump();
  void loadGit(id);
  loadSessionWorktrees(id);
  if (leftTab() === "files") void loadFiles();
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
  const first = model.sessionsRecent().find((s) => s.project_id === id && !s.parent_id && !s.archived);
  if (first) void selectSession(first.id);
  else { model.select(null); centre.set("new_session"); bump(); }
}
