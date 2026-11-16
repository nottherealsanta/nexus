// The live layout (plan 45, docs/layout.md): the pure tree in layout.ts held in a signal, its persistence per
// project + worktree, the reconciliation with the things tabs stand for (open sessions, files, terminals), and
// every user action on it. The UI (ui/groups.tsx) and the commands only call what is exported here.
import { Storage, computed, effect, signal, untracked } from "nsh";
import { uiText } from "./gen/manifest";
import {
  activate, activeTab, addTab, allTabs, closeGroup as closeGroupNode, closeTab as closeTabNode, collapsed, dropTab, emptyLayout, findGroup, focus as focusNode, focusedGroup, groupRects,
  groupsOf, groupWith, hasTab, join as joinNode, moveTab as moveTabNode, moveToward, neighbour, openTab, openToSide as openToSideNode, parse, resize as resizeNode, restore, serialize,
  split as splitNode, splitFits, splitWith, stepTab as stepNode, tabKey, MIN_H, MIN_W,
  type Dir, type Layout, type Rect, type Side, type Size, type Tab, type Zone,
} from "./layout";
import {
  bufferDirty, confirmDialog, currentFileId, currentProject, currentWorktree, dropBuffer, dropGitDiff, fileBuffers, fileFindOpen, fileTabId, gitDiffs, gitDiffId, live, model, openedTabs, openGitDiff,
  rememberNewSession, rootOf, saveFile, selectSession, showFile, splitFileTabId, toast, viewed, visibleTabIds, centre, bump, phone, selectedGitFile, m, sync, sessionOpened,
} from "./store";
import {
  focusTerminal, forgetTerminal, newTerminal, refreshTerminals, requestTerminalClose, terminalAvailable, terminalItems, terminalOpen, terminalsKnown, terminalView, detachTerminal,
} from "./terminal-panel";

export { MIN_H, MIN_W };
/** The layout tree is for desktop and web (and the iPad); the phone client never reads it. */
export const layoutEnabled = !phone;

export const layout = signal<Layout>(emptyLayout());
/** The centre area the layout fills, as last measured. */
export const layoutSize = signal<Size>({ w: 0, h: 0 });
let cur: Layout = layout();
let loadedKey: string | null = null;
let loadingKey: string | null = null;
let everLoaded = false;
/** What the reconcile effects last applied; a freshly loaded layout starts them over. */
let lastRoot: string | null = null;
let lastOpened = 0;
let lastIds = "";
export const layoutReady = signal(false);
/** The "New session" tab (centre is the new-session view): shown in the focused group, not part of the tree. */
export const newTabShown = signal(false);
/** The terminal tab the user last used; Ctrl+` returns to it. */
let lastTerminal: string | null = null;

const FALLBACK: Size = { w: 1600, h: 900 };
/** The size operations plan with: the measured area, or a roomy default before the first measurement. */
const sizeNow = (): Size => { const s = layoutSize(); return s.w > 0 && s.h > 0 ? s : FALLBACK; };
export const layoutBox = (): Rect => { const s = sizeNow(); return { x: 0, y: 0, w: s.w, h: s.h }; };

// --- storage ------------------------------------------------------------------------------------------------------

const worktreeKey = () => { const w = currentWorktree(); return w && w.main === false ? String(w.path ?? "") : "main"; };
const storageKey = (project: string, worktree: string) => `nexus.layout.${project}.${worktree}`;
const projectKeyNow = () => { const p = currentProject(); return p ? storageKey(p, worktreeKey()) : null; };
function save() {
  if (!loadedKey) return;
  try { void Storage.setItem(loadedKey, serialize(cur)).catch(() => {}); } catch { /* private storage: the layout just is not kept */ }
}

// --- the single writer --------------------------------------------------------------------------------------------

/** Replace the layout; everything derived from it (focused file / terminal, session shown) follows. */
function commit(next: Layout, opts: { select?: boolean } = {}) {
  if (next === cur) return;
  const before = cur;
  cur = next;
  layout.set(next);
  terminalOpen.set(allTabs(next).some((t) => t.kind === "terminal"));
  syncFocus(before, opts.select !== false);
  save();
}
/** The focused group's active tab drives the file commands, the terminal keys and the session in view. */
function syncFocus(before: Layout | null, select: boolean) {
  const g = focusedGroup(cur);
  const tab = activeTab(g);
  const prev = before ? activeTab(focusedGroup(before)) : undefined;
  const fileId = tab?.kind === "file" && !(newTabShown() && g.id === cur.focus) ? tab.id : null;
  if (currentFileId() !== fileId) { fileFindOpen.set(false); currentFileId.set(fileId); }
  focusTerminal(tab?.kind === "terminal" ? tab.id : null);
  if (tab?.kind === "terminal") lastTerminal = tab.id;
  if (select && tab?.kind === "session" && (!prev || tabKey(prev) !== tabKey(tab) || before?.focus !== cur.focus) && rootOf(viewed()?.id) !== tab.id && model.sessions.has(tab.id)) {
    newTabShown.set(false);
    void selectSession(tab.id);
  }
}

// --- reading for the UI and the commands --------------------------------------------------------------------------

export const focusedActive = (): Tab | undefined => { layout(); return activeTab(focusedGroup(cur)); };
export const hasActiveTab = () => !!focusedActive() || (newTabShown() && layoutEnabled);
export const hiddenGroups = () => { layout(); return collapsedNow() ? groupsOf(cur.root).length - 1 : 0; };
export function collapsedNow(): boolean { layout(); const s = layoutSize(); return s.w > 0 && collapsed(cur, s); }
/** The groups that are drawn: all of them, or the focused one alone when the area is too small. */
export const shownRects = (): Map<string, Rect> => {
  layout();
  const s = sizeNow();
  const all = groupRects(cur, s);
  if (!collapsedNow()) return all;
  const f = focusedGroup(cur);
  return new Map([[f.id, { x: 0, y: 0, w: s.w, h: s.h }]]);
};
const can = (dir: Dir) => { layout(); return !!neighbour(cur, cur.focus, dir, sizeNow()); };
export const canFocus = can;
export const canJoinRight = () => can("right");
export const canJoinDown = () => can("down");
export const multipleGroups = () => { layout(); return groupsOf(cur.root).length > 1; };
/** The group that shows the live timeline and composer: the focused one if it shows a session, else the one showing the viewed session. */
export function sessionOwner(): string | null {
  layout(); newTabShown();
  const f = focusedGroup(cur);
  if (newTabShown()) return f.id;
  const root = rootOf(viewed()?.id);
  const t = activeTab(f);
  if (t?.kind === "session" && (!root || t.id === root)) return f.id;
  if (root) {
    const g = groupsOf(cur.root).find((x) => activeTab(x)?.kind === "session" && activeTab(x)!.id === root);
    if (g) return g.id;
  }
  return t?.kind === "session" ? f.id : null;
}
/**
 * Sessions a drawn group shows without owning the composer (plan 45): each gets a live, read-only timeline, so
 * several sessions can be watched side by side. The TUI's `background_sessions`.
 */
export function backgroundSessions(): string[] {
  layout(); m();
  const owner = sessionOwner();
  const ids: string[] = [];
  for (const [gid] of shownRects()) {
    if (gid === owner) continue;
    const b = bodyOf(gid);
    if (b && b !== "new" && b.kind === "session" && b.id && model.sessions.has(b.id) && !ids.includes(b.id)) ids.push(b.id);
  }
  return ids.sort();
}
/** What a group body shows: the New session tab wins in the focused group while it is shown. */
export function bodyOf(groupId: string): Tab | "new" | undefined {
  layout();
  if (newTabShown() && groupId === cur.focus) return "new";
  const g = findGroup(cur, groupId);
  return g ? activeTab(g) : undefined;
}
export const groupOf = (groupId: string) => { layout(); return findGroup(cur, groupId); };
export const groupIds = (): string[] => { layout(); return groupsOf(cur.root).map((g) => g.id); };
export const focusedGroupId = () => { layout(); return cur.focus; };

// --- focus and activation -----------------------------------------------------------------------------------------

export function focusGroup(id: string) {
  if (newTabShown() && id !== cur.focus) { /* the New session tab stays in its group */ }
  commit(focusNode(cur, id));
}
/** A tab was chosen: it becomes its group's active tab and the group takes focus. */
export function activateTab(groupId: string, key: string) {
  const wasNew = newTabShown();
  newTabShown.set(false);
  const next = activate(cur, groupId, key);
  if (next === cur) { if (wasNew) syncFocus(null, true); } else commit(next);
}
export function showNewTab() { newTabShown.set(true); syncFocus(cur, false); }
export function hideNewTab() { if (newTabShown()) { newTabShown.set(false); syncFocus(cur, false); } }
export function focusDirection(dir: Dir): boolean {
  const target = neighbour(cur, cur.focus, dir, sizeNow());
  if (!target) { toast("info", uiText("layout.no_neighbour", { direction: dir === "up" ? "above" : dir === "down" ? "below" : dir })); return false; }
  focusGroup(target);
  return true;
}
export function stepTabInFocused(step: number) {
  const g = focusedGroup(cur);
  const items = g.tabs;
  if (items.length < 2) return;
  const next = stepNode(cur, step);
  const t = activeTab(focusedGroup(next));
  if (t) activateTab(g.id, tabKey(t));
}

// --- opening ------------------------------------------------------------------------------------------------------

/** Show a file in the focused group (or, to the side, in the group to its right). */
export function openFileTab(path: string, side = false) {
  const project = currentProject(); if (!project) return;
  const tab: Tab = { kind: "file", id: fileTabId(project, path) };
  newTabShown.set(false);
  openIn(tab, side);
  void showFile(path, project);
}
export function openDiffTab(side = false) {
  const project = currentProject(), sel = selectedGitFile();
  const id = openGitDiff();
  if (!project || !sel || !id) return;
  newTabShown.set(false);
  openIn({ kind: "diff", id }, side);
}
export function openSessionTab(id: string, side = false) {
  newTabShown.set(false);
  openedTabs.update((tabs) => (tabs.includes(id) ? tabs : [...tabs, id]));
  if (side) { openIn({ kind: "session", id }, true); return; }
  void selectSession(id);
}
function openIn(tab: Tab, side: boolean) {
  if (side) {
    const r = openToSideNode(cur, tab, sizeNow());
    if (!r.ok) { refuse(r.reason); return; }
    commit(r.layout);
  } else commit(openTab(cur, tab));
}
function refuse(reason: string) {
  if (reason === "too_small") toast("warning", uiText("layout.too_small", { cols: Math.floor(MIN_W / 9.6), rows: Math.floor(MIN_H / 24) }));
  else if (reason === "no_neighbour") toast("info", uiText("layout.no_neighbour", { direction: "that way" }));
}

/** Ctrl+`: back to the terminal tab last used, or a new terminal in a split below the focused group. */
export async function openTerminalTab() {
  if (!terminalAvailable || !currentProject()) return;
  const terms = allTabs(cur).filter((t) => t.kind === "terminal");
  const last = terms.find((t) => t.id === lastTerminal) ?? terms[terms.length - 1];
  if (last) { focusTab({ kind: "terminal", id: last.id }); return; }
  await newTerminalTab(true);
}
/** Make an existing tab the focused group's, whichever group holds it. */
export function focusTab(tab: Tab) {
  const g = groupWith(cur, tabKey(tab));
  if (g) { activateTab(g.id, tabKey(tab)); if (tab.kind === "terminal") requestScreenFocus(tab.id); }
}
const screens = new Map<string, () => void>();
/** A terminal pane registers how to give its screen the keyboard. */
export function setScreenFocus(id: string, fn: (() => void) | null) { if (fn) screens.set(id, fn); else screens.delete(id); }
const requestScreenFocus = (id: string) => screens.get(id)?.();

/** A new terminal: beside the terminal the focused group has (a tab in it), else in a split below. */
export async function newTerminalTab(forceBelow = false) {
  if (!terminalAvailable || !currentProject()) return;
  const g = focusedGroup(cur);
  const below = forceBelow || !g.tabs.some((t) => t.kind === "terminal");
  if (below && g.tabs.length && !splitFits(cur, g.id, "bottom", sizeNow())) { refuse("too_small"); return; }
  const item = await newTerminal();
  if (!item) return;
  newTabShown.set(false);
  const tab: Tab = { kind: "terminal", id: item.id };
  if (below && g.tabs.length) {
    const r = splitWith(cur, cur.focus, "bottom", [tab], sizeNow());
    if (!r.ok) { commit(openTab(cur, tab)); return; }
    commit(r.layout);
  } else commit(openTab(cur, tab));
  requestScreenFocus(item.id);
}

// --- split, join, move --------------------------------------------------------------------------------------------

/** Split right / down. A terminal gets a new terminal in the new group; anything else is shown again. */
export async function splitFocused(side: "right" | "down") {
  const g = focusedGroup(cur);
  const tab = activeTab(g);
  if (!tab) return;
  const target: Side = side === "right" ? "right" : "bottom";
  if (!splitFits(cur, g.id, target, sizeNow())) { refuse("too_small"); return; }
  if (tab.kind === "terminal") {
    const item = await newTerminal();
    if (!item) return;
    const r = splitWith(cur, cur.focus, target, [{ kind: "terminal", id: item.id }], sizeNow());
    if (r.ok) commit(r.layout); else refuse(r.reason);
    return;
  }
  const r = splitNode(cur, side, tab, sizeNow());
  if (r.ok) commit(r.layout); else refuse(r.reason);
}
export function joinFocused(dir: "right" | "down") {
  const r = joinNode(cur, dir, sizeNow());
  if (r.ok) commit(r.layout); else refuse(r.reason);
}
export function moveActive(dir: Dir) {
  const r = moveToward(cur, dir, sizeNow());
  if (r.ok) commit(r.layout); else refuse(r.reason);
}
/** Drop a tab on a group body: a zone splits (or moves into) the group. `copy` keeps the source (never for a terminal). */
export function dropOnBody(from: string, key: string, to: string, zone: Zone, copy = false) {
  const src = findGroup(cur, from);
  const tab = src?.tabs.find((t) => tabKey(t) === key);
  if (!src || !tab) return;
  const keep = copy && tab.kind !== "terminal";
  if (zone === "centre") { commit(moveTabNode(cur, from, key, to, undefined, keep)); return; }
  if (from === to && src.tabs.length < 2) return;
  const side: Side = zone;
  if (!splitFits(cur, to, side, sizeNow())) { refuse("too_small"); return; }
  const r = splitWith(cur, to, side, [tab], sizeNow(), MIN_W, MIN_H, key);
  if (!r.ok) { refuse(r.reason); return; }
  const fresh = r.group!;
  commit(keep ? r.layout : closeTabNode(r.layout, from, key));
  commit(focusNode(cur, fresh));
}
/** Drop a tab on a group's strip at an index. */
export function dropOnStrip(from: string, key: string, to: string, index: number, copy = false) {
  const tab = findGroup(cur, from)?.tabs.find((t) => tabKey(t) === key);
  commit(moveTabNode(cur, from, key, to, index, copy && tab?.kind !== "terminal"));
}
/** Drag a divider: `delta` is the pointer's travel since the drag began from `base` (the layout then). */
export function resizeDivider(path: number[], index: number, delta: number, base: Layout = cur) {
  const next = resizeNode(base, path, index, delta, sizeNow());
  if (next !== cur) { cur = next; layout.set(next); save(); }
}
export const layoutNow = () => cur;

// --- closing ------------------------------------------------------------------------------------------------------

/** The label a tab shows. */
export function tabTitle(tab: Tab): string {
  if (tab.kind === "session") return model.sessions.get(tab.id)?.title || "Untitled";
  if (tab.kind === "file") return splitFileTabId(tab.id).path.split("/").pop() || tab.id;
  if (tab.kind === "terminal") return terminalItems().find((t) => t.id === tab.id)?.name ?? "Terminal";
  if (tab.kind === "diff") { const d = gitDiffs()[tab.id]; return d ? `${d.path.split("/").pop()} (${d.group})` : "Diff"; }
  return "Browser";
}
export const tabDirty = (tab: Tab) => tab.kind === "file" && bufferDirty(tab.id);

/** Close a tab: unsaved files ask; a terminal asks while it runs, then terminates; a session closes its header tab. */
export function closeTabAt(groupId: string, key: string) {
  const g = findGroup(cur, groupId);
  const tab = g?.tabs.find((t) => tabKey(t) === key);
  if (!g || !tab) return;
  if (tab.kind === "terminal") { requestTerminalClose(tab.id); return; }
  const shownElsewhere = groupsOf(cur.root).some((x) => x.id !== groupId && hasTab(x, key));
  if (tab.kind === "file" && !shownElsewhere && bufferDirty(tab.id)) { askUnsaved([tab], () => removeTab(groupId, key)); return; }
  removeTab(groupId, key);
}
function removeTab(groupId: string, key: string) {
  const g = findGroup(cur, groupId);
  const tab = g?.tabs.find((t) => tabKey(t) === key);
  if (!g || !tab) return;
  const elsewhere = groupsOf(cur.root).some((x) => x.id !== groupId && hasTab(x, key));
  const wasViewed = tab.kind === "session" && rootOf(viewed()?.id) === tab.id;
  const next = closeTabNode(cur, groupId, key);
  if (tab.kind === "session" && !elsewhere) openedTabs.update((tabs) => tabs.filter((t) => t !== tab.id));
  commit(next, { select: false });
  if (!elsewhere) {
    if (tab.kind === "file") dropBuffer(tab.id);
    if (tab.kind === "diff") dropGitDiff(tab.id);
  }
  if (wasViewed && !elsewhere) {
    // Focus moves to a neighbour: the group's new active session, else any other open session, else a new session.
    const f = activeTab(focusedGroup(cur));
    const other = f?.kind === "session" ? f : allTabs(cur).find((t) => t.kind === "session");
    if (other) { const g2 = groupWith(cur, tabKey(other)); if (g2) commit(activate(cur, g2.id, tabKey(other)), { select: false }); void selectSession(other.id); }
    else { rememberNewSession(currentProject()); model.select(null); centre.set("new_session"); showNewTab(); bump(); }
  } else syncFocus(null, true);
}
/** Close the focused group's active tab (layout.tab_close and session.tab_close). */
export function closeFocusedTab() {
  if (newTabShown() && !activeTab(focusedGroup(cur))) { newTabShown.set(false); return; }
  const g = focusedGroup(cur);
  if (g.active) closeTabAt(g.id, g.active);
}
/** Close a whole group: asks once when it holds unsaved files; its tabs go with it. */
export function closeGroupTabs(groupId: string) {
  const g = findGroup(cur, groupId);
  if (!g) return;
  const dirty = g.tabs.filter((t) => t.kind === "file" && bufferDirty(t.id) && !groupsOf(cur.root).some((x) => x.id !== groupId && hasTab(x, tabKey(t))));
  const go = () => { for (const t of [...g.tabs]) { if (t.kind === "terminal") requestTerminalClose(t.id); else removeTab(groupId, tabKey(t)); } };
  if (dirty.length) askUnsaved(dirty, go); else go();
}
/** Save / Discard / Cancel before unsaved text is lost. */
function askUnsaved(files: Tab[], done: () => void) {
  const one = files.length === 1;
  const name = one ? tabTitle(files[0]) : "";
  confirmDialog.set({
    title: one ? uiText("layout.unsaved.title", { name }) : uiText("layout.unsaved.group", { count: files.length }),
    body: uiText("layout.unsaved.body"),
    confirm: uiText("layout.unsaved.save"), destructive: false,
    run: async () => {
      for (const f of files) { await saveFile(f.id); if (bufferDirty(f.id)) throw new Error("Could not save " + tabTitle(f)); }
      done();
    },
    alt: { label: uiText("layout.unsaved.discard"), testID: "unsaved-discard", run: () => { for (const f of files) { const b = fileBuffers()[f.id]; if (b) fileBuffers.update((all) => ({ ...all, [f.id]: { ...b, draft: b.content } })); } done(); } },
  });
}

// --- reconciliation and restore -----------------------------------------------------------------------------------

// `m()` subscribes to the model: without it this never recomputed, so opening a session did not move the focus.
const rootNow = computed(() => rootOf(m().selected?.sessionId));
const tabIdsNow = computed(() => visibleTabIds().join("\n"));

/**
 * Keep the tree and the world in step: sessions that are open (or gone) get (or lose) their tabs, the session
 * being viewed is revealed in its group, and a terminal that was closed or ended takes its tab with it.
 */
export function installLayout() {
  // Every drawn session tab keeps a live view: pin it and have its history read; one no longer drawn lets go.
  let watching = "";
  effect(() => {
    const selected = m().selected?.sessionId;
    const ids = backgroundSessions().filter((id) => id !== selected);
    const k = ids.join(",");
    if (k === watching) return;
    watching = k;
    untracked(() => { model.pin(ids); sync.watch(ids); bump(); });
  });
  // Restore for the project + worktree, once the connection is live.
  effect(() => {
    const project = currentProject();
    // Sessions are known once Core has said anything (a snapshot); the restore prunes against them.
    m();
    if (!project || !layoutEnabled || (!live() && model.projects.size === 0)) return;
    const key = projectKeyNow();
    if (!key || key === loadedKey || key === loadingKey) return;
    loadingKey = key;
    untracked(() => { void load(key).finally(() => { if (loadingKey === key) loadingKey = null; }); });
  });
  effect(() => {
    const ids = tabIdsNow();
    if (!layoutReady()) return;
    if (ids === lastIds) return;
    lastIds = ids;
    untracked(() => {
      const open = new Set(visibleTabIds());
      let next = cur;
      for (const t of allTabs(next)) if (t.kind === "session" && !open.has(t.id)) next = dropTab(next, tabKey(t));
      for (const id of open) if (!allTabs(next).some((t) => t.kind === "session" && t.id === id)) next = addTab(next, { kind: "session", id });
      if (next !== cur) commit(next, { select: false });
    });
  });
  effect(() => {
    const root = rootNow();
    // Opening the session already in view still brings its tab forward (and the highlight with it).
    const opened = sessionOpened();
    if (!layoutReady()) return;
    if (root === lastRoot && opened === lastOpened) return;
    lastRoot = root;
    lastOpened = opened;
    if (!root) return;
    untracked(() => {
      newTabShown.set(false);
      const tab: Tab = { kind: "session", id: root };
      const g = groupWith(cur, tabKey(tab));
      commit(g ? activate(cur, g.id, tabKey(tab)) : openTab(cur, tab), { select: false });
      syncFocus(null, false);
    });
  });
  effect(() => {
    const items = terminalItems();
    if (!layoutReady() || !terminalsKnown()) return;
    untracked(() => {
      let next = cur;
      for (const t of allTabs(next)) {
        if (t.kind !== "terminal") continue;
        const item = items.find((i) => i.id === t.id);
        if (!item || item.status === ("closed" as string)) { next = dropTab(next, tabKey(t)); forgetTerminal(t.id); }
      }
      if (next !== cur) commit(next, { select: false });
    });
  });
  // The New session tab shows whenever nothing is viewed or the new-session view is asked for.
  effect(() => {
    const c = centre(), v = viewed();
    untracked(() => {
      if (c === "new_session" || (c === "session" && !v && !activeTab(focusedGroup(cur)))) { if (!newTabShown()) showNewTab(); }
      else if (newTabShown() && v) newTabShown.set(false);
    });
  });
}

async function load(key: string) {
  loadedKey = null;
  layoutReady.set(false);
  // Tabs opened before the layout was restored stay; later (a worktree swap) only unsaved files are carried over.
  const carried = allTabs(cur).filter((t) => (!everLoaded && (t.kind === "file" || t.kind === "diff")) || (t.kind === "file" && bufferDirty(t.id)));
  let raw: string | null = null;
  try { raw = await Storage.getItem(key); } catch { /* none */ }
  // Terminal tabs are pruned against Core's list when it is already known; otherwise they stay and the reconcile effect
  // drops the dead ones when the list arrives (the restore never waits on a slow request).
  if (terminalAvailable) void refreshTerminals();
  const project = currentProject();
  if (!project) return;
  const saved = parse(raw);
  const exists = (t: Tab) => t.kind === "session" ? !!model.sessions.get(t.id) && !model.sessions.get(t.id)!.archived
    : t.kind === "terminal" ? !terminalsKnown() || terminalItems().some((i) => i.id === t.id)
    : t.kind === "browser" ? false : true;
  let next = saved ? restore(saved, exists) : emptyLayout();
  for (const t of carried) if (!allTabs(next).some((x) => tabKey(x) === tabKey(t))) next = addTab(next, t);
  loadedKey = key; everLoaded = true;
  lastRoot = null; lastIds = "";
  cur = next;
  // Restored sessions are open tabs again (the header tabs of this run are not stored anywhere else).
  const restoredSessions = allTabs(next).filter((t) => t.kind === "session").map((t) => t.id);
  if (restoredSessions.length) openedTabs.update((tabs) => [...tabs, ...restoredSessions.filter((id) => !tabs.includes(id))]);
  layout.set(next);
  terminalOpen.set(allTabs(next).some((t) => t.kind === "terminal"));
  // Re-read the files the restored tabs show; a file that is gone drops its tab quietly.
  for (const t of allTabs(next)) if (t.kind === "file" && !fileBuffers()[t.id]) void restoreFile(t);
  layoutReady.set(true);
  // The restored focused tab is a session: show it, unless the user already picked another while this loaded.
  syncFocus(null, !viewed());
}
async function restoreFile(t: Tab) {
  const { project, path } = splitFileTabId(t.id);
  await showFile(path, project);
  if (fileBuffers()[t.id]?.error) { dropBuffer(t.id); commit(dropTab(cur, tabKey(t)), { select: false }); }
}

/** A terminal's pane went away; Core keeps the terminal and its attachment is released. */
export const releaseTerminalView = (id: string) => detachTerminal(id);
export { terminalView };

// --- drag state ---------------------------------------------------------------------------------------------------

export type DragTarget = { kind: "strip"; group: string; index: number } | { kind: "body"; group: string; zone: Zone } | null;
/** A tab being dragged: where it came from, the pointer (in the layout area) and what it would drop on. */
export const layoutDrag = signal<{ from: string; key: string; x: number; y: number; copy: boolean; target: DragTarget } | null>(null);
export function cancelLayoutDrag(): boolean {
  if (!layoutDrag()) return false;
  layoutDrag.set(null);
  toast("info", uiText("layout.drop.cancelled"));
  return true;
}
/** Tab strips register how to focus themselves (Ctrl+] from a terminal screen lands there). */
const strips = new Map<string, () => void>();
export const registerStrip = (groupId: string, focusFn: (() => void) | null) => { if (focusFn) strips.set(groupId, focusFn); else strips.delete(groupId); };
export function focusStripOf(tabId: string) {
  const g = groupsOf(cur.root).find((x) => x.tabs.some((t) => t.id === tabId));
  if (g) strips.get(g.id)?.();
}

/** The group holding a tab by kind and id. */
export function groupIdOfTab(kind: Tab["kind"], id: string): string | null {
  return groupWith(cur, tabKey({ kind, id }))?.id ?? null;
}

/** The group whose menu is open (the ⋯ glyph, or layout.group_menu for the focused group). */
export const groupMenuFor = signal<string | null>(null);
export function openGroupMenu() { groupMenuFor.set(cur.focus); }

/**
 * The modifier keys held at the last pointer event: Ctrl/Cmd+click ("open to the side") and Alt-drag (copy).
 * A press reports the keys held when it began (`PressEvent.modifiers`); a mouse move over the area reports them
 * as they are now (an Alt pressed or released mid-drag), and the press's own set fills in when it moves not at all.
 */
export const pointerMods = { alt: false, ctrl: false, cmd: false };
/** The host's `Modifiers` (`control`, `alt`, `shift`, `platform`) as the three this module tests. */
export function rememberMods(m: any): void {
  if (!m) return;
  pointerMods.alt = !!m.alt;
  pointerMods.ctrl = !!(m.control ?? m.ctrl);
  pointerMods.cmd = !!(m.platform ?? m.cmd ?? m.meta);
}
export const trackMods = {
  onMouseMove: (e: any) => rememberMods(e?.modifiers),
  onMouseEnter: (e: any) => rememberMods(e?.modifiers),
};
/** A press that begins a drag: the keys it reports, so Alt-drag copies even with no mouse move before the release. */
export const pressMods = { onPressDown: (e: any) => rememberMods(e?.modifiers) };
/** A click with Ctrl or Cmd ("open to the side"): the press's own keys, else those of the pointer's last move. */
export const sideClick = (e?: any) => {
  if (e?.modifiers) rememberMods(e.modifiers);
  return pointerMods.ctrl || pointerMods.cmd;
};
