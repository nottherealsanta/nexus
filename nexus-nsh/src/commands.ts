import { loadFiles, loadOlderMessages, rememberNewSession } from "./store";
import type { UsageProvider } from "./store";
// Command registry built from interface/manifest.toml (generated to src/gen/manifest.ts).
// Ids, titles, keys, `when` rules, reasons and placements come from the manifest; this file
// only evaluates terms and binds handlers for the actions whose Core API exists.
import { Clipboard, effect, keymap, openUrl, platform } from "nsh";
import { manifest } from "./gen/manifest";
import {
  editFile, fileDirty, fileFindOpen, fileSource, markdownFile, openFile, reloadFile, saveFile,
  firstPrompts, pendingInputs, contextDetail, imagePreview, annotating, addProjectOpen, alertDetail, usageDialog, archivedFilter, banners, bump, centre, confirmDialog, connection, currentProject, dismissToast, drawer,
  agentCatalog, beginOwnSkillInstall, currentWorktree, draft, rememberedModel, failOwnSkillInstall, finishOwnSkillInstall, isNarrow, leftOpen, leftTab, learnChecklist, live, loadComposerData, loadInputs, loadProjectWorktrees, loadStartingInventory, m, model, notifications, openedTabs, paletteOpen, previousCentre, renaming, rightOpen,
  composerFocused, flowProject, flowSession, flowStage, projectsOverview, focusedSidebarRow, focusedTool, railDrag, picker, rootOf, savePrefs, scopeAll, scrolledUp, selectProject, selectSession, sessionDraft, setTimelineDetail, settingsNavFocused, settingsPage, settingsPageOpen, settingsQuery, settingsSection, providerDetail, sidebarRowMenu, switcherOpen, sync, timelineDetail, toast, toasts, toolOutputAll, unread, viewed, waitIndex, resourcesOpen, loadResources,
} from "./store";
import { ApiError, apiBase, newKey, request } from "./core/transport";
import { MESSAGE_PAGE } from "./core/sync";
import { Message } from "./core/api";
import { normalizeSession } from "./core/reduce";
import { imageDrafts } from "./images";
import { pasteDrafts } from "./pastes";
import type { InventoryKind } from "./skills-mcp";
import { disabledKey, selectedSkillCounts, setDisabledTool, shouldInstallRequestedDirectly, toggleDisabledTool } from "./skills-mcp";
import { speakAnswer, speechPlaying, stopSpeaking } from "./speech";
import { cancelDictation, isDictationBusy, microphoneAvailable, startDictation, stopDictation } from "./dictation";
import { appendTranscript } from "./dictation-logic";
import { dictationPhase } from "./voice-state";
import { toggleManagerDropdown, toggleManagerAudio, cancelListening, cancelTurn, closeField, dismissCard, managerEscapeRule, managerSlash, managerTerm, minimise, openField, openManagerContext, openManagerSession, sendListening, scrollDetails, startQuietly, stopCall, stopSpeech, submitField, talk } from "./core/manager";
import { refreshSearchService, searchStartReason, startSearchService } from "./search-service";
import { closeInspector, inspectSession, inspector, inspectorCommand } from "./inspector";
import { rowSessions } from "./ui/sidebar";
import { PROJECT_STATUS, dropMove, orderedProjects, projectStatus, railProjects } from "./project-rail";
import type { ProjectStatus } from "./project-rail";
import { askVoiceDownload, askVoiceRemove, refreshVoiceOnSettingsOpen, voiceCommandReason } from "./voice-settings";
import { beginRenameTerminal, closeTerminalPanel, controlTerminal, focusTerminalList, newTerminal, openTerminal, requestTerminalClose, requestTerminalCopy, terminalAvailable, terminalControlled, terminalFocused, terminalOpen, terminalRename, terminalSelected } from "./terminal-panel";

export interface Cmd {
  id: string; title: string; category: string; slash?: string; keys?: { tui?: string; nsh?: string };
  /** The TUI's Ctrl+X leader keys (`shift+n`, `shift+/`), bound as a short sequence by NSH. */
  leader?: string[];
  key_context?: string; when: string[]; unavailable_reason?: string; placement: string[]; status?: string; clients?: string; note?: string;
}
export const commands: Cmd[] = (manifest.command as unknown as Cmd[]).filter((c) => (c.clients ?? "both") !== "tui" && !(!terminalAvailable && c.id.startsWith("terminal.")));
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
    case "project.current": case "project.passive": case "project.inactive": {
      const p = targetProject() ? model.projects.get(targetProject()!) : undefined;
      return !!p && projectStatus(p) === t.slice("project.".length);
    }
    case "sidebar.projects_focus": return flowStage() === "projects" && !projectsOverview();
    case "project.can_move_up": return railIndex() > 0;
    case "project.can_move_down": { const i = railIndex(); return i >= 0 && i < railProjects(model.projects.values()).length - 1; }
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
    case "terminal.open": return terminalOpen();
    case "terminal.selected": return !!terminalSelected();
    case "terminal.focused": return terminalFocused();
    case "terminal.controlled": return terminalControlled();
    case "panel.file": return openFile() !== null;
    case "panel.diff": return false;
    case "editor.dirty": return fileDirty();
    case "editor.readonly": return openFile() !== null && openFile()?.draft === undefined && openFile()?.content !== undefined;
    case "editor.markdown": return openFile() !== null && markdownFile();
    case "overlay.open": return !!inspector() || !!projectsOverview() || paletteOpen() || switcherOpen() || !!picker() || !!learnChecklist() || !!alertDetail() || !!usageDialog() || !!imagePreview() || !!annotating();
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
    default: if (t.startsWith("manager.")) return managerTerm(t);
      return false; // other focus.*, editor.dirty: no per-item focus tracking in this increment
  }
}
let signedIn = false;
export const providersSignedIn = () => signedIn;
export async function loadProviders() { try { const p = await request<any[]>("GET", "/api/provider"); signedIn = p.some((x) => x.connected); bump(); return p; } catch { return []; } }

// --- handlers -----------------------------------------------------------------------------
/** Handlers for commands whose Core API exists (or that are pure client navigation). `mutates` = needs a live connection. */
interface Handler { run: () => void | Promise<void>; mutates?: boolean }
export const handlers = new Map<string, Handler>();
/** `app.usage`: every connected provider's usage (K11), in the provider-usage dialog; again while open refreshes. */
async function showUsage() {
  const refresh = !!usageDialog();
  const previous = usageDialog();
  const pending = { providers: previous?.providers ?? [], fetched: previous?.fetched, loading: true };
  usageDialog.set(pending);
  try {
    const u = await request<{ providers: UsageProvider[]; fetched?: number | null }>(refresh ? "POST" : "GET", "/api/usage");
    if (usageDialog() === pending) usageDialog.set({ providers: u.providers ?? [], loading: false, missing: u.fetched === null, fetched: u.fetched });
  } catch (e) { if (usageDialog() === pending) { usageDialog.set(previous); toast("error", `Provider usage failed to load: ${e instanceof ApiError ? e.message : String(e)}`); } }
}

const on = (id: string, run: Handler["run"], mutates = false) => handlers.set(id, { run, mutates });
on("terminal.open", openTerminal);
on("terminal.new", newTerminal);
on("terminal.rename", beginRenameTerminal);
on("terminal.close", requestTerminalClose);
on("terminal.copy", requestTerminalCopy);
on("terminal.focus_list", focusTerminalList);
on("terminal.acquire", () => controlTerminal("acquire"));
on("terminal.release", () => controlTerminal("release"));

/** Copies text to the system clipboard and confirms it (the timeline's message copy). */
export function copyText(text: string, done: string) { void Clipboard.writeText(text).then(() => toast("success", done)); }
export function toggleLeft() { if (isNarrow()) drawer.set(drawer() === "left" ? null : "left"); else { leftOpen.update((v) => !v); savePrefs(); } }
/** The right panel opens on demand from the icon rail. */
export function rightIsOpen(_w: number) { return rightOpen() ?? false; }
export function toggleRight(w: number) {
  if (isNarrow()) drawer.set(drawer() === "right" ? null : "right");
  else { rightOpen.set(!rightIsOpen(w)); savePrefs(); }
}
export function openSettings() {
  if (centre() === "settings") { centre.set(previousCentre()); return; }
  previousCentre.set(centre()); centre.set("settings"); settingsQuery.set(""); providerDetail.set(null); settingsNavFocused.set(true); settingsPageOpen.set(false); if (isNarrow()) drawer.set(null);
  refreshVoiceOnSettingsOpen();
  void refreshSearchService();
}
on("app.sign_in", () => { if (centre() !== "settings") openSettings(); settingsQuery.set(""); settingsSection.set("providers"); settingsPage.set(null); settingsPageOpen.set(true); settingsNavFocused.set(false); providerDetail.set(null); }, true);
on("app.sign_out", () => { if (centre() !== "settings") openSettings(); settingsQuery.set(""); settingsSection.set("providers"); settingsPage.set(null); settingsPageOpen.set(true); settingsNavFocused.set(false); providerDetail.set(null); }, true);
function tab(t: "sessions" | "files" | "projects") {
  if (t === "files") void loadFiles(); leftTab.set(t);
  // As in the TUI, the sidebar tab holds the keys: Esc and Right step back, Left goes further in.
  flowStage.set(t === "files" ? null : t);
  if (t === "projects") flowProject.set(currentProject() ?? null); savePrefs(); if (isNarrow()) drawer.set("left"); else leftOpen.set(true);
}

export function newSession() {
  rememberNewSession(currentProject());
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
    return s && !s.parent_id && s.project_id === currentProject();
  });
  const root = rootOf(model.selected?.sessionId);
  return root && model.sessions.get(root)?.project_id === currentProject() && !ids.includes(root) ? [...ids, root] : ids;
}

export function closeTab(id: string) {
  const ids = tabIds();
  const at = ids.indexOf(id);
  if (at < 0) return;
  openedTabs.update((tabs) => tabs.filter((tab) => tab !== id));
  if (rootOf(model.selected?.sessionId) !== id) return;
  const next = ids[at + 1] ?? ids[at - 1];
  if (next) void selectSession(next);
  else {
    rememberNewSession(currentProject());
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

export async function sendPrompt(rawText: string, images: import("./images").DraftImage[] = [], mode: "send" | "steer" | "queue" = "send"): Promise<boolean> {
  const source = draftKey();
  const raw = rawText.trim();
  // Core stores each paste as a collapsed text part: the model gets the full text,
  // the timeline shows the marker.
  const pastes = (pasteDrafts()[source] ?? []).filter((p) => raw.includes(p.marker));
  if (!raw && !images.length) return false;
  let createdId: string | null = null;
  let startingProject: string | null = null;
  try {
    let ses = viewed();
    if (centre() === "new_session" || !ses) {
      const project = currentProject();
      if (!project) throw new ApiError(400, "Add or choose a project first.");
      if (firstPrompts()[project] && !firstPrompts()[project].session) return false;
      startingProject = project;
      const selected = sessionDraft();
      const fields: Record<string, unknown> = {};
      if (selected.agent) fields.agent = selected.agent;
      if (selected.model) fields.model = selected.model;
      if (selected.effort !== undefined) fields.effort = selected.effort;
      if (selected.disabled_tools !== undefined) fields.disabled_tools = selected.disabled_tools;
      firstPrompts.update((all) => ({ ...all, [project]: { text: raw, images: images.length, choices: selected } }));
      draft.update((d) => ({ ...d, [source]: "" }));
      imageDrafts.update((d) => ({ ...d, [source]: (d[source] ?? []).filter((i) => !images.includes(i)) }));
      pasteDrafts.update((d) => ({ ...d, [source]: (d[source] ?? []).filter((p) => !pastes.includes(p)) }));
      const created = await request<any>("POST", "/api/session", { body: { project, ...fields }, key: newKey() });
      createdId = created.id;
      // The POST reply is authoritative: keep the tab and its title from the start, instead of
      // falling back to "New session" until the session's event or detail read arrives.
      const previous = model.sessions.get(created.id);
      if (!previous || (created.revision ?? 0) >= previous.revision) model.sessions.set(created.id, normalizeSession(created));
      firstPrompts.update((all) => ({ ...all, [project]: { ...all[project], session: created.id } }));
      void selectSession(created.id);
      ses = created;
    }
    await request("POST", `/api/session/${ses!.id}/prompt`, {
      body: { text: raw, images, pastes: pastes.map(({ marker, text }) => ({ marker, text })), mode },
      key: newKey(),
    });
    return true;
  } catch (e) {
    if (startingProject) {
      firstPrompts.update((all) => { const next = { ...all }; delete next[startingProject!]; return next; });
      if (!createdId) {
        draft.update((d) => ({ ...d, [source]: [raw, d[source] ?? ""].filter(Boolean).join("\n") }));
        imageDrafts.update((d) => ({ ...d, [source]: [...images, ...(d[source] ?? [])] }));
        pasteDrafts.update((d) => ({ ...d, [source]: [...pastes, ...(d[source] ?? [])] }));
      }
    }
    if (createdId) {
      const target = createdId;
      imageDrafts.update((d) => ({ ...d, [source]: (d[source] ?? []).filter((i) => !images.includes(i)), [target]: [...images, ...(d[target] ?? [])] }));
      if (pastes.length) pasteDrafts.update((d) => ({ ...d, [target]: [...pastes, ...(d[target] ?? [])] }));
      draft.update((d) => ({ ...d, [source]: (d[source] ?? "").trim() === raw ? "" : d[source] ?? "", [target]: [raw, d[target] ?? ""].filter(Boolean).join("\n") }));
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
  // `/manager stop` is the registered `/manager-stop`: a slash name has no space.
  const stop = name === "manager" && tail[0]?.toLowerCase() === "stop" && tail.length === 1;
  const command = commands.find((c) => c.slash === (stop ? "manager-stop" : name));
  if (!command) return false;
  const arg = tail.join(" ").trim();
  if (command.id === "session.rename" && arg) void patchSession(arg);
  else if (command.id === "skills.learn") learn(arg);
  else if (command.id === "manager.start") void managerSlash(arg);
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

export function toggleDraftInventory(kind: InventoryKind, name: string, locked = false) {
  if (locked) { toast("info", "Available in a new session."); return; }
  const key = disabledKey(kind, name);
  sessionDraft.update((draft) => ({
    ...draft,
    disabled_tools: setDisabledTool(draft.disabled_tools ?? [], key, !(draft.disabled_tools ?? []).includes(key)),
  }));
}

/** Switch one skill or MCP server in a running session using its optimistic revision. */
export async function toggleSessionInventory(kind: InventoryKind, name: string, locked = false) {
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
/** The project a project command acts on: the row whose menu is open or focused, else the current project. */
export function targetProject(): string | null {
  const row = focusedSidebarRow();
  return row?.kind === "project" ? row.id : currentProject() ?? null;
}
/** The target project's place in the rail, or -1 (not in the rail, or none). */
function railIndex(): number {
  m();
  const id = targetProject();
  return id ? railProjects(model.projects.values()).findIndex((p) => p.id === id) : -1;
}
/** Move a project in Core's saved order: before/after a neighbour, or to an end when it is null. */
export async function moveProject(id: string, to: { before: string | null } | { after: string | null }) {
  if (!live()) { toast("warning", "Core is not connected: the view is read-only."); return; }
  try { await request("POST", `/api/project/${id}/move`, { body: to, key: newKey() }); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}
function moveProjectStep(delta: number) {
  const ids = railProjects(model.projects.values()).map((p) => p.id);
  const id = targetProject();
  if (!id) return;
  const move = dropMove(ids, id, ids.indexOf(id) + delta);
  if (move) void moveProject(id, move);
}
/** Set a project's status (`PATCH`); the project event updates every client. */
export async function setProjectStatus(id: string, status: ProjectStatus) {
  if (!live()) { toast("warning", "Core is not connected: the view is read-only."); return; }
  try { await request("PATCH", `/api/project/${id}`, { body: { status }, key: newKey() }); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}
function setCurrentProjectStatus(status: ProjectStatus) { const id = targetProject(); if (id) void setProjectStatus(id, status); }
export function askRemoveProject(id?: string) {
  const row = focusedSidebarRow();
  const target = id ?? (row?.kind === "project" ? row.id : currentProject());
  const project = target ? model.projects.get(target) : undefined;
  if (!target || !project) return;
  let sessions = 0;
  for (const session of model.sessions.values()) if (session.project_id === target) sessions++;
  confirmDialog.set({
    title: "Remove project",
    body: `Remove “${project.name}” from Nexus?${sessions ? `\nIts ${sessions} session${sessions === 1 ? " is" : "s are"} kept and return if you add the folder again.` : ""}\nFiles on disk are never touched.`,
    confirm: "Remove", destructive: true,
    run: async () => {
      if (!live()) { toast("warning", "Core is not connected: the view is read-only."); return; }
      try {
        await request("POST", `/api/project/${target}/remove`, { key: newKey() });
        toast("success", "Project removed");
      } catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
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
  if (undo) { resumeArchived(s.id); return; }
  try { await request("POST", `/api/session/${root.id}/${undo ? "unarchive" : "archive"}`, { key: newKey() }); toast("success", undo ? "Session unarchived" : "Session archived"); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); }
}

export function resumeArchived(id: string, go?: () => Promise<void>) {
  confirmDialog.set({
    title: "Resume archived session",
    body: "This moves the session and its children out of the archive. The next request may start with an uncached large context, which can be slower and cost more. Archived code kernels restart with fresh state.",
    confirm: "Resume", destructive: false,
    run: async () => {
      if (!live()) throw new Error("Core is not connected: the view is read-only.");
      let root = model.sessions.get(id);
      if (!root) throw new Error("Session no longer exists.");
      while (root.parent_id && model.sessions.get(root.parent_id)) root = model.sessions.get(root.parent_id)!;
      await request("POST", `/api/session/${root.id}/unarchive`, { key: newKey() });
      if (go && viewed()?.id === id) await go();
    },
  });
}
/** Take pending input out of a session's queue, oldest first; returns its parts. */
async function takePendingInputs(id: string): Promise<any[]> {
  const { inputs } = await request<{ inputs: { id: string }[] }>("GET", `/api/session/${id}/input`);
  const parts: any[] = [];
  for (const item of inputs ?? []) {
    const removed = await request<{ parts?: any[] }>("DELETE", `/api/session/${id}/input/${item.id}`);
    parts.push(...(removed.parts ?? []));
  }
  return parts;
}

/** Put removed pending input back in a draft: its text above the draft on a new
 *  line, its images attached. Parts from several inputs arrive in queue order. */
export function restorePendingParts(key: string, parts: any[]) {
  const text = parts.filter((p) => p.type === "text" && !/^\[Image [A-Z]\]$/.test(p.text ?? "")).map((p) => p.text).join("\n");
  const images = parts.filter((p) => p.type === "image").map((p) => ({ id: `restored-${++restoredImages}`, name: p.name ?? "image.png", data_url: p.data_url }));
  if (text) draft.update((d) => ({ ...d, [key]: [text, d[key] ?? ""].filter(Boolean).join("\n") }));
  if (images.length) imageDrafts.update((d) => ({ ...d, [key]: [...images, ...(d[key] ?? [])] }));
}
let restoredImages = 0;
export async function interrupt() {
  const s = viewed(); if (!s) return;
  const key = draftKey();
  try { await request("POST", `/api/session/${s.id}/interrupt`, { key: newKey() }); }
  catch (e) { toast("error", e instanceof ApiError ? e.message : String(e)); return; }
  // Core holds a queue an interrupt leaves behind; take it back instead, so
  // nothing is stranded and the user can send it again. A failed reclaim leaves
  // the queue visible in the client, as before.
  try {
    const parts = await takePendingInputs(s.id);
    if (parts.length) { restorePendingParts(key, parts); focusComposer.fn?.(); }
    await loadInputs(s.id);
  } catch { /* the next refresh shows whatever is still queued */ }
}
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

export const blurComposer = { fn: (() => {}) as () => void };
export const focusComposer = { fn: (() => {}) as () => void };
export const rightWidthHint = { w: 1280 };

on("palette.open", () => paletteOpen.set(true));
on("app.settings", openSettings);
on("app.help_keys", () => contextDetail.set({ title: "Keyboard shortcuts", detail: shortcutText() }));
on("app.open_inspector", () => inspectSession());
// The inspector's own keys (key_context "inspector") act on the open modal only.
for (const c of commands) if (c.id.startsWith("inspector.")) on(c.id, () => { if (inspector()) inspectorCommand(c.id); });
on("app.reconnect", () => sync.retryNow());
on("app.start_daemon", async () => { try { await fetch(`${apiBase()}/__nexus/start`, { method: "POST" }); } catch {} sync.retryNow(); });
on("notifications.open", () => { drawer.set("notifications"); unread.set(0); });
on("notifications.mark_all_read", () => { notifications.set([]); unread.set(0); });
on("alert.dismiss", () => { const t = toasts(); if (t.length) dismissToast(t[0].id); }); // the oldest, drawn on top
on("alert.cancel", () => { const d = confirmDialog(); if (d) { if (!d.busy) confirmDialog.set(null); } else if (connection().kind !== "incompatible") addProjectOpen.set(false); });
on("overlay.close", () => { if (annotating()) { annotating.set(null); return; } if (imagePreview()) { imagePreview.set(null); return; } if (usageDialog()) { usageDialog.set(null); return; } if (alertDetail()) { alertDetail.set(null); return; } if (learnChecklist()) { closeLearnChecklist(); return; } if (switcherOpen()) { switcherOpen.set(false); return; } if (projectsOverview()) { projectsOverview.set(null); return; } if (picker()) { picker.set(null); return; } paletteOpen.set(false); });
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
/** Open the session switcher over the current view (the rail's second click opens it project-scoped). */
export function openSessionSwitcher() {
  if (isNarrow()) drawer.set(null);
  switcherOpen.set(true);
}
on("session.switch", openSessionSwitcher);
on("editor.edit", () => editFile());
on("editor.find", () => { if (openFile()) { if (markdownFile()) fileSource.set(true); fileFindOpen.set(true); } });
on("editor.preview", () => { if (openFile() && markdownFile()) { fileSource.set(!fileSource()); fileFindOpen.set(false); } });
on("editor.save", () => saveFile(), true);
on("editor.reload", () => {
  if (!fileDirty()) { void reloadFile(); return; }
  confirmDialog.set({ title: "Reload file", body: "Reading the file again replaces your unsaved changes.", confirm: "Reload", destructive: true, run: async () => { confirmDialog.set(null); await reloadFile(); } });
});
/** Esc / Close editor: a dirty editor asks first; a clean one leaves editing, then the file. */
function closeEditor() {
  const f = openFile();
  const leave = () => {
    fileFindOpen.set(false);
    if (f?.draft !== undefined) openFile.set({ ...f, draft: undefined, stale: false });
    else { openFile.set(null); centre.set(viewed() ? "session" : "new_session"); }
  };
  if (!fileDirty()) { leave(); return; }
  confirmDialog.set({ title: "Discard changes", body: "The file has unsaved changes. Leaving the editor drops them.", confirm: "Discard", destructive: true, run: () => { confirmDialog.set(null); leave(); } });
}
on("editor.close", closeEditor);
on("app.usage", () => void showUsage());
on("turn.load_older", () => { void loadOlderMessages(true); });
/** The project picker is also the restore action for an inactive project. */
on("project.switch", () => {
  const items = orderedProjects(model.projects.values()).map((p) => ({
    id: p.id, label: p.name,
    hint: projectStatus(p) === PROJECT_STATUS.current ? p.path : `${projectStatus(p)} · ${p.path}`,
    run: async () => {
      picker.set(null);
      if (projectStatus(p) === PROJECT_STATUS.inactive) await setProjectStatus(p.id, PROJECT_STATUS.current);
      selectProject(p.id);
    },
  }));
  picker.set({ title: "Switch project", items, note: items.length ? undefined : "No projects yet." });
});
on("project.overview", () => openProjectsOverview());
on("sidebar.step_back", () => flowBack());
on("project.add", () => addProjectOpen.set(true), true);
on("project.remove", () => askRemoveProject());
on("project.move_up", () => moveProjectStep(-1), true);
on("project.move_down", () => moveProjectStep(1), true);
for (const status of Object.values(PROJECT_STATUS)) on(`project.status.${status}`, () => setCurrentProjectStatus(status), true);
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
  if (!microphoneAvailable()) {
    focusComposer.fn?.();
    toast("info", platform.os === "ios"
      ? "This build has no in-app microphone capture. Tap the microphone on the iOS keyboard to dictate."
      : "This build has no in-app microphone capture. Use the keyboard's dictation or the TUI.");
    return;
  }
  const target = draftKey();
  startDictation((transcript) => {
    draft.update((all) => ({ ...all, [target]: appendTranscript(all[target] ?? "", transcript) }));
  });
}
// The manifest currently has only the deferred `composer.dictate` command. Keep the
// requested runtime handler registered by its final ID so the mic button can invoke it
// now and the shared manifest can expose it when that command lands.
on("voice.dictate", dictateFromComposer, true);
// The Manager (docs/manager-design.md 10.6). Everything else of it lives in core/manager.ts.
on("manager.details_up", () => scrollDetails(-200));
on("manager.details_down", () => scrollDetails(200));
on("manager.talk", () => talk(), true);
on("manager.header", () => toggleManagerDropdown(), true);
on("manager.audio_toggle", () => toggleManagerAudio(), true);
on("manager.start", () => startQuietly(), true);
on("manager.context", () => openManagerContext(), true);
on("manager.open_session", () => openManagerSession(), true);
on("manager.stop", () => stopCall());
on("manager.minimise", () => minimise());
on("manager.send", () => sendListening());
on("manager.listen_cancel", () => cancelListening());
on("manager.speech_stop", () => stopSpeech());
on("manager.turn_cancel", () => cancelTurn());
on("manager.field_open", () => openField(""));
on("manager.field_send", () => submitField());
on("manager.field_close", () => closeField());
on("manager.card_mic", () => talk(), true);
on("manager.card_dismiss", () => dismissCard());
on("search.start", () => startSearchService(), true);
on("voice.download_stt", () => askVoiceDownload("stt"), true);
on("voice.remove_stt", () => askVoiceRemove("stt"), true);
on("voice.download_tts", () => askVoiceDownload("tts"), true);
on("voice.remove_tts", () => askVoiceRemove("tts"), true);
on("session.copy_id", () => { const s = viewed(); if (s) void Clipboard.writeText(s.id).then(() => toast("success", "Copied session id")); });
on("session.open_inspector", () => inspectSession());
on("tool.open_inspector", () => inspectSession());
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
on("session.resources", () => { resourcesOpen.set(true); void loadResources(); });

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
  const s = viewed();
  const chosenAgent = s?.agent ?? sessionDraft().agent ?? "build";
  const agentModel = agentCatalog().agents.find((a) => a.name === chosenAgent)?.model;
  const selectedModel = s ? s.model : sessionDraft().model || agentModel || rememberedModel(currentProject(), chosenAgent);
  return models.map((m) => {
    const ref = String(m.ref ?? "");
    const provider = ref.split("/")[0] ?? ref;
    const context = Number(m.context);
    const efforts = Array.isArray(m.efforts) ? m.efforts.map(String) : [];
    return {
      id: ref,
      // The TUI shows the display name, with the provider at the row's end and the
      // context and efforts in an info panel under the list.
      label: String(m.name ?? "").trim() || ref.split("/").at(-1) || ref,
      hint: `${provider}${ref === selectedModel ? " · current" : ""}`,
      detail: [
        `Context: ${Number.isFinite(context) && context > 0 ? `${context.toLocaleString()} tokens` : "unknown"}`,
        `Efforts: ${efforts.join(" / ") || "none listed"}`,
      ],
      run: choose(ref, applyModel(ref)),
    };
  });
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
  if (c.id === "search.start") { const why = searchStartReason(); if (why) return { ok: false, reason: why }; }
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
  // In the terminal backend the TUI's keys are the ones that reach the app (upstream maps cmd to ctrl),
  // so show their spelling, as the Rust client does.
  if (platform.idiom === "terminal") return c.keys?.tui ?? null;
  const k = c.keys?.nsh; if (!k) return null;
  if (k === "esc") return "esc";
  try { return keymap.label(toKeystroke(k)); } catch { return k; }
}

const shortcutLabel = (k: string) => platform.idiom === "terminal" ? k : (() => { try { return keymap.label(toKeystroke(k)); } catch { return k; } })();
/** The manifest's keys as text for `?`: the Ctrl+X leader first, then every direct NSH shortcut. */
export function shortcutText(): string {
  const leaders = commands.flatMap((c) => (c.leader ?? []).map((k) => `${shortcutLabel(k).padEnd(8)} ${c.title}`));
  const direct = commands
    .filter((c) => c.keys?.nsh && c.key_context !== "escape_chain")
    .map((c) => `${(keyLabel(c) ?? c.keys!.nsh!).padEnd(12)} ${c.title}`);
  return [
    `Leader · press ${shortcutLabel("ctrl+x")}, then:`,
    ...leaders,
    "",
    "Direct shortcuts",
    ...direct,
  ].join("\n");
}

const GLOBAL_CONTEXTS = new Set(["global"]);
/** Bare keys (enter, space, [, ], @, /, i) belong to the focused control or text field, never a global binding. */
const hasModifier = (k: string) => /(^|\+)(cmd|ctrl|alt)(\+|$)/.test(k) || /^f\d+$/.test(k);
/** Esc is not a per-command lookup: the manifest's ordered rules decide (interface/README.md "Esc precedence"). */
export function escape() {
  if (terminalFocused()) return null;
  if (terminalOpen() && terminalRename()) { terminalRename.set(false); return "terminal_rename"; }
  if (terminalOpen() && !confirmDialog() && !term("overlay.open")) { closeTerminalPanel(); return "terminal"; }
  const drag = railDrag();
  if (drag) { railDrag.set({ ...drag, cancelled: true }); return "rail_drag"; }
  if (inspector()) { closeInspector(); return "inspector"; }
  // The Manager's context modal closes first, then a consent notice cancels or
  // any open call (focused or minimised) ends.
  const manager = managerEscapeRule();
  if (manager) return manager;
  if (isDictationBusy()) { cancelDictation(); return "dictation"; }
  if (annotating()) { annotating.set(null); return "annotate"; }
  if (imagePreview()) { imagePreview.set(null); return "image_preview"; }
  if (contextDetail()) { contextDetail.set(null); return "context_detail"; }
  if (resourcesOpen()) { resourcesOpen.set(false); return "resources"; }
  if (switcherOpen()) { switcherOpen.set(false); return "session_switcher"; }
  if (fileFindOpen()) { fileFindOpen.set(false); return "file_find"; }
  // Settings steps back one level at a time, as the TUI does: a provider/agent
  // detail, then a page's content, then the section rail, then the view.
  if (centre() === "settings" && providerDetail()) { providerDetail.set(null); return "provider_detail"; }
  if (centre() === "settings" && settingsQuery()) { settingsQuery.set(""); return "settings_search"; }
  if (centre() === "settings" && settingsPage()) { settingsPage.set(null); return "settings_page"; }
  if (centre() === "settings" && !settingsNavFocused()) {
    settingsPageOpen.set(false);
    settingsNavFocused.set(true);
    return "settings_rail";
  }
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

const LEADER_PREFIX = "ctrl+x";
const LEADER_MS = 3000;

/** The TUI's Ctrl+X leader: arm the manifest's `leader` keys for a moment, then release them. */
export function bindLeader(): () => void {
  let pending: { off: () => void; timer: ReturnType<typeof setTimeout> } | null = null;
  const disarm = () => {
    if (!pending) return;
    const p = pending; pending = null;
    clearTimeout(p.timer);
    p.off();
  };
  const arm = () => {
    disarm();
    if (terminalFocused() || term("overlay.open") || term("dialog.open") || textEdit.active()) return;
    // Esc cancels the armed leader instead of falling through to interrupt/back.
    const keys: Record<string, () => void> = { escape: disarm };
    for (const c of commands) for (const key of c.leader ?? []) keys[key] = () => { disarm(); void run(c.id); };
    pending = { timer: setTimeout(disarm, LEADER_MS), off: bindOpenKeys(keys) };
  };
  const off = (() => { try { return keymap.bind(LEADER_PREFIX, arm, { when: "always" }); } catch { return null; } })();
  if (!off) return () => {};
  return () => { disarm(); off(); };
}

export function bindKeys(): () => void {
  const offs: (() => void)[] = [];
  for (const c of commands) {
    if (c.id === "app.quit") continue; // platform-owned
    // The terminal backend reaches the TUI's keys, and shows them (keyLabel), so both are live there.
    const keys = platform.idiom === "terminal" ? [c.keys?.tui, c.keys?.nsh] : [c.keys?.nsh];
    for (const k of new Set(keys)) {
      if (!k || k === "esc" || !GLOBAL_CONTEXTS.has(c.key_context ?? "") || !hasModifier(k)) continue;
      try { offs.push(keymap.bind(toKeystroke(k), () => { if (!terminalFocused()) void run(c.id); }, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
    }
  }
  for (const id of ["sidebar.row_menu", "tool.raw_toggle", "tool.show_all_output"]) {
    const c = byId.get(id);
    for (const k of new Set(platform.idiom === "terminal" ? [c?.keys?.tui, c?.keys?.nsh] : [c?.keys?.nsh])) {
      if (!k) continue;
      try {
        offs.push(keymap.bind(toKeystroke(k), () => {
          if (id === "sidebar.row_menu" && focusedSidebarRow()) void run(id);
          if (id === "tool.raw_toggle" && focusedTool()) void run(id);
          if (id === "tool.show_all_output" && focusedTool()?.clipped) void run(id);
        }, { when: "notEditing" }));
      } catch { /* the platform keeps this keystroke */ }
    }
  }
  // The editor's own keys act only while a file is open (cmd+s saves; cmd+e starts editing;
  // cmd+f opens the find bar). A bare terminal key (e) must not be taken from a text field.
  for (const id of ["editor.save", "editor.edit", "editor.find", "editor.preview"]) {
    const c = byId.get(id);
    for (const k of new Set(platform.idiom === "terminal" ? [c?.keys?.tui, c?.keys?.nsh] : [c?.keys?.nsh])) {
      if (!k) continue;
      try { offs.push(keymap.bind(toKeystroke(k), () => { if (openFile()) void run(id); }, { when: hasModifier(k) ? "always" : "notEditing" })); } catch { /* the platform keeps this keystroke */ }
    }
  }
  try { offs.push(keymap.bind("escape", () => { escape(); }, { when: "always" })); } catch {}
  // Left in an empty, focused composer starts the keyboard flow; the key still reaches the input,
  // where it has nothing to move, and with text in the composer this binding does nothing.
  try { offs.push(keymap.bind("left", () => { if (!flowStage() && composerFocused() && term("composer.empty")) flowStart(); }, { when: "always", passThrough: true })); } catch {}
  offs.push(bindLeader());
  return () => offs.forEach((f) => f());
}
void selectProject;

// ---- keyboard flow from an empty composer (PLAN 15.4) ----

/** Open the projects overview. */
export function openProjectsOverview() {
  if (isNarrow()) drawer.set("left"); else leftOpen.set(true);
  leftTab.set("projects");
  flowStage.set("projects");
  projectsOverview.set({ cursor: 0, archived: [] });
}
/** Left in an empty composer: open the left session sidebar and give it the keys. */
export function flowStart() {
  if (term("overlay.open") || term("dialog.open") || textEdit.active()) return;
  leftTab.set("sessions");
  if (isNarrow()) drawer.set("left"); else leftOpen.set(true);
  flowStage.set("sessions");
  flowSession.set(viewed()?.id ?? rowSessions()[0]?.id ?? null);
  blurComposer.fn?.();
}
export function flowEnd() { flowStage.set(null); flowProject.set(null); flowSession.set(null); focusComposer.fn?.(); }
/** Left again: the sessions give way to the project list, whose Left opens the overview. */
function flowIn() {
  const stage = flowStage();
  if (stage === "sessions") {
    leftTab.set("projects");
    flowStage.set("projects");
    flowProject.set(currentProject() ?? orderedProjects(model.projects.values())[0]?.id ?? null);
  } else if (stage === "projects") openProjectsOverview();
}
/** Esc or Right: one step back out, in reverse order. */
function flowBack() {
  if (projectsOverview()) { projectsOverview.set(null); return; }
  const stage = flowStage();
  if (stage === "projects") { leftTab.set("sessions"); flowStage.set("sessions"); flowProject.set(null); flowSession.set(viewed()?.id ?? null); }
  else flowEnd();
}
function flowMove(delta: number) {
  if (flowStage() === "sessions") {
    const ids = rowSessions().map((x) => x.id);
    if (!ids.length) return;
    const at = ids.indexOf(flowSession() ?? "");
    flowSession.set(ids[Math.max(0, Math.min(ids.length - 1, (at < 0 ? (delta > 0 ? -1 : 0) : at) + delta))]);
    return;
  }
  if (flowStage() !== "projects") return;
  const ids = orderedProjects(model.projects.values()).map((p) => p.id);
  if (!ids.length) return;
  const at = ids.indexOf(flowProject() ?? "");
  flowProject.set(ids[Math.max(0, Math.min(ids.length - 1, (at < 0 ? 0 : at) + delta))]);
}
function flowOpen() {
  if (flowStage() === "sessions") {
    const sid = flowSession();
    if (sid) { void selectSession(sid); flowEnd(); }
    return;
  }
  const id = flowProject();
  const project = id ? model.projects.get(id) : undefined;
  if (!id || !project) return;
  if (projectStatus(project) === PROJECT_STATUS.inactive) void setProjectStatus(id, PROJECT_STATUS.current);
  selectProject(id);
  flowEnd();
}
// While the flow holds the keys (and the overview, which binds its own, is closed) these keys are ours.
effect(() => {
  if (!flowStage() || projectsOverview()) return;
  const keys: Record<string, () => void> = { left: flowIn, right: flowBack };
  keys.up = () => flowMove(-1); keys.down = () => flowMove(1); keys.enter = flowOpen;
  const offs: (() => void)[] = [];
  for (const [k, fn] of Object.entries(keys)) {
    try { offs.push(keymap.bind(k, fn, { when: "always" })); } catch { /* the platform keeps this keystroke */ }
  }
    return () => offs.forEach((f) => f());
});
