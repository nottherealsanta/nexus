import { fileWatchStatus } from "../file-watch";
import type { JSX } from "nsh";
import { List, PanResponder, Scroll, Text, View, effect, safeArea, signal } from "nsh";
import { Button, ContextMenu, MenuItem, Tab, Tabs, Input, Tooltip } from "nsh/base";
import { Btn, Empty, FocusEdge, Icon, IconButton, Status, AttentionMark, T, TS, FOCUS, relTime, abbrev, midTruncate, shortPath } from "./kit";
import { manifest } from "../gen/manifest";
import { desktopOnly, phoneSessionGroups } from "../mobile";
import { availability, byId, commands, keyLabel, moveProject, newSession, run, setProjectStatus, toggleLeft } from "../commands";
import { railDrag, archivedFilter, centre, connection, currentProject, drawer, leftOpen, railShown, focusedSidebarRow, flowProject, flowSession, flowStage, info, isNarrow, leftTab, m, model, scopeAll, selectProject, selectSession, sidebarRowMenu, tick, live, touch, userHome, viewed, savePrefs, SidebarRowTarget, loadProjectWorktrees, loadSessionWorktrees, projectWorktrees, worktreeForDirectory, currentWorktree, phone, phonePage, width } from "../store";
import { Project, Session } from "../core/api";
import { request } from "../core/transport";
import { fileTree, filesExpanded, loadFiles, openFile, showFile, toggleFileDir, selectedFileEntry, gitPage, loadGitPage, selectedGitFile, openGitDiff, gitOpen, toggleGitFolder, gitSummary, gitFolders, gitGroups, inGitGroup } from "../store";
import { fileRows } from "../file-tree";
import { icons, IconName } from "../icons";
import { fold } from "../gen/theme";
import { ProjectMcpPanel } from "./project-mcp";
import { PROJECT_STATUS, dropMove, orderedProjects, projectActivity, projectCounts, projectLabels, projectStatus, railCount, railProjects } from "../project-rail";
import { defaultLeftSidebarWidth } from "./geometry";
import { PhoneSessions } from "./mobile/sessions";
import { closeDrawer } from "./mobile/state";

export const leftSidebarWidth = signal(272);
const sidebarResized = signal(false);
effect(() => { if (!sidebarResized()) leftSidebarWidth.set(defaultLeftSidebarWidth(width())); });
/** The project rail's width. */
export const RAIL_PX = 40;
const tabs = manifest.navigation.left_sidebar.tab as unknown as { id: "sessions" | "files" | "git" | "projects"; label: string; command: string; order: number }[];

function hint(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * The left sidebar's switch icons (Sessions, Files, Projects). Docked, they live in the
 * header's left slot over the sidebar; in a narrow drawer they sit in the drawer's own top row
 * with labels.
 */
export function SidebarTabs(p: { labels?: boolean }) {
  return (
    <Tabs class={`flex-row items-center gap-0.5 ${p.labels ? "" : "rounded-(--radius) bg-raised p-0.5"}`}>
      <Each of={[...tabs].sort((a, b) => a.order - b.order)} key={(t) => t.id}>
        {(t) => (
          <View tooltip={<Tooltip delay={200}><Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{t().label}{hint(t().command) ? `  ${hint(t().command)}` : ""}</Text></Tooltip>}><Tab
            selected={leftTab() === t().id} accessibilityLabel={t().label} testID={`tab-${t().id}`} disabled={false}
            onPress={() => { leftTab.set(t().id); if (t().id === "files") void loadFiles(); if (t().id === "git") void loadGitPage(); savePrefs(); }}
            class={`flex-row items-center justify-center gap-1.5 rounded-(--radius) hover:bg-accent active:bg-selection ${p.labels ? "px-2 min-h-11" : "size-8"} ${leftTab() === t().id ? (p.labels ? "bg-background" : "bg-selection") : ""} ${FOCUS}`}
          >
            <Icon name={(t().id === "git" ? "branch" : t().id) as IconName} size={16} class={leftTab() === t().id ? "tint-foreground" : "tint-muted-foreground"} />
            <If when={!!p.labels}><Text class={`${TS.base} ${leftTab() === t().id ? "text-foreground font-semibold" : "text-muted-foreground"}`}>{t().label}</Text></If>
          </Tab></View>
        )}
      </Each>
    </Tabs>
  );
}

export function LeftSidebar(p: { drawer?: boolean }) {
  let startWidth = 264;
  const resize = PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onPanResponderGrant: () => { sidebarResized.set(true); startWidth = leftSidebarWidth(); },
    onPanResponderMove: (_event, gesture) => { leftSidebarWidth.set(Math.max(180, Math.min(480, startWidth + gesture.dx))); },
  });
  return (
    <View class={`bg-sidebar ${p.drawer ? "flex-1" : "shrink-0 border-r border-border"}`} style={p.drawer ? {} : { width: leftSidebarWidth() }} testID="left-sidebar" role="none" accessibilityLabel="Left sidebar">
      <If when={!!p.drawer}>
        <View class="flex-row items-center h-14 px-2"><SidebarTabs labels /></View>
      </If>
      <View class="flex-1 min-h-0">
        <If when={leftTab() === "sessions"}><SessionsTab /></If>
        <If when={leftTab() === "files"}><FilesTab /></If>
        <If when={leftTab() === "git"}><GitTab /></If>
        <If when={leftTab() === "projects"}><ProjectsTab /></If>
      </View>
      <If when={!p.drawer}><View {...resize.panHandlers} class="absolute right-0 top-0 bottom-0 w-2" testID="left-sidebar-resize" accessibilityLabel="Resize left sidebar" /></If>
    </View>
  );
}
function GitTab() {
  effect(() => { const project = currentProject(), worktree = currentWorktree()?.path; if (project && (gitPage()?.project !== project || gitPage()?.worktree !== worktree)) void loadGitPage(); });
  const data = () => gitPage()?.data;
  const changes = () => data()?.changes ?? [];
  const summary = () => gitSummary(changes());
  const folderName = () => (data()?.worktree ?? "").split("/").filter(Boolean).pop() ?? "";
  return <Scroll class="flex-1"><View class="p-2" testID="git-page">
    <Text class={`${TS.base} font-medium text-foreground px-2 pb-2`}>Git</Text>
    <If when={!!gitPage()?.error}><Text class={`${T.sec} px-2`}>{gitPage()?.error}</Text></If>
    <If when={!!data()}>
      <Text class={`${TS.base} px-2 pb-2`} testID="git-branch">{`${folderName()} · ${data()?.branch ?? ""}`}</Text>
      <If when={changes().length === 0}><Text class={`${T.meta} px-2`} testID="git-empty">No changes</Text></If>
      <If when={changes().length > 0}>
        <Text class={`${TS.base} px-2`} testID="git-summary">{`${summary().files} ${summary().files === 1 ? "file" : "files"} changed · +${summary().added} −${summary().removed}`}</Text>
        <Text class={`${T.meta} px-2 pb-2`} testID="git-groups">{`${summary().staged} staged · ${summary().unstaged} unstaged · ${summary().untracked} untracked`}</Text>
        <Each of={gitFolders(changes())} key={(f) => f.name}>{(folder) => <View>
          <Button testID={`git-folder-${folder().name}`} onPress={() => toggleGitFolder(folder().name)} class={`flex-row gap-2 px-2 h-7 rounded-(--radius) hover:bg-accent ${FOCUS}`}><Text class={T.meta}>{gitOpen().has(folder().name) ? "▾" : "▸"}</Text><Text class={TS.base}>{folder().name}</Text><Text class={T.meta}>{`${folder().files.length}`}</Text></Button>
          <If when={gitOpen().has(folder().name)}>
            <Each of={gitGroups} key={(g) => g}>{(group) => <Each of={folder().files.filter((c) => inGitGroup(c, group()))} key={(c) => c.path}>{(change) => <ContextMenu onOpenChange={({ open }) => { if (open) selectedGitFile.set({ path: change().path, group: group() }); }} popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg" trigger={<Button testID={`git-${group().toLowerCase()}-${change().path}`} onPress={() => { selectedGitFile.set({ path: change().path, group: group() }); void openGitDiff(); }} class={`flex-row gap-2 pl-5 pr-2 h-7 rounded-(--radius) hover:bg-accent ${FOCUS}`}><Text class={T.meta}>{group() === "Staged" ? change().status[0] : group() === "Untracked" ? "?" : change().status[1]}</Text><Text class={TS.base}>{change().path}</Text></Button>}>
              <Each of={commands.filter((c) => ["git.stage", "git.unstage", "git.discard"].includes(c.id) && c.placement.includes("row_menu"))} key={(c) => c.id}>{(cmd) => <MenuItem onSelect={() => void run(cmd().id)} disabled={!availability(cmd()).ok} class="rounded-(--radius) px-2 py-1.5 hover:bg-accent"><Text class={TS.base}>{cmd().title}</Text></MenuItem>}</Each>
            </ContextMenu>}</Each>}</Each>
          </If>
        </View>}</Each>
      </If>
    </If>
  </View></Scroll>;
}
/** Compact project rail shown when the docked sidebar is collapsed. */
export function SessionRail() {
  const projects = () => { m(); return railProjects(model.projects.values()); };
  const labels = () => projectLabels(projects());
  const activity = (id: string) => { m(); return projectActivity(id, model.sessions.values(), model.running, tick()); };
  const counts = (id: string) => { m(); return projectCounts(id, model.sessions.values(), model.approvals.values(), model.questions.values(), model.running, model.unreadSessions); };
  // The rail popup uses two rows of two, in aligned columns.
  const details = (id: string) => {
    const c = counts(id);
    return [`${c[0]} attention`, `${c[1]} unread`, `${c[2]} running`, `${c[3]} idle`];
  };
  // Dragging a tile: the pointer's vertical travel picks the insertion index; release commits it, Esc or
  // leaving the rail sideways cancels, and a press without travel stays a click.
  let tileHeight = 56;
  const RAIL_GAP = 16;
  const dragPan = (id: string) => PanResponder.create({
    onStartShouldSetPanResponder: () => false,
    onMoveShouldSetPanResponder: (_event, g) => Math.abs(g.dy) > 4,
    onPanResponderGrant: () => railDrag.set({ id, to: projects().findIndex((p) => p.id === id), cancelled: false }),
    onPanResponderMove: (_event, g) => {
      const d = railDrag();
      if (!d || d.id !== id) return;
      const ids = projects();
      const from = ids.findIndex((p) => p.id === id);
      const to = Math.max(0, Math.min(ids.length - 1, from + Math.round(g.dy / (tileHeight + RAIL_GAP))));
      railDrag.set({ ...d, to, cancelled: d.cancelled || Math.abs(g.dx) > RAIL_PX });
    },
    onPanResponderRelease: () => {
      const d = railDrag();
      railDrag.set(null);
      if (!d || d.cancelled) return;
      const move = dropMove(projects().map((p) => p.id), id, d.to);
      if (move) void moveProject(id, move);
    },
    onPanResponderTerminate: () => railDrag.set(null),
  });
  const railMenu = signal<string | null>(null);
  const railTip = signal<string | null>(null);
  const railFocus = signal<string | null>(null);
  const tones = ["text-multi", "text-info", "text-success", "text-foreground"];
  const CORNERS = [{ at: 0, kind: "attention" }, { at: 1, kind: "finished" }, { at: 2, kind: "running" }, { at: 3, kind: "idle" }];
  return (
    <View class="shrink-0 bg-sidebar border-r border-border items-center pt-6" style={{ width: RAIL_PX }} testID="session-rail" accessibilityLabel="Project rail">
      <Scroll class="flex-1">
        <View class="items-center gap-4 pb-2">
        <Each of={projects()} key={(p) => p.id}>
          {(p) => {
            const pan = dragPan(p().id);
            const passive = () => projectStatus(p()) === PROJECT_STATUS.passive;
            const dragging = () => railDrag()?.id === p().id;
            const target = () => { const d = railDrag(); return !!d && !d.cancelled && projects()[d.to]?.id === p().id && d.id !== p().id; };
            return (
              <ContextMenu
                open={railMenu() === p().id}
                onOpenChange={({ open }) => {
                  railMenu.set(open ? p().id : railMenu() === p().id ? null : railMenu());
                  if (open) focusedSidebarRow.set({ kind: "project", id: p().id });
                  else if (focusedSidebarRow()?.id === p().id) focusedSidebarRow.set(null);
                }}
                popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg"
                trigger={
              <View role="button" focusable {...pan.panHandlers}
                onLayout={(b: any) => { tileHeight = b.bounds?.size?.height ?? b.size?.height ?? tileHeight; }}
                onPress={() => {
                  if (railDrag()) return;
                  if (currentProject() === p().id) {
                    scopeAll.set(false);
                    if (isNarrow()) { leftTab.set("sessions"); drawer.set(drawer() === "left" ? null : "left"); }
                    else if (leftOpen()) leftOpen.set(false);
                    else { leftTab.set("sessions"); leftOpen.set(true); }
                  } else {
                    scopeAll.set(false);
                    selectProject(p().id);
                    leftTab.set("sessions");
                    if (isNarrow()) drawer.set("left"); else leftOpen.set(true);
                  }
                  savePrefs();
                }}
                onFocus={() => { railFocus.set(p().id); railTip.set(p().id); }}
                onBlur={() => { if (railFocus() === p().id) railFocus.set(null); if (railTip() === p().id) railTip.set(null); }}
                accessibilityState={{ selected: currentProject() === p().id }}
                accessibilityLabel={`${p().name} · ${details(p().id).join(", ")}`}
                testID={`rail-project-${p().id}`}
                tooltip={<Tooltip placement="right" offset={8} delay={120} open={railTip() === p().id || railFocus() === p().id}
                  onOpenChange={({ open }) => { if (!open) railTip.set(null); else railTip.set(p().id); }}>
                  <View class="gap-1 rounded-(--radius) bg-raised px-3 py-2" testID={`rail-popup-${p().id}`}>
                    <Text class={`${TS.base} font-semibold text-foreground`}>{p().name}</Text>
                    <View class="flex-row gap-3" testID={`rail-popup-${p().id}-above`}>
                      <Text class={`w-[112px] ${TS.base} text-multi tabular-nums`}>{details(p().id)[0]}</Text>
                      <Text class={`w-[64px] ${TS.base} text-info tabular-nums`}>{details(p().id)[1]}</Text>
                    </View>
                    <View class="flex-row gap-3" testID={`rail-popup-${p().id}-below`}>
                      <Text class={`w-[112px] ${TS.base} text-success tabular-nums`}>{details(p().id)[2]}</Text>
                      <Text class={`w-[64px] ${TS.base} text-foreground tabular-nums`}>{details(p().id)[3]}</Text>
                    </View>
                  </View>
                </Tooltip>}
                style={{ width: RAIL_PX }}
                class={`relative items-center rounded-none hover:bg-accent ${currentProject() === p().id ? "bg-active hover:bg-active" : ""} ${dragging() ? "opacity-40" : ""} ${target() ? "bg-accent" : ""} ${FOCUS}`}>
                <If when={currentProject() === p().id}><ActiveBar testID={`rail-active-${p().id}`} /></If>
                <View class="w-full flex-row justify-between px-1">
                  <Each of={CORNERS.slice(0, 2)} key={(c) => c.kind}>{(c) => <Text testID={counts(p().id)[c().at] > 0 ? `rail-count-${p().id}-${c().kind}` : undefined} class={`text-center text-[10px] leading-3 tabular-nums ${tones[c().at]}`}>{counts(p().id)[c().at] > 0 ? railCount(counts(p().id)[c().at]) : " "}</Text>}</Each>
                </View>
                <Text testID={`rail-label-${p().id}`} class={`${TS.base} font-medium ${target() ? "text-primary" : currentProject() === p().id ? "text-highlight" : activity(p().id).active ? (passive() ? "text-foreground opacity-40" : "text-foreground opacity-70") : "text-muted-foreground opacity-60"}`}>{labels().get(p().id)}</Text>
                <View class="w-full flex-row justify-between px-1">
                  <Each of={CORNERS.slice(2)} key={(c) => c.kind}>{(c) => <Text testID={counts(p().id)[c().at] > 0 ? `rail-count-${p().id}-${c().kind}` : undefined} class={`text-center text-[10px] leading-3 tabular-nums ${tones[c().at]}`}>{counts(p().id)[c().at] > 0 ? railCount(counts(p().id)[c().at]) : " "}</Text>}</Each>
                </View>
                <If when={counts(p().id).every((n) => n === 0)}>
                  <View testID={`rail-placeholder-${p().id}`} class="w-3 h-px bg-border opacity-50" />
                </If>
              </View>
                }
              >
                <RowMenuItems row={() => ({ kind: "project" as const, id: p().id })} />
              </ContextMenu>
            );
          }}
        </Each>
        </View>
      </Scroll>
      <View class="w-full items-center pt-2" style={{ paddingBottom: safeArea().bottom + 16 }}>
        <IconButton icon="plus" label="Add project" testID="rail-add-project" onPress={() => run("project.add")} disabled={!availability(byId.get("project.add")!).ok} />
        <IconButton icon="settings" label="Settings" testID="rail-settings" selected={centre() === "settings"} class={centre() === "settings" ? "bg-selection" : ""} onPress={() => run("app.settings")} reason={hint("app.settings") ?? undefined} />
      </View>
    </View>
  );
}

function projectName(id: string) { return model.projects.get(id)?.name ?? "project"; }

const sessionQuery = signal("");
let sessionSearchInput: any;
function dismissSessionSearch() { if (phone) { try { sessionSearchInput?.blur(); } catch {} } }
/** `/search [text]` on a phone: the Sessions list, its search field filled and focused. */
export function focusSessionSearch(query = "") {
  setSessionQuery(query);
  const focus = () => { try { sessionSearchInput?.focus(); } catch { /* the list is not mounted yet */ } };
  focus();
  setTimeout(focus, 0);
  setTimeout(focus, 80);
}
/** Core's search (titles and user messages) for the query typed, when it has answered: the query and the best snippet per session. */
const searchHits = signal<{ q: string; snippets: Map<string, string> } | null>(null);
let searchTimer: ReturnType<typeof setTimeout> | undefined;
let searchSeq = 0;
function setSessionQuery(value: string) {
  sessionQuery.set(value);
  clearTimeout(searchTimer);
  const q = value.trim();
  if (q.length < 2) { searchSeq++; searchHits.set(null); return; }
  const seq = ++searchSeq;
  searchTimer = setTimeout(async () => {
    try {
      const r = await request<{ hits: { session_id: string; snippet: string }[] }>("GET", `/api/search?q=${encodeURIComponent(q)}&limit=30`);
      if (seq === searchSeq) searchHits.set({ q, snippets: new Map(r.hits.map((h) => [h.session_id, h.snippet.replace(/[\[\]]/g, "")])) });
    } catch { /* quiet: the title matches are already on screen */ }
  }, 150);
}
/** The message text that made a session match, when its title did not. */
const snippetOf = (id: string) => { const h = searchHits(); const q = sessionQuery().trim(); return h && h.q === q && !titleMatches(model.sessions.get(id)?.title ?? "") ? h.snippets.get(id) : undefined; };
const titleMatches = (title: string) => title.toLowerCase().includes(sessionQuery().trim().toLowerCase());

type Item = { key: string; kind: "header"; title: string; n: number } | { key: string; kind: "session"; s: Session } | { key: string; kind: "new" };
/** All-projects scope groups sessions under their project; one project is a flat list. */
function rowItems(): Item[] {
  const query = sessionQuery().trim().toLowerCase();
  const hits = searchHits();
  const byMessage = hits && hits.q === sessionQuery().trim() ? hits.snippets : null;
  const ses = rowSessions().filter((s) => !query || s.title.toLowerCase().includes(query) || byMessage?.has(s.id));
  if (phone) {
    return phoneSessionGroups(ses, (s) => ({ waiting: attention(s.id) === "waiting", running: sessionRunning(s) })).flatMap((group) => [
      { key: `phone-${group.title}`, kind: "header" as const, title: group.title, n: group.sessions.length },
      ...group.sessions.map((s) => ({ key: s.id, kind: "session" as const, s })),
    ]);
  }
  if (!scopeAll() || (railShown() && !query)) return ses.map((s) => ({ key: s.id, kind: "session" as const, s }));
  const out: Item[] = [];
  for (const pr of model.projectsRecent()) {
    const mine = ses.filter((s) => s.project_id === pr.id);
    if (!mine.length) continue;
    out.push({ key: `h-${pr.id}`, kind: "header", title: pr.name, n: mine.length });
    for (const s of mine) out.push({ key: s.id, kind: "session", s });
  }
  return out;
}
/** The session list as shown, for the keyboard flow's cursor. */
export function rowSessions(): Session[] {
  m();
  const all = model.sessionsRecent().filter((s) => !s.parent_id);
  const p = currentProject();
  return all.filter((s) => ((scopeAll() && (phone || !railShown() || !!sessionQuery().trim())) || !p || s.project_id === p) && (archivedFilter() || !s.archived));
}
function sessionRunning(s: Session) { return s.status === "busy" || model.tree(s.id).some((id) => model.running.has(id)); }
function childCount(id: string) { let n = 0; for (const s of model.sessions.values()) if (s.parent_id === id) n++; return n; }
export function attention(id: string): "waiting" | "error" | null {
  const tree = model.tree(id);
  for (const a of model.approvals.values()) if (tree.includes(a.session_id)) return "waiting";
  for (const q of model.questions.values()) if (tree.includes(q.session_id)) return "waiting";
  return null;
}

/**
 * Branch labels come from Core's worktree listings, which the store reads once per project. A
 * checkout moves on without any event, so the sidebar asks again (as the TUI does on every
 * snapshot): when Core comes back, when a turn starts or ends, and every 30 s while it is open.
 */
let worktreeTimer: ReturnType<typeof setInterval> | undefined;
let worktreeKey = "";
function refreshWorktrees() {
  if (connection().kind !== "live") return;
  for (const id of Object.keys(projectWorktrees())) loadProjectWorktrees(id, true);
  const open = viewed()?.id;
  if (open) loadSessionWorktrees(open, true);
}
function useWorktreeRefresh() {
  effect(() => {
    const key = `${connection().kind}|${(m(), model.running.size)}|${viewed()?.id ?? ""}`;
    if (key === worktreeKey) return;
    const first = worktreeKey === "";
    worktreeKey = key;
    if (!first) refreshWorktrees();
  });
  if (!worktreeTimer) worktreeTimer = setInterval(refreshWorktrees, 30000);
}

/** The current project (or all projects); pressing it opens the project picker. */
function ArchivedToggle() {
  return <IconButton icon="archive" label="Archived" testID="chip-archived" selected={archivedFilter()} tint={archivedFilter() ? "tint-multi" : "tint-muted-foreground"} onPress={() => run("sessions.archived_filter")} />;
}

export function ProjectSelector() {
  return <Button onPress={() => run("project.switch")} accessibilityLabel="Current project" testID="project-selector" class={`flex-1 min-w-0 flex-row items-center gap-1.5 rounded-(--radius) hover:bg-accent ${phone ? "min-h-11 px-2" : isNarrow() ? "min-h-11 px-2" : "min-h-8 px-2"} ${FOCUS}`}>
    <Text style={{ numberOfLines: 1, ...(phone ? { fontFamily: "Geist" } : {}) }} class={`flex-1 ${phone ? "text-[22px] leading-7 font-semibold text-foreground" : `${TS.base} font-medium text-foreground`}`}>{scopeAll() ? "All projects" : currentProject() ? projectName(currentProject()!) : "No project"}</Text>
    <Icon name="chevron-down" size={14} class="tint-muted-foreground" />
  </Button>;
}

export function NewSessionButton() {
  return <IconButton icon="plus" label="New session" testID="new-session" class={phone ? "bg-raised" : ""} onPress={() => { dismissSessionSearch(); if (phone) { closeDrawer(); phonePage.set("conversation"); } void run("session.new"); }} disabled={!availability(byId.get("session.new")!).ok} reason={availability(byId.get("session.new")!).ok ? hint("session.new") ?? undefined : availability(byId.get("session.new")!).reason} />;
}

/** The phone's session list (Sessions tab, and the conversation's drawer): sectioned, shares the search field and its Core search. */
export function PhoneSessionsPane(p: { variant: "tab" | "drawer" }) {
  useWorktreeRefresh();
  const matches = (s: Session) => {
    const q = sessionQuery().trim().toLowerCase();
    if (!q) return true;
    const hits = searchHits();
    return s.title.toLowerCase().includes(q) || !!(hits && hits.q === sessionQuery().trim() && hits.snippets.has(s.id));
  };
  return <PhoneSessions variant={p.variant} query={sessionQuery} setQuery={setSessionQuery} matches={matches} snippetOf={snippetOf}
    attention={attention} inputRef={(n: any) => { sessionSearchInput = n; }} actions={<NewSessionButton />} />;
}

export function SessionsTab() {
  useWorktreeRefresh();
  if (phone) return <PhoneSessionsPane variant="tab" />;
  return (
    <View class="flex-1 min-h-0">
      <View class={phone ? "flex-row items-center gap-2 px-4 pt-3 pb-2" : "gap-2 pr-2"}>
        <If when={phone}><View class="flex-1 flex-row items-center gap-2 px-3 h-11 rounded-(--radius-card) bg-raised">
          <Icon name="search" size={14} class="tint-muted-foreground" />
          <Input ref={(n: any) => { sessionSearchInput = n; }} value={sessionQuery()} placeholder="Search sessions" accessibilityLabel="Search sessions" testID="session-search" onChange={({ value }) => setSessionQuery(value)} class={`h-full flex-1 min-w-0 rounded-none px-0 ${TS.base} text-foreground placeholder:text-muted-foreground bg-raised`} />
        </View><ArchivedToggle /></If>
        {/* A phone has the project selector in its top bar (PhoneProjectBar). */}
        <If when={!railShown() && !phone}>
        <View class="flex-row items-center gap-2"><ProjectSelector /></View>
        </If>
        <If when={!railShown() && !phone}>
          <View class="flex-row items-center gap-1">
            <Chip label="Project" on={!scopeAll()} onPress={() => scopeAll.set(false)} />
            <Chip label="All projects" on={scopeAll()} onPress={() => scopeAll.set(true)} />
          </View>
        </If>
      </View>
      <View class="flex-1 min-h-0">
      <If when={rowItems().length > 0} else={
        <If when={(m(), model.projects.size === 0)} else={<Empty title={sessionQuery().trim() ? "No matching sessions" : archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button or the New session command."} />}>
          <Empty title="No projects yet" body="Add a project directory to start a session." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
        </If>
      }>
        <List each={rowItems()} keyExtractor={(x) => x.key} class="flex-1" contentStyle={{ paddingLeft: phone ? 16 : 0, paddingRight: phone ? 16 : 8, paddingBottom: 8, gap: 2 }} testID="session-list">
          {(x) => <If when={x().kind === "header"} else={<SessionRow s={() => (x() as any).s as Session} />}><View class={phone ? "px-3 pt-5 pb-2" : "px-2 pt-3 pb-1"} testID="session-group"><Text style={{ numberOfLines: 1, ...(phone ? { fontFamily: "Geist" } : {}) }} class={phone ? "text-[14px] leading-5 font-semibold text-muted-foreground" : `${TS.base} font-medium text-muted-foreground`}>{(x() as any).title}{phone && (x() as any).title === "Needs you" ? ` · ${(x() as any).n}` : ""}</Text></View></If>}
        </List>
      </If>
      </View>
      <If when={!phone}><View class="border-t border-border p-2 shrink-0">
        <Button onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} testID="new-session-row" accessibilityLabel="New session" class={`flex-row items-center gap-2 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Icon name="plus" size={16} class="tint-muted-foreground" /><Text class={`${TS.base} text-muted-foreground`}>New session</Text></Button>
        <Button onPress={() => run("session.switch")} accessibilityLabel="Search sessions" testID="session-search" class={`w-full flex-row items-center gap-2 px-2 h-8 rounded-(--radius) hover:bg-accent ${FOCUS}`}>
          <Icon name="search" size={14} class="tint-muted-foreground" /><Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.base} text-muted-foreground`}>{leftSidebarWidth() < 220 ? "Search" : "Search sessions"}</Text>
          <If when={!!hint("session.switch")}><Text class={T.meta} testID="session-search-key">{hint("session.switch")!}</Text></If>
        </Button>
        <Button onPress={() => run("sessions.archived_filter")} testID="chip-archived" accessibilityLabel="Show archived" selected={archivedFilter()} class={`flex-row items-center gap-2 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Icon name="archive" size={16} class={archivedFilter() ? "tint-multi" : "tint-muted-foreground"} /><Text class={`${TS.base} text-foreground`}>Show archived</Text></Button>
      </View></If>
    </View>
  );
}

function Chip(p: { label: string; on: boolean; onPress: () => void }) {
  return (
    <Button onPress={p.onPress} selected={p.on} accessibilityLabel={p.label} testID={`chip-${p.label.toLowerCase().replace(/ /g, "-")}`} class={`rounded-(--radius) px-2 ${phone ? "min-h-10" : "min-h-6"} justify-center hover:bg-accent ${p.on ? "bg-selection" : ""} ${FOCUS}`}>
      <Text class={`${TS.base} ${p.on ? "text-foreground font-medium" : "text-muted-foreground"}`}>{p.label}</Text>
    </Button>
  );
}

function SessionRow(p: { s: () => Session }) {
  const s = p.s;
  effect(() => { loadProjectWorktrees(s().project_id); });
  // Under a project heading the name is already above the row: show the branch, as the project scope does.
  const place = () => {
    const tree = worktreeForDirectory(s().directory, projectWorktrees()[s().project_id]);
    // The branch where Core reports one, else the linked checkout's folder; all-projects rows
    // sit under a project heading, so the project is never repeated here.
    return tree?.branch ? `⌥ ${tree.branch}` : (tree?.path && tree.main === false ? tree.path.replace(/\/$/, "").split("/").pop() : "") || "";
  };
  const ref = () => ({ kind: "session" as const, id: s().id });
  const sel = () => model.selected?.sessionId === s().id && centre() === "session";
  const n = () => childCount(s().id);
  const att = () => { m(); return attention(s().id); };
  const flowCursor = () => flowStage() === "sessions" && flowSession() === s().id;
  const two = () => !!(place() || snippetOf(s().id) || phone || n() > 0);
  return (
    <ContextMenu
      open={sidebarRowMenu()?.kind === "session" && sidebarRowMenu()?.id === s().id}
      onOpenChange={({ open }) => {
        if (open) {
          focusedSidebarRow.set(ref());
          sidebarRowMenu.set(ref());
          if (!phone) void selectSession(s().id);
        } else if (sidebarRowMenu()?.id === s().id) sidebarRowMenu.set(null);
      }}
      popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg"
      trigger={
        <View role="button"
          onFocus={() => focusedSidebarRow.set(ref())}
          onBlur={() => { if (sidebarRowMenu()?.id !== s().id && focusedSidebarRow()?.id === s().id) focusedSidebarRow.set(null); }}
          onPress={() => { dismissSessionSearch(); focusedSidebarRow.set(ref()); if (phone) drawer.set(null); void selectSession(s().id); }} accessibilityState={{ selected: sel() }} testID={`session-${s().id}`} accessibilityLabel={`${s().title || "Untitled"}, ${att() ? "needs your input" : sessionRunning(s()) ? "running" : "idle"}`}
          class={`group/row relative px-2 hover:bg-accent ${phone ? "rounded-(--radius-card)" : "rounded-none"} ${sel() ? "bg-active hover:bg-active" : flowCursor() ? "bg-selection" : ""} ${phone ? "min-h-[76px] px-3 py-3 pr-14" : isNarrow() ? (two() ? "min-h-[64px] py-2" : "min-h-11 justify-center") : (two() ? "h-16 py-2" : "h-8 justify-center")} ${FOCUS}`}
        >
          <If when={sel() && !phone}><ActiveBar testID={`session-active-${s().id}`} /></If>
          <If when={flowCursor()}><FocusEdge testID={`session-cursor-${s().id}`} /></If>
          <View class="flex-row items-center gap-2 h-6">
            <If when={!phone}><View class="w-4 items-center justify-center">
              <If when={att()}><AttentionMark kind="waiting" /></If>
              <If when={!att() && s().status === "busy"}><Status kind="running" /></If>
              <If when={!att() && s().status !== "busy"}><Icon name="session" size={14} class="tint-muted-foreground" /></If>
            </View></If>
            <If when={phone && !!att()}><AttentionMark kind="waiting" /></If>
            <If when={phone && !att() && sessionRunning(s())}><Status kind="running" /></If>
            <Text style={{ numberOfLines: 1, ...(phone ? { fontFamily: "Geist" } : {}) }} class={`flex-1 min-w-0 ${phone ? "text-[16px] leading-6 font-medium" : TS.base} ${sel() ? "text-highlight" : "text-foreground"}`}>{s().title || "Untitled"}</Text>
            <If when={!phone}>
              <View class="flex-row items-center gap-1">
                <Text class={`${T.meta} ${touch ? "opacity-0" : "group-hover/row:opacity-0"}`}>{relTime(s().updated, (tick(), Date.now()))}</Text>
              </View>
            </If>
          </View>
          <If when={two()}>
            <View class={`flex-row items-center gap-1 h-6 ${phone ? "" : "pl-6"}`} testID={`session-line-${s().id}`}>
              <If when={phone}><Text style={{ numberOfLines: 1, fontFamily: "Geist" }} class={`text-[13px] leading-5 min-w-0 ${att() ? "text-warning" : "text-muted-foreground"}`}>{att() ? "Needs your input" : projectName(s().project_id)}</Text></If>
              <If when={!phone && place()}><Text style={{ numberOfLines: 1 }} testID={`session-place-${s().id}`} class={`${T.meta} min-w-0`}>{place()}</Text></If>
              <If when={phone}><Text style={{ fontFamily: "Geist" }} class="text-[13px] leading-5 text-muted-foreground">{` · ${relTime(s().updated, (tick(), Date.now()))}`}</Text></If>
              <If when={snippetOf(s().id)}>{(t) => <Text style={{ numberOfLines: 1 }} testID={`session-snippet-${s().id}`} class={`${T.meta} flex-1 min-w-0`}>{`· ${t()}`}</Text>}</If>
              <View class="flex-1" />
              <If when={!phone && n() > 0}><Text class={T.meta}>{`${n()} ↳`}</Text></If>
            </View>
          </If>
          <Button onPress={() => { focusedSidebarRow.set(ref()); sidebarRowMenu.set(ref()); if (!phone) void selectSession(s().id); }}
            accessibilityLabel={`Actions for ${s().title || "Untitled"}`} testID={`row-menu-${s().id}`}
            class={`absolute right-1.5 ${phone ? "top-3 size-11" : touch ? "top-1.5 size-11 bg-sidebar" : "top-1.5 size-6 opacity-0 group-hover/row:opacity-100 focus:opacity-100"} items-center justify-center rounded-(--radius) hover:bg-hover ${FOCUS}`}>
            <Icon name="more" size={16} class="tint-foreground-secondary" />
          </Button>
        </View>
      }
    >
      <RowMenuItems row={ref} />
    </ContextMenu>
  );
}
/** The active sidebar block's left bar (`interactive.active_bar`), drawn over the row so it never shifts layout. */
function ActiveBar(p: { testID: string }) {
  return <View testID={p.testID} accessible={false} class="absolute left-0 top-0 bottom-0 w-0.5 bg-active-bar" style={{ pointerEvents: "none" }} />;
}
function RowMenuItems(p: { row: () => SidebarRowTarget }) {
  const items = () => commands.filter((c) => c.placement.includes("row_menu") && c.id.startsWith(`${p.row().kind}.`) && (!phone || !desktopOnly(c.id)));
  const choose = (id: string) => {
    const row = p.row();
    sidebarRowMenu.set(null);
    if (row.kind === "session") void selectSession(row.id).then(() => run(id));
    else { selectProject(row.id); void run(id); }
  };
  return (
    <Each of={items()} key={(c) => c.id}>
      {(cmd) => {
        const c = cmd();
        const state = () => availability(c);
        const danger = c.id === "project.remove" || c.id === "session.delete";
        return <MenuItem onSelect={() => choose(c.id)} disabled={!state().ok}
          accessibilityLabel={`${c.title}${state().ok ? "" : `, unavailable: ${state().reason}`}`}
          testID={`row-action-${c.id}`}
          class="flex-col items-stretch gap-0 rounded-(--radius) px-2 py-1.5 hover:bg-accent disabled:opacity-50">
          <View class="flex-row items-center gap-2">
            <Text class={`flex-1 ${TS.base} ${danger ? "text-error" : "text-popover-foreground"}`}>{c.title}</Text>
            <Text class={T.meta}>{keyLabel(c) ?? ""}</Text>
          </View>
          <If when={!state().ok}><Text class={T.meta}>{state().reason!}</Text></If>
        </MenuItem>;
      }}
    </Each>
  );
}

const GIT_TONE: Record<string, string> = { M: "text-warning", A: "text-success", D: "text-error", "?": "text-muted-foreground" };
function FilesTab() {
  effect(() => { const p = currentProject(); if (p && fileTree().project !== p) void loadFiles(); });
  const rows = () => fileRows(fileTree().dirs, filesExpanded());
  const root = () => fileTree().dirs[""];
  const isOpen = (path: string) => { m(); return openFile()?.path === path; };
  const branch = () => currentWorktree()?.branch ?? "";
  return <Scroll class="flex-1">
    <If when={root() || fileTree().project} else={<Text class={`${T.sec} p-4`}>Loading files…</Text>}>
      {() => <View class="p-2">
        <View class="flex-row items-center gap-2 px-2 pb-2" testID="files-header">
          <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.base} font-medium text-foreground`}>{currentProject() ? projectName(currentProject()!) : "Files"}</Text>
          <If when={!!branch()}><Text style={{ numberOfLines: 1 }} class={T.meta}>{`⌥ ${branch()}`}</Text></If>
        </View>
        <If when={fileWatchStatus() === "polling"}><Text class={`${T.meta} px-2`} testID="files-watch-status">Polling every 3 seconds</Text></If>
        <If when={fileWatchStatus() === "disconnected"}><Text class={`${T.meta} px-2`} testID="files-watch-status">File watching disconnected</Text></If>
        <If when={root()?.error}><Text class={`${T.sec} px-2`}>{root()!.error!}</Text></If>
        <If when={!root()?.error && rows().length === 0}><Text class={`${T.sec} px-2 pt-1`} testID="files-empty">This folder is empty.</Text></If>
        <Each of={rows()} key={(row) => row.path}>{(row) =>
          <View style={{ paddingLeft: 8 + row().depth * 16 }} onFocus={() => selectedFileEntry.set({ path: row().path, kind: row().kind })}>
          <ContextMenu onOpenChange={({ open }) => { if (open) selectedFileEntry.set({ path: row().path, kind: row().kind }); }} popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg" trigger={<Button onPress={() => { const r = row(); selectedFileEntry.set({ path: r.path, kind: r.kind }); if (r.kind === "directory") toggleFileDir(r.path); else if (r.kind === "file") void showFile(r.path); }}
            accessibilityLabel={row().name} testID={`file-entry-${row().path}`}
            class={`flex-row items-center gap-1.5 h-7 rounded-(--radius) pr-2 hover:bg-accent ${isOpen(row().path) ? "bg-selection" : ""} ${FOCUS}`}>
            <View class="w-3.5 shrink-0 items-center justify-center">
              <If when={row().kind === "directory"}><Text class={`${TS.base} text-muted-foreground`}>{row().expanded ? fold.open.unicode : fold.closed.unicode}</Text></If>
              <If when={row().kind === "error"}><Text class={`${TS.base} text-error`}>✕</Text></If>
            </View>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${row().kind === "directory" ? `${TS.base} font-medium text-foreground` : row().ignored ? `${TS.base} text-faint` : `${TS.base} text-foreground`}`}>{row().name}</Text>
            <View class="w-4 shrink-0 items-center justify-end">
              <If when={!!row().git_status && row().kind !== "directory"}><Text class={`${TS.base} font-medium ${GIT_TONE[(row().git_status ?? "").trim().slice(-1)] ?? "text-muted-foreground"}`}>{(row().git_status ?? "").trim().slice(-1)}</Text></If>
            </View>
          </Button>}>
            <Each of={commands.filter((c) => c.id.startsWith("files.") && c.placement.includes("row_menu"))} key={(c) => c.id}>{(cmd) => <MenuItem onSelect={() => void run(cmd().id)} disabled={!availability(cmd()).ok} class="rounded-(--radius) px-2 py-1.5 hover:bg-accent"><Text class={TS.base}>{cmd().title}</Text></MenuItem>}</Each>
          </ContextMenu>
          </View>
        }</Each>
      </View>}
    </If>
  </Scroll>;
}
function ProjectsTab() {
  const rows = () => { m(); return orderedProjects(model.projects.values()); };
  const items = () => [...rows().map((project) => ({ kind: "project" as const, id: project.id, project })), { kind: "add" as const, id: "add-project" }];
  const count = (id: string) => { let n = 0; for (const s of model.sessions.values()) if (s.project_id === id && !s.parent_id && !s.archived) n++; return n; };
  return (
    <View class="flex-1 min-h-0">
      <View class="flex-row items-center gap-2 p-2">
        <Text class={`flex-1 ${TS.base} font-medium text-foreground px-2`}>Projects</Text>
      </View>
      <If when={rows().length === 0}><Empty title="No projects yet" body="Add a project by its directory path. Core creates it if it is new." /></If>
      <List each={items()} keyExtractor={(x) => x.id} class="flex-1" contentStyle={{ paddingHorizontal: 8, paddingBottom: 8, gap: 2 }} testID="project-list">
        {(item) => {
          const row = item();
          if (row.kind === "add") return <Button onPress={() => run("project.add")} disabled={!availability(byId.get("project.add")!).ok} testID="add-project" accessibilityLabel="+ Add project"
            class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 min-h-8 hover:bg-accent ${FOCUS}`}>
            <Text class={`${TS.base} text-primary`}>+</Text>
            <Text class={`${TS.base} text-foreground`}>Add project</Text>
          </Button>;
          return <ProjectRow project={() => row.project} count={count} />;
        }}
      </List>
      <ProjectMcpPanel />
    </View>
  );
}

function ProjectRow(p: { project: () => Project; count: (id: string) => number }) {
  const project = p.project;
  const home = () => {
    const stateHome = info()?.home ?? "";
    return userHome() || (/\/\.nexus(10)?$/.test(stateHome) ? stateHome.replace(/\/\.nexus(10)?$/, "") : "");
  };
  const short = () => shortPath(project().path, home());
  const ref = () => ({ kind: "project" as const, id: project().id });
  const selected = () => currentProject() === project().id;
  const cursor = () => flowStage() === "projects" && flowProject() === project().id;
  return (
    <ContextMenu
      open={sidebarRowMenu()?.kind === "project" && sidebarRowMenu()?.id === project().id}
      onOpenChange={({ open }) => {
        if (open) {
          focusedSidebarRow.set(ref());
          sidebarRowMenu.set(ref());
          selectProject(project().id);
        } else if (sidebarRowMenu()?.id === project().id) sidebarRowMenu.set(null);
      }}
      popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg"
      trigger={
        <View role="button" onFocus={() => focusedSidebarRow.set(ref())}
          onBlur={() => { if (sidebarRowMenu()?.id !== project().id && focusedSidebarRow()?.id === project().id) focusedSidebarRow.set(null); }}
          onPress={() => { focusedSidebarRow.set(ref()); if (projectStatus(project()) === PROJECT_STATUS.inactive) void setProjectStatus(project().id, PROJECT_STATUS.current); selectProject(project().id); }} accessibilityState={{ selected: selected() }} testID={`project-${project().id}`} accessibilityLabel={`Project ${project().name}`}
          class={`relative gap-0.5 rounded-none px-2.5 py-1.5 hover:bg-accent ${selected() || cursor() ? "bg-selection" : ""} ${FOCUS}`}>
          <If when={cursor()}><FocusEdge testID={`project-cursor-${project().id}`} /></If>
          <View class="flex-row items-center gap-2">
            <Text style={{ numberOfLines: 1 }} class={`flex-1 ${TS.base} ${selected() ? "text-foreground font-medium" : "text-foreground"}`}>{project().name}</Text>
            <Text class={T.meta}>{`${projectStatus(project()) === PROJECT_STATUS.current ? "" : `${projectStatus(project())} · `}${p.count(project().id)} session${p.count(project().id) === 1 ? "" : "s"} · ${relTime(project().updated)}`}</Text>
          </View>
          <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`${T.mono} w-full min-w-0`}>{short()}</Text>
        </View>
      }
    >
      <RowMenuItems row={ref} />
    </ContextMenu>
  );
}
void Input; void Scroll; void abbrev; void icons; void fold; void newSession; void toggleLeft; void connection; void live; void viewed;
