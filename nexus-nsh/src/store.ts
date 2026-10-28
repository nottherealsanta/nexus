// Reactive app state: the model (a mutable cache, versioned by `rev`), connection, banners,
// alerts (toasts + banner history), and view state (layout, tabs, centre view, overlays).
import { Storage, computed, signal, windowSize } from "nsh";
import { Model, normalizeSession } from "./core/reduce";
import { Banner, ConnectionState, Sync, SyncHost } from "./core/sync";
import { Compat, Info, Session } from "./core/api";
import { ApiError, request } from "./core/transport";

export type CentreView = "session" | "new_session" | "settings" | "editor" | "file";
export type Toast = { id: number; level: "success" | "info" | "warning" | "error"; text: string; at: number };

export const model = new Model();
export const rev = signal(0);
export const connection = signal<ConnectionState>({ kind: "connecting" });
export const info = signal<Info | null>(null);
export const userHome = signal("");
export const modelCatalog = signal<any[]>([]);
export const catalogSource = signal("");
export const defaultModel = signal("");
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
export const draft = signal<Record<string, string>>({});
export const waitIndex = signal(0);
export const tick = signal(Date.now());
export const settingsSection = signal<string | null>(null);
export const themeChoice = signal<"system" | "light" | "dark">("system");
export const prefs = signal<Record<string, boolean | number | string>>({});
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
    }
  } catch {}
}
export function savePrefs() {
  try { void Storage.setItem("nexus.prefs", JSON.stringify({ prefs: prefs(), theme: themeChoice(), leftOpen: leftOpen(), rightOpen: rightOpen(), leftTab: leftTab() })); } catch {}
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
const directoryEventsSeen = new Map<string, number>();

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
export function toast(level: Toast["level"], text: string) {
  const t: Toast = { id: toastId++, level, text, at: Date.now() };
  toasts.update((l) => [...l.slice(-2), t]);
  notifications.update((l) => [t, ...l].slice(0, 100));
  unread.update((n) => n + 1);
  setTimeout(() => toasts.update((l) => l.filter((x) => x.id !== t.id)), level === "error" ? 8000 : 4000);
}
export const dismissToast = (id: number) => toasts.update((l) => l.filter((x) => x.id !== id));

// --- sync ---------------------------------------------------------------------------------
const host: SyncHost = {
  model,
  canStart: true,
  setConnection(c) { connection.set(c); },
  setInfo(i, c) { info.set(i); compat.set(c); },
  addBanner(b) { banners.update((l) => [...l.filter((x) => x.kind !== b.kind), b]); },
  changed() { bump(); reconcileSelection(); },
  snapshotApplied() { reconcileSelection(); },
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
    const newest = model.selected?.events.filter((e) => e.type === "session.directory_changed").at(-1);
    if (newest && newest.id > (directoryEventsSeen.get(selected) ?? 0)) {
      directoryEventsSeen.set(selected, newest.id);
      loadSessionWorktrees(selected, true);
    }
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

export type FileEntry = { name: string; kind: string; git_status?: string | null; ignored: boolean };
export const fileListing = signal<{ path: string; entries: FileEntry[]; error?: string } | null>(null);
export const openFile = signal<{ path: string; content?: string; error?: string } | null>(null);
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
  openFile.set({ path }); centre.set("file");
  try {
    const data = await request<{ content: string }>("GET", `/api/project/${project}/file`, { query: { path, session } });
    if (seq === filesRequest) openFile.set({ path, content: data.content });
  } catch (e) { if (seq === filesRequest) openFile.set({ path, error: String(e) }); }
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
    if (cur && full.revision >= cur.revision) { model.sessions.set(id, { ...cur, ...normalizeSession(full), status: full.status ?? cur.status }); bump(); }
  }).catch(() => {});
  // The complete history (messages, diagnostics, starting context); no global snapshot needed.
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
