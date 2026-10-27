// Command registry built from interface/manifest.toml (generated to src/gen/manifest.ts).
// Ids, titles, keys, `when` rules, reasons and placements come from the manifest; this file
// only evaluates terms and binds handlers for the actions whose Core API exists.
import { Clipboard, keymap, openUrl } from "nsh";
import { manifest } from "./gen/manifest";
import {
  addProjectOpen, archivedFilter, banners, bump, centre, confirmDialog, connection, currentProject, dismissToast, drawer,
  draft, isNarrow, leftOpen, leftTab, live, m, model, notifications, paletteOpen, previousCentre, renaming, rightOpen,
  picker, savePrefs, scopeAll, scrolledUp, selectProject, selectSession, sync, toast, toasts, unread, viewed, waitIndex,
} from "./store";
import { ApiError, newKey, request } from "./core/transport";
import { Message } from "./core/api";

export interface Cmd {
  id: string; title: string; category: string; slash?: string; keys?: { tui?: string; nsh?: string };
  key_context?: string; when: string[]; unavailable_reason?: string; placement: string[]; status?: string; clients?: string; note?: string;
}
export const commands: Cmd[] = (manifest.command as unknown as Cmd[]).filter((c) => (c.clients ?? "both") !== "tui");
export const byId = new Map(commands.map((c) => [c.id, c]));

// --- context terms ----------------------------------------------------------------------
export const textEdit = { active: () => renaming() };

export function waitItems(): { kind: "approval" | "question"; id: string; session_id: string }[] {
  const id = model.selected?.sessionId;
  const tree = id ? model.tree(id) : [];
  const out: { kind: "approval" | "question"; id: string; session_id: string }[] = [];
  for (const a of model.approvals.values()) if (tree.includes(a.session_id)) out.push({ kind: "approval", id: a.id, session_id: a.session_id });
  for (const q of model.questions.values()) if (tree.includes(q.session_id)) out.push({ kind: "question", id: q.id, session_id: q.session_id });
  return out;
}
export const otherWaits = () => {
  const id = model.selected?.sessionId;
  const tree = id ? model.tree(id) : [];
  const set = new Set<string>();
  for (const a of model.approvals.values()) if (!tree.includes(a.session_id)) set.add(a.session_id);
  for (const q of model.questions.values()) if (!tree.includes(q.session_id)) set.add(q.session_id);
  return [...set];
};

export function lastTurnIncomplete(msgs: Message[]): boolean {
  if (!msgs.length) return false;
  const last = msgs[msgs.length - 1];
  if (last.role === "tool" || last.role === "user") return true;
  return last.parts.some((p) => p.type === "tool_call");
}

export const draftKey = () => (centre() === "new_session" ? `new:${currentProject() ?? ""}` : viewed()?.id ?? "");

export function term(t: string): boolean {
  m(); connection();
  const neg = t.startsWith("!");
  const name = neg ? t.slice(1) : t;
  const v = evalTerm(name);
  return neg ? !v : v;
}
function evalTerm(t: string): boolean {
  const ses = viewed();
  const inSession = centre() === "session" && !!ses;
  switch (t) {
    case "always": return true;
    case "has_project": return !!currentProject();
    case "has_session": return inSession;
    case "has_turns": return inSession && (model.selected?.messages.some((x) => x.role === "user") ?? false);
    case "session.idle": return inSession && ses!.status !== "busy";
    case "session.busy": return inSession && ses!.status === "busy";
    case "session.interrupted": return inSession && ses!.status !== "busy" && lastTurnIncomplete(model.selected?.messages ?? []);
    case "session.archived": return inSession && !!ses!.archived;
    case "session.child": return inSession && !!ses!.parent_id;
    case "session.in_worktree": return false;
    case "has_waiting": return model.approvals.size + model.questions.size > 0;
    case "waiting.approval": { const w = waitItems()[waitIndex() % Math.max(1, waitItems().length)]; return w?.kind === "approval"; }
    case "waiting.question": { const w = waitItems()[waitIndex() % Math.max(1, waitItems().length)]; return w?.kind === "question"; }
    case "waiting.multiple": return waitItems().length > 1;
    case "composer.has_text": return (draft()[draftKey()] ?? "").trim().length > 0;
    case "composer.empty": return (draft()[draftKey()] ?? "").trim().length === 0;
    case "has_queued": return false;
    case "timeline.scrolled_up": return scrolledUp();
    case "timeline.has_older": return !!model.selected?.messagesMore;
    case "view.settings": return centre() === "settings";
    case "view.editor": return centre() === "editor";
    case "overlay.open": return paletteOpen() || !!picker();
    case "dialog.open": return !!confirmDialog() || addProjectOpen() || connection().kind === "incompatible";
    case "alert.open": return toasts().length > 0;
    case "drawer.open": return !!drawer();
    case "layout.narrow": return isNarrow();
    case "text_edit.active": return textEdit.active();
    case "connection.lost": return ["reconnecting", "offline", "auth_failed"].includes(connection().kind);
    case "connection.no_daemon": { const c = connection(); return c.kind === "offline" && c.noDaemon; }
    case "provider.signed_in": return providersSignedIn();
    case "has_notifications": return notifications().length > 0;
    default: return false; // focus.*, editor.dirty, tool.*: no per-item focus tracking in this increment
  }
}
let signedIn = false;
export const providersSignedIn = () => signedIn;
export async function loadProviders() { try { const p = await request<any[]>("GET", "/api/provider"); signedIn = p.some((x) => x.connected); bump(); return p; } catch { return []; } }

// --- handlers -----------------------------------------------------------------------------
/** Handlers for commands whose Core API exists (or that are pure client navigation). `mutates` = needs a live connection. */
interface Handler { run: () => void | Promise<void>; mutates?: boolean }
export const handlers = new Map<string, Handler>();
const on = (id: string, run: Handler["run"], mutates = false) => handlers.set(id, { run, mutates });

export function toggleLeft() { if (isNarrow()) drawer.set(drawer() === "left" ? null : "left"); else { leftOpen.update((v) => !v); savePrefs(); } }
export function rightIsOpen(w: number) { return rightOpen() ?? w >= 1280; }
export function toggleRight(w: number) {
  if (isNarrow()) drawer.set(drawer() === "right" ? null : "right");
  else { rightOpen.set(!rightIsOpen(w)); savePrefs(); }
}
export function openSettings() {
  if (centre() === "settings") { centre.set(previousCentre()); return; }
  previousCentre.set(centre()); centre.set("settings"); if (isNarrow()) drawer.set(null);
}
function tab(t: "sessions" | "files" | "projects") { leftTab.set(t); savePrefs(); if (isNarrow()) drawer.set("left"); else leftOpen.set(true); }

export function newSession() { model.select(null); centre.set("new_session"); if (isNarrow()) drawer.set(null); bump(); }

export async function sendPrompt(text: string): Promise<boolean> {
  text = text.trim();
  if (!text) return false;
  try {
    let ses = viewed();
    if (centre() === "new_session" || !ses) {
      const project = currentProject();
      if (!project) throw new ApiError(400, "Add or choose a project first.");
      const created = await request<any>("POST", "/api/session", { body: { project }, key: newKey() });
      await selectSession(created.id);
      ses = created;
    }
    await request("POST", `/api/session/${ses!.id}/prompt`, { body: { text }, key: newKey() });
    return true;
  } catch (e) {
    toast("error", e instanceof ApiError ? e.message : String(e));
    return false;
  }
}

async function patchSession(title: string) {
  const s = viewed(); if (!s) return;
  try { await request("PATCH", `/api/session/${s.id}`, { body: { title }, ifMatch: s.revision, key: newKey() }); toast("success", "Session renamed"); }
  catch (e) {
    if (e instanceof ApiError && e.isConflict) toast("warning", "Someone else changed this session; reloaded. Try again.");
    else toast("error", e instanceof ApiError ? e.message : String(e));
  }
}
export { patchSession as renameSession };

export function askDelete(id?: string) {
  const s = id ? model.sessions.get(id) : viewed(); if (!s) return;
  const n = model.tree(s.id).length - 1;
  confirmDialog.set({
    title: "Delete session",
    body: `Delete “${s.title || "Untitled"}”${n ? ` and its ${n} child session${n === 1 ? "" : "s"}` : ""}?\nThis cannot be undone.`,
    confirm: "Delete", destructive: true,
    run: async () => { await request("DELETE", `/api/session/${s.id}`, { key: newKey() }); toast("success", "Session deleted"); },
  });
}
export async function archive(id?: string, undo = false) {
  const s = id ? model.sessions.get(id) : viewed(); if (!s) return;
  const root = (() => { let c = s; while (c.parent_id && model.sessions.get(c.parent_id)) c = model.sessions.get(c.parent_id)!; return c; })();
  try { await request("POST", `/api/session/${root.id}/${undo ? "unarchive" : "archive"}`, { key: newKey() }); toast("success", undo ? "Session unarchived" : "Session archived"); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}
export async function interrupt() { const s = viewed(); if (!s) return; try { await request("POST", `/api/session/${s.id}/interrupt`, { key: newKey() }); } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); } }
export async function continueSession() {
  const s = viewed(); if (!s) return;
  try { await request("POST", `/api/session/${s.id}/continue`, { body: {}, key: newKey() }); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}

export const focusComposer = { fn: (() => {}) as () => void };
export const rightWidthHint = { w: 1280 };

on("palette.open", () => paletteOpen.set(true));
on("app.settings", openSettings);
on("app.open_inspector", () => { void openUrl(new URL("/inspector", import.meta.url).href).catch(() => toast("info", "Open /inspector on the host page.")); });
on("app.reconnect", () => sync.retryNow());
on("app.start_daemon", async () => { try { await fetch("/__nexus/start", { method: "POST" }); } catch {} sync.retryNow(); });
on("notifications.open", () => { drawer.set("notifications"); unread.set(0); });
on("notifications.mark_all_read", () => { notifications.set([]); unread.set(0); });
on("alert.dismiss", () => { const t = toasts(); if (t.length) dismissToast(t[t.length - 1].id); });
on("alert.cancel", () => { const d = confirmDialog(); if (d) { if (!d.busy) confirmDialog.set(null); } else if (connection().kind !== "incompatible") addProjectOpen.set(false); });
on("overlay.close", () => { if (picker()) { picker.set(null); return; } paletteOpen.set(false); });
on("nav.drawer.close", () => drawer.set(null));
on("sidebar.left.toggle", toggleLeft);
on("sidebar.right.toggle", () => toggleRight(rightWidthHint.w));
on("sidebar.tab.sessions", () => tab("sessions"));
on("sidebar.tab.files", () => tab("files"));
on("sidebar.tab.projects", () => tab("projects"));
on("sidebar.focus", () => { if (isNarrow()) drawer.set("left"); else leftOpen.set(true); });
on("composer.focus", () => focusComposer.fn());
on("session.new", newSession);
on("session.switch", () => { tab("sessions"); });
on("project.switch", () => tab("projects"));
on("project.add", () => addProjectOpen.set(true), true);
on("session.rename", () => renaming.set(true), true);
on("session.archive", () => archive(), true);
on("session.unarchive", () => archive(undefined, true), true);
on("session.delete", () => askDelete(), true);
on("session.interrupt", interrupt, true);
on("session.continue", continueSession, true);
on("session.copy_id", () => { const s = viewed(); if (s) void Clipboard.writeText(s.id).then(() => toast("success", "Copied session id")); });
on("sessions.scope_toggle", () => scopeAll.update((v) => !v));
on("sessions.archived_filter", () => archivedFilter.update((v) => !v));
on("session.jump_latest", () => { scrolledUp.set(false); jump.fn(); });
on("waiting.next", () => waitIndex.update((n) => n + 1));
on("waiting.prev", () => waitIndex.update((n) => Math.max(0, n - 1)));
on("waiting.focus", () => focusWaiting.fn());
on("waiting.go_to", () => { const id = otherWaits()[0]; if (id) void selectSession(id); });
on("app.theme", () => { openSettings(); });
on("session.export", () => {
  const s = viewed();
  if (!s) return;
  const url = new URL(`/api/session/${s.id}/export?format=markdown&scope=session`, import.meta.url);
  void openUrl(url.href).catch(() => toast("info", "Export is served at /api/session/{id}/export on the host."));
});

/** Replace the picker with a Core-backed list. */
async function pickOne(title: string, load: () => Promise<{ id: string; label: string; hint?: string; run: () => void | Promise<void> }[]>) {
  picker.set({ title, items: [], note: "loading…" });
  try {
    const items = await load();
    picker.set({ title, items, note: items.length ? undefined : "Nothing to choose from." });
  } catch (e) {
    picker.set({ title, items: [], note: e instanceof ApiError ? e.message : String(e) });
  }
}

function choose(label: string, run: () => void | Promise<void>) {
  return async () => { picker.set(null); await run(); };
}

function applyModel(model: string) {
  return async () => {
    const s = viewed();
    if (!s) return;
    try {
      await request("POST", `/api/session/${s.id}/model`, { body: { model }, ifMatch: s.revision, key: newKey() });
      toast("success", "Model changed from the next call");
    } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
  };
}

on("model.change", () => void pickOne("Change model", async () => {
  const cat = await request<any>("GET", "/api/model");
  const models = (cat.models ?? []) as any[];
  return models.map((m) => ({
    id: m.ref,
    label: `${m.ref}${m.name ? ` · ${m.name}` : ""}`,
    hint: [m.context ? `${Math.round(m.context / 1000)}k context` : "", (m.efforts ?? []).join("/")].filter(Boolean).join(" · "),
    run: choose(m.ref, applyModel(m.ref)),
  }));
}));
on("agent.change", () => void pickOne("Change agent", async () => {
  const s = viewed();
  const res = await request<any>("GET", "/api/agent", { query: { project: s?.project_id } });
  return ((res.agents ?? []) as any[]).map((a) => ({
    id: a.name,
    label: a.name,
    hint: [a.source, a.mode].filter(Boolean).join(" · "),
    run: choose(a.name, async () => {
      if (!s) return;
      try {
        await request("POST", `/api/session/${s.id}/agent`, { body: { agent: a.name }, ifMatch: s.revision, key: newKey() });
        toast("success", "Agent changed from the next call");
      } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
    }),
  }));
}));
on("model.effort", () => void pickOne("Reasoning effort", async () => {
  const s = viewed();
  const cat = await request<any>("GET", "/api/model");
  const cur = (cat.models ?? []).find((m: any) => m.ref === s?.model);
  const efforts: string[] = cur?.efforts?.length ? cur.efforts : ["low", "medium", "high"];
  const apply = async (effort: string) => {
    if (!s) return;
    try {
      await request("PATCH", `/api/session/${s.id}`, { body: { effort }, ifMatch: s.revision, key: newKey() });
      toast("success", effort ? `Effort set to ${effort}` : "Effort follows the agent/provider");
    } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
  };
  return [
    ...efforts.map((e) => ({ id: e, label: e, hint: "reasoning effort", run: choose(e, () => apply(e)) })),
    { id: "(default)", label: "(default)", hint: "follow the agent/provider", run: choose("(default)", () => apply("")) },
  ];
}));
export const jump = { fn: () => {} };
export const focusWaiting = { fn: () => {} };

// Reasons for commands the manifest marks as not API-ready (shown disabled, never hidden).
const BLOCKED: Record<string, string> = {
  "app.sign_in": "Sign-in over the API is not in Core yet; use `nexus login` in a terminal.",
  "app.sign_out": "Sign-out over the API is not in Core yet.",
};

export interface Availability { ok: boolean; reason?: string }
export function availability(c: Cmd): Availability {
  if (c.status === "blocked_core") return { ok: false, reason: BLOCKED[c.id] ?? `Needs a Core increment (PLAN.md Step 12a). ${c.unavailable_reason ?? ""}`.trim() };
  if (c.status === "deferred") return { ok: false, reason: `Deferred: ${c.unavailable_reason ?? "not built yet"}` };
  const h = handlers.get(c.id);
  const whenOk = c.when.every(term);
  if (!whenOk) return { ok: false, reason: c.unavailable_reason ?? "Not available right now." };
  if (!h) return { ok: false, reason: "Not built in this NSH increment yet." };
  if (h.mutates && !live()) return { ok: false, reason: "Core is not connected: the view is read-only." };
  return { ok: true };
}

export async function run(id: string) {
  const c = byId.get(id); if (!c) return;
  const a = availability(c);
  if (!a.ok) { toast("info", `${c.title}: ${a.reason}`); return; }
  await handlers.get(id)!.run();
}

// --- keys --------------------------------------------------------------------------------
/** `cmd` becomes `mod` (Cmd on Apple platforms, Ctrl elsewhere). */
export const toKeystroke = (k: string) => k.replace(/^cmd(\+|$)/, "mod$1").replace("+cmd+", "+mod+");
export function keyLabel(c: Cmd): string | null {
  const k = c.keys?.nsh; if (!k) return null;
  if (k === "esc") return "esc";
  try { return keymap.label(toKeystroke(k)); } catch { return k; }
}

const GLOBAL_CONTEXTS = new Set(["global"]);
/** Bare keys (enter, space, [, ], @, /, i) belong to the focused control or text field, never a global binding. */
const hasModifier = (k: string) => /(^|\+)(cmd|ctrl|alt)(\+|$)/.test(k) || /^f\d+$/.test(k);
/** Esc is not a per-command lookup: the manifest's ordered rules decide (interface/README.md "Esc precedence"). */
export function escape() {
  for (const rule of manifest.keymap.escape as readonly { id: string; when: readonly string[]; action: string }[]) {
    if (rule.when.every(term)) {
      if (rule.id === "text_edit") { renaming.set(false); return rule.id; }
      if (rule.id === "alert") { void run("alert.dismiss"); return rule.id; }
      if (rule.id === "interrupt") { void run("session.interrupt"); return rule.id; }
      if (rule.id === "back") { const s = viewed(); if (s?.parent_id) void selectSession(s.parent_id); return rule.id; }
      if (rule.id === "editor") return rule.id;
      void run(rule.action);
      return rule.id;
    }
  }
  return null;
}

export function bindKeys(): () => void {
  const offs: (() => void)[] = [];
  for (const c of commands) {
    const k = c.keys?.nsh;
    if (!k || k === "esc" || !GLOBAL_CONTEXTS.has(c.key_context ?? "") || !hasModifier(k)) continue;
    if (c.id === "app.quit") continue; // platform-owned
    try { offs.push(keymap.bind(toKeystroke(k), () => { void run(c.id); }, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  }
  try { offs.push(keymap.bind("escape", () => { escape(); }, { when: "always" })); } catch {}
  return () => offs.forEach((f) => f());
}
void selectProject;
