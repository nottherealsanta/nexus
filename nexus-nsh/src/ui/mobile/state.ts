// Phone-only UI state and actions (plans/05 section 5.2). Kept apart from the shared store and kit so the desktop
// work does not conflict; nothing here is reachable on desktop or web.
import { signal } from "nsh";
import { ApiError, apiBase, newKey, request } from "../../core/transport";
import { pushSample } from "../../mobile";
import { archive } from "../../commands";
import { currentProject, loadComposerData, model, phonePage, scopeAll, sync, toast } from "../../store";
import { m } from "../../store";

/** The session drawer over an open conversation. */
export const phoneDrawer = signal(false);
/** The row menu (long press, or the header's overflow): the session it is for, and whether it is renaming. */
export const rowSheet = signal<{ id: string; renaming: boolean } | null>(null);
/** The project switcher sheet. */
export const projectSheet = signal(false);

export function openDrawer() { rowSheet.set(null); projectSheet.set(false); phoneDrawer.set(true); }
export function closeDrawer() { phoneDrawer.set(false); }
/** A session picked in the drawer opens (the store closes nothing on a phone), and the drawer goes. */
export function leaveDrawer() { phoneDrawer.set(false); }

/** The project switcher both filters the list and sets the project for new sessions (user, 2026-11-11). */
export function chooseProject(id: string | null) {
  if (id === null) { scopeAll.set(true); }
  else { scopeAll.set(false); currentProject.set(id); loadComposerData(id); }
  projectSheet.set(false);
}
export function projectLabel(): string {
  m();
  const id = currentProject();
  return scopeAll() ? "All projects" : id ? model.projects.get(id)?.name ?? "Project" : "All projects";
}

export async function renameById(id: string, title: string) {
  const s = model.sessions.get(id);
  const next = title.trim();
  if (!s || !next || next === s.title) { rowSheet.set(null); return; }
  rowSheet.set(null);
  try { await request("PATCH", `/api/session/${id}`, { body: { title: next }, ifMatch: s.revision, key: newKey() }); toast("success", "Session renamed"); }
  catch (e) {
    if (e instanceof ApiError && e.isConflict) toast("warning", "Someone else changed this session; reloaded. Try again.");
    else toast("error", e instanceof ApiError ? e.message : String(e));
  }
}
export async function archiveById(id: string) {
  const wasOpen = phonePage() === "conversation" && model.selected?.sessionId === id;
  rowSheet.set(null);
  await archive(id);
  if (wasOpen) phonePage.set("sessions");
}
/** Pull to refresh: ask the event stream to reload the snapshot. */
export function refreshSessions() { try { sync.restart(); } catch { /* offline: the connection banner says so */ } }

// --- Settings: the drill-in page and the connection card's health trace -------------------------------------------
/** The Settings section open as a pushed page, or null for the grouped list. */
export const settingsDrill = signal<string | null>(null);
/** Round trips to Core in ms (null: the probe failed), newest last. */
export const latency = signal<(number | null)[]>([]);

let probing = false;
/** One timed `GET /api/info`: the connection card's latency figure and trace. */
export async function probeLatency() {
  if (probing) return;
  probing = true;
  const start = Date.now();
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 4000);
  try {
    await request("GET", "/api/info", { signal: abort.signal });
    latency.update((l) => pushSample(l, Date.now() - start));
  } catch { latency.update((l) => pushSample(l, null)); }
  finally { clearTimeout(timer); probing = false; }
}
/** Where Core is: the address the client talks to (the page's own origin in a browser). */
export function coreAddress(): string {
  const base = apiBase();
  if (base) return base;
  try { return String((globalThis as any).location?.origin ?? ""); } catch { return ""; }
}
