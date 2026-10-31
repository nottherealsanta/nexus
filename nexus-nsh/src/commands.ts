import { loadFiles, loadOlderMessages } from "./store";
import type { UsageProvider } from "./store";
// Command registry built from interface/manifest.toml (generated to src/gen/manifest.ts).
// Ids, titles, keys, `when` rules, reasons and placements come from the manifest; this file
// only evaluates terms and binds handlers for the actions whose Core API exists.
import { Clipboard, keymap, openUrl } from "nsh";
import { manifest } from "./gen/manifest";
import {
  editFile, fileDirty, openFile, reloadFile, saveFile,
  pendingInputs, contextDetail, addProjectOpen, alertDetail, usageDialog, archivedFilter, banners, bump, centre, confirmDialog, connection, currentProject, dismissToast, drawer,
  agentCatalog, beginOwnSkillInstall, currentWorktree, draft, rememberedModel, failOwnSkillInstall, finishOwnSkillInstall, isNarrow, leftOpen, leftTab, learnChecklist, live, loadComposerData, loadProjectWorktrees, loadStartingInventory, m, model, notifications, openedTabs, paletteOpen, previousCentre, renaming, rightOpen,
  focusedSidebarRow, focusedTool, picker, rootOf, savePrefs, scopeAll, scrolledUp, selectProject, selectSession, sessionDraft, setTimelineDetail, settingsPage, settingsSection, sidebarRowMenu, sync, timelineDetail, toast, toasts, toolOutputAll, unread, viewed, waitIndex,
} from "./store";
import { ApiError, newKey, request } from "./core/transport";
import { MESSAGE_PAGE } from "./core/sync";
import { Message } from "./core/api";
import { normalizeSession } from "./core/reduce";
import { imageDrafts } from "./images";
import { disabledKey, selectedSkillCounts, setDisabledTool, shouldInstallRequestedDirectly, toggleDisabledTool } from "./skills-mcp";
import { speakAnswer, speechPlaying, stopSpeaking } from "./speech";
import { cancelDictation, isDictationBusy, startDictation, stopDictation } from "./dictation";
import { appendTranscript } from "./dictation-logic";
import { dictationPhase } from "./voice-state";
import { askVoiceDownload, askVoiceRemove, refreshVoiceOnSettingsOpen, voiceCommandReason } from "./voice-settings";

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
    case "session.continued": return inSession && !!ses!.continued_from;
    case "session.continued_on": return inSession && !!ses!.continued_to;
    case "session.in_worktree": return currentWorktree()?.main === false;
    case "has_waiting": return model.approvals.size + model.questions.size > 0;
    case "waiting.approval": { const w = waitItems()[waitIndex() % Math.max(1, waitItems().length)]; return w?.kind === "approval"; }
    case "waiting.question": { const w = waitItems()[waitIndex() % Math.max(1, waitItems().length)]; return w?.kind === "question"; }
    case "waiting.multiple": return waitItems().length > 1;
    case "composer.has_text": return (draft()[draftKey()] ?? "").trim().length > 0 || (imageDrafts()[draftKey()]?.length ?? 0) > 0;
    case "composer.empty": return (draft()[draftKey()] ?? "").trim().length === 0 && !(imageDrafts()[draftKey()]?.length);
    case "has_queued": return !!viewed() && (pendingInputs()[viewed()!.id]?.inputs.length ?? 0) > 0;
    case "timeline.scrolled_up": return scrolledUp();
    case "timeline.has_older": return !!model.selected?.messagesMore;
    case "view.settings": return centre() === "settings";
    case "view.editor": return centre() === "file";
    case "editor.dirty": return fileDirty();
    case "editor.readonly": return centre() === "file" && openFile()?.draft === undefined && openFile()?.content !== undefined;
    case "overlay.open": return paletteOpen() || !!picker() || !!learnChecklist() || !!alertDetail() || !!usageDialog();
    case "dialog.open": return !!confirmDialog() || addProjectOpen() || connection().kind === "incompatible";
    case "alert.open": return toasts().length > 0;
    case "drawer.open": return !!drawer();
    case "layout.narrow": return isNarrow();
    case "text_edit.active": return textEdit.active();
    case "connection.lost": return ["reconnecting", "offline", "auth_failed"].includes(connection().kind);
    case "connection.no_daemon": { const c = connection(); return c.kind === "offline" && c.noDaemon; }
    case "provider.signed_in": return providersSignedIn();
    case "has_notifications": return notifications().length > 0;
    case "focus.tool": return !!focusedTool();
    case "tool.output_clipped_in_ui": return !!focusedTool()?.clipped;
    default: return false; // other focus.*, editor.dirty: no per-item focus tracking in this increment
  }
}
let signedIn = false;
export const providersSignedIn = () => signedIn;
export async function loadProviders() { try { const p = await request<any[]>("GET", "/api/provider"); signedIn = p.some((x) => x.connected); bump(); return p; } catch { return []; } }

// --- handlers -----------------------------------------------------------------------------
/** Handlers for commands whose Core API exists (or that are pure client navigation). `mutates` = needs a live connection. */
interface Handler { run: () => void | Promise<void>; mutates?: boolean }
export const handlers = new Map<string, Handler>();
/** `app.usage`: the plan windows of the providers that report them (K11), in the plan-limits dialog; again while open refreshes. */
async function showUsage() {
  usageDialog.set({ providers: usageDialog()?.providers ?? [], loading: true });
  try {
    const u = await request<{ providers: UsageProvider[] }>("GET", "/api/usage");
    usageDialog.set({ providers: u.providers ?? [], loading: false });
  } catch (e) { usageDialog.set(null); toast("error", `Plan limits failed to load: ${e instanceof ApiError ? e.message : String(e)}`); }
}

const on = (id: string, run: Handler["run"], mutates = false) => handlers.set(id, { run, mutates });

/** Copies text to the system clipboard and confirms it (the timeline's message copy). */
export function copyText(text: string, done: string) { void Clipboard.writeText(text).then(() => toast("success", done)); }
export function toggleLeft() { if (isNarrow()) drawer.set(drawer() === "left" ? null : "left"); else { leftOpen.update((v) => !v); savePrefs(); } }
export function rightIsOpen(w: number) { return rightOpen() ?? w >= 1280; }
export function toggleRight(w: number) {
  if (isNarrow()) drawer.set(drawer() === "right" ? null : "right");
  else { rightOpen.set(!rightIsOpen(w)); savePrefs(); }
}
export function openSettings() {
  if (centre() === "settings") { centre.set(previousCentre()); return; }
  previousCentre.set(centre()); centre.set("settings"); if (isNarrow()) drawer.set(null);
  refreshVoiceOnSettingsOpen();
}
on("app.sign_in", () => { if (centre() !== "settings") openSettings(); settingsSection.set("providers"); settingsPage.set("providers"); }, true);
on("app.sign_out", () => { if (centre() !== "settings") openSettings(); settingsSection.set("providers"); settingsPage.set("providers"); }, true);
function tab(t: "sessions" | "files" | "projects") { if (t === "files") void loadFiles(); leftTab.set(t); savePrefs(); if (isNarrow()) drawer.set("left"); else leftOpen.set(true); }

export function newSession() {
  model.select(null); centre.set("new_session");
  const project = currentProject();
  if (project) { loadComposerData(project); loadProjectWorktrees(project); }
  if (isNarrow()) drawer.set(null);
  bump();
}

/** Header tabs contain only root sessions; the viewed child keeps its root represented. */
export function tabIds(): string[] {
  m();
  const ids = openedTabs().filter((id) => {
    const s = model.sessions.get(id);
    return s && !s.parent_id;
  });
  const root = rootOf(model.selected?.sessionId);
  return root && !ids.includes(root) ? [...ids, root] : ids;
}

export function closeTab(id: string) {
  const ids = tabIds();
  const at = ids.indexOf(id);
  if (at < 0) return;
  openedTabs.set(ids.filter((tab) => tab !== id));
  if (rootOf(model.selected?.sessionId) !== id) return;
  const next = ids[at + 1] ?? ids[at - 1];
  if (next) void selectSession(next);
  else {
    model.select(null);
    centre.set("new_session");
    bump();
  }
}

export function stepTab(step: number) {
  const ids = tabIds();
  const at = ids.indexOf(rootOf(model.selected?.sessionId) ?? "");
  if (at < 0 || ids.length < 2) return;
  const next = ids[(at + step + ids.length) % ids.length];
  if (next !== ids[at]) void selectSession(next);
}

export async function sendPrompt(text: string, images: import("./images").DraftImage[] = [], mode: "send" | "steer" | "queue" = "send"): Promise<boolean> {
  text = text.trim();
  if (!text && !images.length) return false;
  const source = draftKey();
  let createdId: string | null = null;
  try {
    let ses = viewed();
    if (centre() === "new_session" || !ses) {
      const project = currentProject();
      if (!project) throw new ApiError(400, "Add or choose a project first.");
      const selected = sessionDraft();
      const fields: Record<string, unknown> = {};
      if (selected.agent) fields.agent = selected.agent;
      if (selected.model) fields.model = selected.model;
      if (selected.effort) fields.effort = selected.effort;
      if (selected.disabled_tools !== undefined) fields.disabled_tools = selected.disabled_tools;
      const created = await request<any>("POST", "/api/session", { body: { project, ...fields }, key: newKey() });
      createdId = created.id;
      // The POST reply is authoritative: keep the tab and its title from the start, instead of
      // falling back to "New session" until the session's event or detail read arrives.
      const previous = model.sessions.get(created.id);
      if (!previous || (created.revision ?? 0) >= previous.revision) model.sessions.set(created.id, normalizeSession(created));
      sessionDraft.set({});
      await selectSession(created.id);
      ses = created;
    }
    await request("POST", `/api/session/${ses!.id}/prompt`, { body: { text, images, mode }, key: newKey() });
    return true;
  } catch (e) {
    if (createdId) {
      const target = createdId;
      imageDrafts.update((d) => ({ ...d, [source]: (d[source] ?? []).filter((i) => !images.includes(i)), [target]: [...images, ...(d[target] ?? [])] }));
      draft.update((d) => ({ ...d, [source]: (d[source] ?? "").trim() === text ? "" : d[source] ?? "", [target]: [text, d[target] ?? ""].filter(Boolean).join("\n") }));
    }
    toast("error", e instanceof ApiError ? e.message : String(e));
    return false;
  }
}

/** A slash command in the composer runs like its palette entry, not as a prompt. */
export function runSlashCommand(text: string): boolean {
  if (text.includes("\n")) return false;
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return false;
  const [name, ...tail] = trimmed.slice(1).split(/\s+/);
  const command = commands.find((c) => c.slash === name);
  if (!command) return false;
  const arg = tail.join(" ").trim();
  if (command.id === "session.rename" && arg) void patchSession(arg);
  else if (command.id === "skills.learn") learn(arg);
  else if (command.id === "session.btw") { if (arg) void sideQuestion(arg); else toast("info", "Usage: /btw your question"); }
  else void run(command.id);
  return true;
}

/** `/learn <source>`: preview first, then install direct hits or show the found-skills checklist. */
function learn(source: string) {
  if (!source) { toast("info", "Usage: /learn owner/repo, a GitHub URL, ./folder, or npx skills add …"); return; }
  const project = currentProject();
  if (!project) { toast("warning", "Open a project first."); return; }
  if (!live()) { toast("warning", "Core is not connected: the view is read-only."); return; }
  const install = (scope: "project" | "global") => async () => {
    picker.set(null);
    toast("info", `Fetching ${source}…`);
    try {
      const preview = await request<any>("POST", "/api/skill/preview", { body: { source, scope, project } });
      const found = (preview.found ?? []) as { name: string; description?: string; installed?: boolean; warnings?: string[] }[];
      const requested = (preview.requested ?? []) as string[];
      if (shouldInstallRequestedDirectly(requested, found)) {
        await installSkills({ fetch_id: preview.fetch_id, source, scope, project, requested, found }, requested);
        return;
      }
      learnChecklist.set({
        fetch_id: preview.fetch_id, source, scope, project, requested,
        problems: preview.problems ?? [], found,
      });
    } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
  };
  picker.set({ title: "Install skills for", items: [
    { id: "project", label: "This project", hint: ".agents/skills", run: install("project") },
    { id: "global", label: "Every project", hint: "~/.agents/skills", run: install("global") },
  ] });
}

type InstallPlan = {
  fetch_id: string; source: string; scope: "project" | "global"; project: string;
  requested: string[]; found: { name: string; installed?: boolean }[];
};

async function installSkills(plan: InstallPlan, names: string[]) {
  if (!names.length) { toast("info", "Choose at least one skill"); return; }
  const counts = selectedSkillCounts(plan.found, names);
  beginOwnSkillInstall();
  try {
    const result = await request<{ installed: { name: string; replaced?: boolean }[] }>("POST", "/api/skill/install", {
      body: {
        fetch_id: plan.fetch_id, skills: names, scope: plan.scope, project: plan.project,
        overwrite: counts.replacedCount > 0,
      },
      key: newKey(),
    });
    finishOwnSkillInstall();
    const installed = result.installed ?? [];
    const namesLearned = installed.map((skill) => skill.name);
    const where = plan.scope === "global" ? "for every project" : "for this project";
    const replaced = installed.filter((skill) => !!skill.replaced).map((skill) => skill.name);
    const replacement = replaced.length ? ` (replaced ${replaced.join(", ")})` : "";
    learnChecklist.set(null);
    toast("success", `Learned ${namesLearned.join(", ")} ${where}. Available in new sessions.${replacement}`);
    const project = currentProject();
    if (project) loadStartingInventory(project, true);
  } catch (e) {
    failOwnSkillInstall();
    toast("error", e instanceof ApiError ? e.message : String(e));
    learnChecklist.update((d) => d && { ...d, busy: false });
  }
}

/** Called by the checklist modal after its selection changes. */
export function installLearnChecklist(selected: string[]) {
  const plan = learnChecklist();
  if (!plan) return;
  if (!selected.length) { toast("info", "Choose at least one skill"); return; }
  learnChecklist.update((d) => d && { ...d, busy: true });
  void installSkills(plan, selected);
}
export function closeLearnChecklist() { if (!learnChecklist()?.busy) learnChecklist.set(null); }

export function toggleDraftInventory(kind: "skill" | "mcp", name: string, locked = false) {
  if (locked) { toast("info", "Available in a new session."); return; }
  const key = disabledKey(kind, name);
  sessionDraft.update((draft) => ({
    ...draft,
    disabled_tools: setDisabledTool(draft.disabled_tools ?? [], key, !(draft.disabled_tools ?? []).includes(key)),
  }));
}

/** Switch one skill or MCP server in a running session using its optimistic revision. */
export async function toggleSessionInventory(kind: "skill" | "mcp", name: string, locked = false) {
  if (locked) { toast("info", "Available in a new session."); return; }
  const session = viewed();
  if (!session) return;
  const key = disabledKey(kind, name);
  const current = Array.isArray(session.disabled_tools) ? session.disabled_tools : [];
  const disabled = toggleDisabledTool(current, kind, name);
  try {
    const updated = await request<any>("PATCH", `/api/session/${session.id}`, {
      body: { disabled_tools: disabled }, ifMatch: session.revision, key: newKey(),
    });
    const latest = model.sessions.get(session.id);
    if (latest) {
      const response = updated && typeof updated === "object" ? updated : {};
      model.sessions.set(session.id, {
        ...latest,
        ...response,
        disabled_tools: disabled,
        skills: latest.skills?.map((skill) => skill.name === name && kind === "skill" ? { ...skill, state: disabled.includes(key) ? "off" : "on" } : skill),
        mcp: latest.mcp?.map((server) => server.server === name && kind === "mcp" && server.state !== "unavailable" ? { ...server, state: disabled.includes(key) ? "off" : "on" } : server),
      });
      bump();
    }
  } catch (e) {
    if (e instanceof ApiError && e.isConflict) toast("warning", "Someone else changed this session; reloaded. Try again.");
    else toast("error", e instanceof ApiError ? e.message : String(e));
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
export function askRemoveProject(id?: string) {
  const row = focusedSidebarRow();
  const target = id ?? (leftTab() === "projects" && row?.kind === "project" ? row.id : currentProject());
  const project = target ? model.projects.get(target) : undefined;
  if (!target || !project) return;
  let sessions = 0;
  for (const session of model.sessions.values()) if (session.project_id === target) sessions++;
  confirmDialog.set({
    title: "Remove project",
    body: `Remove “${project.name}”${sessions ? ` and its ${sessions} session${sessions === 1 ? "" : "s"}` : ""}?\nThe directory and its files stay. This cannot be undone.`,
    confirm: "Remove", destructive: true,
    run: async () => {
      if (!live()) { toast("warning", "Core is not connected: the view is read-only."); return; }
      await request("DELETE", `/api/project/${target}`, { key: newKey() });
      toast("success", "Project removed");
    },
  });
}

function openFocusedSidebarMenu() {
  const row = focusedSidebarRow();
  if (!row) return;
  sidebarRowMenu.set(row);
  if (row.kind === "session") void selectSession(row.id);
  else selectProject(row.id);
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

export async function handoff() {
  const s = viewed(); if (!s) return;
  toast("info", "Writing the handoff…");
  try {
    const r = await request<{ to: string }>("POST", `/api/session/${s.id}/handoff`, { body: {}, key: newKey() });
    await selectSession(r.to);
  } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}
/** `/btw question`: answered from the context so far in a modal; nothing is added to the session. */
export async function sideQuestion(question: string) {
  const s = viewed(); if (!s) return;
  toast("info", "Asking…");
  try {
    const r = await request<{ answer: string }>("POST", `/api/session/${s.id}/btw`, { body: { question } });
    contextDetail.set({ title: `Side question · ${question.slice(0, 40)}`, detail: `${r.answer}\n\nNot added to the session's context.` });
  } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
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
on("alert.dismiss", () => { const t = toasts(); if (t.length) dismissToast(t[0].id); }); // the oldest, drawn on top
on("alert.cancel", () => { const d = confirmDialog(); if (d) { if (!d.busy) confirmDialog.set(null); } else if (connection().kind !== "incompatible") addProjectOpen.set(false); });
on("overlay.close", () => { if (usageDialog()) { usageDialog.set(null); return; } if (alertDetail()) { alertDetail.set(null); return; } if (learnChecklist()) { closeLearnChecklist(); return; } if (picker()) { picker.set(null); return; } paletteOpen.set(false); });
on("nav.drawer.close", () => drawer.set(null));
on("sidebar.left.toggle", toggleLeft);
on("sidebar.right.toggle", () => toggleRight(rightWidthHint.w));
on("sidebar.tab.sessions", () => tab("sessions"));
on("sidebar.tab.files", () => tab("files"));
on("sidebar.tab.projects", () => tab("projects"));
on("sidebar.focus", () => { if (isNarrow()) drawer.set("left"); else leftOpen.set(true); });
on("composer.focus", () => focusComposer.fn());
on("session.new", newSession);
on("session.tab_close", () => { const id = rootOf(viewed()?.id); if (id) closeTab(id); });
on("session.tab_next", () => stepTab(1));
on("session.tab_prev", () => stepTab(-1));
on("nav.back", () => { const parent = viewed()?.parent_id; if (parent) void selectSession(parent); });
on("timeline.detail_cycle", () => {
  const levels = ["low", "medium", "high"] as const;
  setTimelineDetail(levels[(levels.indexOf(timelineDetail()) + 1) % levels.length]);
});
on("session.switch", () => { tab("sessions"); });
on("editor.edit", () => editFile());
on("editor.save", () => saveFile(), true);
on("editor.reload", () => {
  if (!fileDirty()) { void reloadFile(); return; }
  confirmDialog.set({ title: "Reload file", body: "Reading the file again replaces your unsaved changes.", confirm: "Reload", destructive: true, run: async () => { confirmDialog.set(null); await reloadFile(); } });
});
/** Esc / Close editor: a dirty editor asks first; a clean one leaves editing, then the file. */
function closeEditor() {
  const f = openFile();
  const leave = () => { if (f?.draft !== undefined) openFile.set({ ...f, draft: undefined, stale: false }); else centre.set(viewed() ? "session" : "new_session"); };
  if (!fileDirty()) { leave(); return; }
  confirmDialog.set({ title: "Discard changes", body: "The file has unsaved changes. Leaving the editor drops them.", confirm: "Discard", destructive: true, run: () => { confirmDialog.set(null); leave(); } });
}
on("editor.close", closeEditor);
on("app.usage", () => void showUsage());
on("turn.load_older", () => { void loadOlderMessages(true); });
on("project.switch", () => tab("projects"));
on("project.add", () => addProjectOpen.set(true), true);
on("project.remove", () => askRemoveProject());
on("sidebar.row_menu", openFocusedSidebarMenu);
on("session.rename", () => renaming.set(true), true);
on("skills.learn", () => toast("info", "Type /learn followed by owner/repo, a GitHub URL or ./folder"), true);
on("session.archive", () => archive(), true);
on("session.unarchive", () => archive(undefined, true), true);
on("session.delete", () => askDelete(), true);
on("session.interrupt", interrupt, true);
on("session.continue", continueSession, true);
on("session.handoff", handoff, true);
export function openLinked(which: "continued_from" | "continued_to") {
  const id = viewed()?.[which];
  if (!id) return;
  if (!model.sessions.has(id)) { toast("warning", "That session is no longer in the list (deleted or archived)"); return; }
  void selectSession(id);
}
on("session.open_origin", () => openLinked("continued_from"));
on("session.open_continuation", () => openLinked("continued_to"));
on("session.btw", () => toast("info", "Usage: /btw your question"), true);
on("session.speak", async () => {
  const session = viewed();
  if (!session) return;
  try { await speakAnswer(session.id); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}, true);
on("session.speak_stop", async () => {
  try { await stopSpeaking(); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
});
function dictateFromComposer() {
  const phase = dictationPhase();
  if (phase === "recording") { void stopDictation(); return; }
  if (phase !== "idle") return;
  if (!live()) { toast("warning", "Core is not connected: dictation is unavailable."); return; }
  if (viewed()?.archived) { toast("warning", "Unarchive this session before dictating."); return; }
  const target = draftKey();
  startDictation((transcript) => {
    draft.update((all) => ({ ...all, [target]: appendTranscript(all[target] ?? "", transcript) }));
  });
}
// The manifest currently has only the deferred `composer.dictate` command. Keep the
// requested runtime handler registered by its final ID so the mic button can invoke it
// now and the shared manifest can expose it when that command lands.
on("voice.dictate", dictateFromComposer, true);
on("voice.download_stt", () => askVoiceDownload("stt"), true);
on("voice.remove_stt", () => askVoiceRemove("stt"), true);
on("voice.download_tts", () => askVoiceDownload("tts"), true);
on("voice.remove_tts", () => askVoiceRemove("tts"), true);
on("session.copy_id", () => { const s = viewed(); if (s) void Clipboard.writeText(s.id).then(() => toast("success", "Copied session id")); });
on("session.open_inspector", () => {
  const s = viewed();
  if (!s) return;
  const url = new URL("/inspector", import.meta.url);
  url.hash = s.id;
  void openUrl(url.href).catch(() => toast("info", "Open /inspector on the host page."));
});
on("tool.show_all_output", () => {
  const tool = focusedTool();
  if (!tool?.clipped) return;
  toolOutputAll.update((all) => ({ ...all, [tool.key]: !(all[tool.key] ?? false) }));
});
on("tool.raw_toggle", () => focusedTool()?.toggleRaw());
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

/**
 * K9: switching the model or the agent can make the next request start cold. Ask Core first and
 * confirm with the user only when something cached would be lost; a failed check does not block.
 */
async function guardSwitch(s: { id: string }, what: "model" | "agent", value: string, go: () => Promise<void>) {
  let cost: { reasons?: string[]; cached_tokens?: number; cache_lost?: boolean } | null = null;
  try { cost = await request("GET", `/api/session/${s.id}/switch-cost`, { query: { [what]: value } }); } catch { /* advice only */ }
  if (!cost?.cache_lost) { await go(); return; }
  confirmDialog.set({
    title: what === "model" ? "Switch model" : "Switch agent",
    body: `${(cost.reasons ?? []).join("; ")}. The next request starts cold: about ${cost.cached_tokens ?? 0} cached tokens are read again at full price.`,
    confirm: "Switch", destructive: false,
    run: async () => { confirmDialog.set(null); await go(); },
  });
}

function applyModel(model: string) {
  return async () => {
    const s = viewed();
    if (!s) {
      sessionDraft.update((d) => ({ ...d, model }));
      toast("info", `The new session will use model ${model || "default"}`);
      return;
    }
    await guardSwitch(s, "model", model, async () => {
      try {
        await request("POST", `/api/session/${s.id}/model`, { body: { model }, ifMatch: s.revision, key: newKey() });
        toast("success", "Model changed from the next call");
      } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
    });
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
  const res = await request<any>("GET", "/api/agent", { query: { project: viewed()?.project_id ?? currentProject() ?? undefined } });
  const mode = viewed()?.parent_id ? "subagent" : "primary";
  return ((res.agents ?? []) as any[]).filter((a) => a.mode === "all" || a.mode === mode).map((a) => ({
    id: a.name,
    label: a.name,
    hint: [a.source, a.mode].filter(Boolean).join(" · "),
    run: choose(a.name, async () => {
      const s = viewed();
      if (!s) {
        sessionDraft.update((d) => ({ ...d, agent: a.name }));
        toast("info", `The new session will use agent ${a.name}`);
        return;
      }
      await guardSwitch(s, "agent", a.name, async () => {
        try {
          await request("POST", `/api/session/${s.id}/agent`, { body: { agent: a.name }, ifMatch: s.revision, key: newKey() });
          toast("success", "Agent changed from the next call");
        } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
      });
    }),
  }));
}));
on("model.effort", () => void pickOne("Reasoning effort", async () => {
  const s = viewed();
  const cat = await request<any>("GET", "/api/model");
  const chosenAgent = s?.agent ?? sessionDraft().agent ?? "build";
  const agentModel = agentCatalog().agents.find((a) => a.name === chosenAgent)?.model;
  const selectedModel = s ? s.model : sessionDraft().model || agentModel || rememberedModel(currentProject(), chosenAgent);
  const cur = (cat.models ?? []).find((m: any) => m.ref === selectedModel);
  const efforts: string[] = cur?.efforts?.length ? cur.efforts : ["low", "medium", "high"];
  const apply = async (effort: string) => {
    const current = viewed();
    if (!current) {
      sessionDraft.update((d) => ({ ...d, effort }));
      toast("info", `The new session will use effort ${effort || "default"}`);
      return;
    }
    try {
      await request("PATCH", `/api/session/${current.id}`, { body: { effort }, ifMatch: current.revision, key: newKey() });
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
  "session.change_directory": "NSH's directory picker is not built yet; this action is unavailable.",
  "session.worktrees": "NSH's worktree controls are not built yet; this action is unavailable.",
};

export interface Availability { ok: boolean; reason?: string }
export function availability(c: Cmd): Availability {
  if (c.status === "blocked_core") return { ok: false, reason: BLOCKED[c.id] ?? c.unavailable_reason ?? "Not available yet." };
  if (c.status === "deferred") return { ok: false, reason: `Deferred: ${c.unavailable_reason ?? "not built yet"}` };
  const voiceReason = voiceCommandReason(c.id);
  if (voiceReason) return { ok: false, reason: voiceReason };
  const h = handlers.get(c.id);
  // Playback is a local NSH state, not a shared manifest term: TUI consumes the
  // same manifest but owns its independent rodio speaking state.
  const whenOk = c.id === "session.speak_stop"
    ? speechPlaying()
    : c.when.every(term) && (c.id !== "session.speak" || !speechPlaying());
  if (!whenOk) return { ok: false, reason: c.unavailable_reason ?? "Not available right now." };
  if (!h) return { ok: false, reason: BLOCKED[c.id] ?? "Not built in this NSH increment yet." };
  if (h.mutates && !live()) return { ok: false, reason: "Core is not connected: the view is read-only." };
  return { ok: true };
}

export async function run(id: string) {
  if (id === "voice.dictate") { await handlers.get(id)?.run(); return; }
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
  if (isDictationBusy()) { cancelDictation(); return "dictation"; }
  if (contextDetail()) { contextDetail.set(null); return "context_detail"; }
  if (centre() === "settings" && settingsPage()) { settingsPage.set(settingsPage()!.includes("/") ? settingsPage()!.split("/")[0] : null); return "settings_page"; }
  for (const rule of manifest.keymap.escape as readonly { id: string; when: readonly string[]; action: string }[]) {
    if (rule.when.every(term)) {
      if (rule.id === "text_edit") { renaming.set(false); return rule.id; }
      if (rule.id === "alert") { void run("alert.dismiss"); return rule.id; }
      if (rule.id === "interrupt") { void run("session.interrupt"); return rule.id; }
      if (rule.id === "back") { const s = viewed(); if (s?.parent_id) void selectSession(s.parent_id); return rule.id; }
      if (rule.id === "editor") { closeEditor(); return rule.id; }
      void run(rule.action);
      return rule.id;
    }
  }
  return null;
}

/** Keys for an open control (a completion list): bound while it shows, released by the returned function. */
export function bindOpenKeys(keys: Record<string, () => void>): () => void {
  const offs: (() => void)[] = [];
  for (const [k, fn] of Object.entries(keys)) {
    try { offs.push(keymap.bind(k, fn, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  }
  return () => offs.forEach((f) => f());
}

export function bindKeys(): () => void {
  const offs: (() => void)[] = [];
  for (const c of commands) {
    const k = c.keys?.nsh;
    if (!k || k === "esc" || !GLOBAL_CONTEXTS.has(c.key_context ?? "") || !hasModifier(k)) continue;
    if (c.id === "app.quit") continue; // platform-owned
    try { offs.push(keymap.bind(toKeystroke(k), () => { void run(c.id); }, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  }
  for (const id of ["sidebar.row_menu", "tool.raw_toggle", "tool.show_all_output"]) {
    const c = byId.get(id);
    const k = c?.keys?.nsh;
    if (!k) continue;
    try {
      offs.push(keymap.bind(toKeystroke(k), () => {
        if (id === "sidebar.row_menu" && focusedSidebarRow()) void run(id);
        if (id === "tool.raw_toggle" && focusedTool()) void run(id);
        if (id === "tool.show_all_output" && focusedTool()?.clipped) void run(id);
      }, { when: "notEditing" }));
    } catch { /* the platform keeps this keystroke */ }
  }
  // The editor's own keys act only while a file is open (cmd+s saves; cmd+e starts editing).
  for (const id of ["editor.save", "editor.edit"]) {
    const k = byId.get(id)?.keys?.nsh;
    if (!k) continue;
    try { offs.push(keymap.bind(toKeystroke(k), () => { if (centre() === "file") void run(id); }, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  }
  try { offs.push(keymap.bind("escape", () => { escape(); }, { when: "always" })); } catch {}
  return () => offs.forEach((f) => f());
}
void selectProject;
