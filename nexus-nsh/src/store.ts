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
}

export function startApp() {
  void fetch("/__nexus/host").then((r) => r.json()).then((h) => { host.canStart = !!h.canStart; }).catch(() => {});
  sync.start();
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
  if (ses) currentProject.set(ses.project_id);
  model.select(id);
  centre.set("session");
  scrolledUp.set(false);
  bump();
  void loadGit(id);
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
  const first = model.sessionsRecent().find((s) => s.project_id === id && !s.parent_id && !s.archived);
  if (first) void selectSession(first.id);
  else { model.select(null); centre.set("new_session"); bump(); }
}
