import type { JSX } from "nsh";
import { List, PanResponder, Scroll, Text, View, effect, safeArea, signal } from "nsh";
import { Button, ContextMenu, MenuItem, Tab, Tabs, Input, Tooltip } from "nsh/base";
import { Btn, Empty, Icon, IconButton, Status, AttentionMark, T, TS, FOCUS, relTime, abbrev, midTruncate, shortPath } from "./kit";
import { manifest } from "../gen/manifest";
import { availability, byId, commands, keyLabel, newSession, run, toggleLeft } from "../commands";
import { archivedFilter, centre, connection, currentProject, drawer, leftOpen, railShown, focusedSidebarRow, info, isNarrow, leftTab, m, model, scopeAll, selectProject, selectSession, sidebarRowMenu, tick, live, touch, userHome, viewed, savePrefs, SidebarRowTarget, loadProjectWorktrees, loadSessionWorktrees, projectWorktrees, worktreeForDirectory, currentWorktree, phone, phonePage, width } from "../store";
import { Project, Session } from "../core/api";
import { request } from "../core/transport";
import { fileTree, filesExpanded, loadFiles, openFile, showFile, toggleFileDir } from "../store";
import { fileRows } from "../file-tree";
import { icons, IconName } from "../icons";
import { fold } from "../gen/theme";
import { ProjectMcpPanel } from "./project-mcp";
import { projectActivity, projectCounts, projectLabels, railCount, railProjects } from "../project-rail";
import { defaultLeftSidebarWidth } from "./geometry";

export const leftSidebarWidth = signal(272);
const sidebarResized = signal(false);
effect(() => { if (!sidebarResized()) leftSidebarWidth.set(defaultLeftSidebarWidth(width())); });
/** The project rail's width. */
export const RAIL_PX = 40;
const tabs = manifest.navigation.left_sidebar.tab as unknown as { id: "sessions" | "files" | "projects"; label: string; command: string; order: number }[];

function hint(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * The left sidebar's switch icons (Sessions, Files, Projects). Docked, they live in the
 * header's left slot over the sidebar; in a narrow drawer they sit in the drawer's own top row
 * with labels.
 */
export function SidebarTabs(p: { labels?: boolean }) {
  return (
    <Tabs class="flex-row items-center gap-0.5">
      <Each of={[...tabs].sort((a, b) => a.order - b.order)} key={(t) => t.id}>
        {(t) => (
          <Tab
            selected={leftTab() === t().id} accessibilityLabel={t().label} testID={`tab-${t().id}`} disabled={false}
            onPress={() => { leftTab.set(t().id); if (t().id === "files") void loadFiles(); savePrefs(); }}
            tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{t().label}{hint(t().command) ? `  ${hint(t().command)}` : ""}</Text>}
            class={`flex-row items-center justify-center rounded-(--radius) px-1 hover:bg-accent ${leftTab() === t().id ? "bg-background" : ""} ${isNarrow() ? "min-h-11" : "min-h-6"} ${FOCUS}`}
          >
            <Text class={`${TS.base} ${leftTab() === t().id ? "text-foreground font-semibold" : "text-muted-foreground"}`}>{`[${t().label}]`}</Text>
          </Tab>
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
        <If when={leftTab() === "projects"}><ProjectsTab /></If>
      </View>
      <View class="border-t border-border p-2" style={{ paddingBottom: safeArea().bottom + 8 }}>
        <Button
          onPress={() => run("app.settings")} testID="open-settings" accessibilityLabel="Settings" selected={centre() === "settings"}
          class={`flex-row items-center gap-2 rounded-(--radius) px-2 hover:bg-accent ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}
        >
          <Text class={`flex-1 ${TS.base} text-foreground`}>Settings</Text>
          <Text class={T.meta}>{hint("app.settings") ?? ""}</Text>
        </Button>
      </View>
      <If when={!p.drawer}><View {...resize.panHandlers} class="absolute right-0 top-0 bottom-0 w-2" testID="left-sidebar-resize" accessibilityLabel="Resize left sidebar" /></If>
    </View>
  );
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
            return (
              <ContextMenu
                open={railMenu() === p().id}
                onOpenChange={({ open }) => { railMenu.set(open ? p().id : railMenu() === p().id ? null : railMenu()); }}
                popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg"
                trigger={
              <View role="button" focusable
                onPress={() => {
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
                class={`relative items-center rounded-none hover:bg-accent ${currentProject() === p().id ? "bg-active hover:bg-active" : ""} ${FOCUS}`}>
                <If when={currentProject() === p().id}><ActiveBar testID={`rail-active-${p().id}`} /></If>
                <View class="w-full flex-row justify-between px-1">
                  <Each of={CORNERS.slice(0, 2)} key={(c) => c.kind}>{(c) => <Text testID={counts(p().id)[c().at] > 0 ? `rail-count-${p().id}-${c().kind}` : undefined} class={`text-center text-[10px] leading-3 tabular-nums ${tones[c().at]}`}>{counts(p().id)[c().at] > 0 ? railCount(counts(p().id)[c().at]) : " "}</Text>}</Each>
                </View>
                <Text class={`${TS.base} font-medium ${currentProject() === p().id ? "text-highlight" : activity(p().id).active ? "text-foreground opacity-70" : "text-muted-foreground opacity-30"}`}>{labels().get(p().id)}</Text>
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
        <IconButton icon="plus" label="Add project" testID="rail-add-project" onPress={() => run("project.add")} disabled={!availability(byId.get("project.add")!).ok} />
        </View>
      </Scroll>
      <View class="w-full items-center pt-2" style={{ paddingBottom: safeArea().bottom + 16 }}>
        <If when={!leftOpen()}><IconButton icon="settings" label="Settings" testID="rail-settings" selected={centre() === "settings"} class={centre() === "settings" ? "bg-selection" : ""} onPress={() => run("app.settings")} reason={hint("app.settings") ?? undefined} /></If>
      </View>
    </View>
  );
}

function projectName(id: string) { return model.projects.get(id)?.name ?? "project"; }

const sessionQuery = signal("");
let sessionSearchInput: any;
function dismissSessionSearch() { if (phone) { try { sessionSearchInput?.blur(); } catch {} } }
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
  if (!scopeAll() || (railShown() && !query)) return [...ses.map((s) => ({ key: s.id, kind: "session" as const, s })), ...(railShown() && !query ? [{ key: "new-session-row", kind: "new" as const }] : [])];
  const out: Item[] = [];
  for (const pr of model.projectsRecent()) {
    const mine = ses.filter((s) => s.project_id === pr.id);
    if (!mine.length) continue;
    out.push({ key: `h-${pr.id}`, kind: "header", title: pr.name, n: mine.length });
    for (const s of mine) out.push({ key: s.id, kind: "session", s });
  }
  return out;
}
function rowSessions(): Session[] {
  m();
  const all = model.sessionsRecent().filter((s) => !s.parent_id);
  const p = currentProject();
  return all.filter((s) => ((scopeAll() && (!railShown() || !!sessionQuery().trim())) || !p || s.project_id === p) && (archivedFilter() || !s.archived));
}
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

export function SessionsTab() {
  useWorktreeRefresh();
  return (
    <View class="flex-1 min-h-0">
      <View class={phone ? "gap-3 px-4 pt-4 pb-3" : "gap-2 py-2 pr-2"}>
        <View class="flex-row justify-end">
          <IconButton icon="archive" label="Archived" testID="chip-archived" selected={archivedFilter()} tint={archivedFilter() ? "tint-multi" : "tint-muted-foreground"} onPress={() => run("sessions.archived_filter")} />
        </View>
        <View class={`w-full flex-row items-center gap-2 px-2 ${phone ? "h-11 bg-raised" : "h-8 bg-hover"}`}>
          <Icon name="search" size={14} class="tint-muted-foreground" />
          <Input ref={(n: any) => { sessionSearchInput = n; }} value={sessionQuery()} placeholder={leftSidebarWidth() < 220 ? "Search" : "Search sessions"} accessibilityLabel="Search sessions" testID="session-search" onChange={({ value }) => setSessionQuery(value)} class={`h-full flex-1 min-w-0 rounded-none px-0 ${TS.base} text-foreground placeholder:text-muted-foreground ${phone ? "bg-raised" : "bg-hover"}`} />
          <If when={!!hint("session.switch") && !phone}>
            <View class="shrink-0 items-center justify-center" style={{ pointerEvents: "none" }} accessible={false}>
              <Text class={T.meta} testID="session-search-key">{hint("session.switch")!}</Text>
            </View>
          </If>
        </View>
        <If when={!railShown()}>
        <View class="flex-row items-center gap-2">
          <Button onPress={() => run("project.switch")} accessibilityLabel="Current project" class={`flex-1 min-w-0 flex-row items-center gap-1.5 rounded-(--radius) hover:bg-accent ${phone ? "min-h-11 bg-raised px-4" : isNarrow() ? "min-h-11 px-2" : "min-h-8 px-2"} ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 ${TS.base} font-medium text-foreground`}>{scopeAll() ? "All projects" : currentProject() ? projectName(currentProject()!) : "No project"}</Text>
            <Icon name="chevron-down" size={14} class="tint-muted-foreground" />
          </Button>
          <IconButton icon="plus" label="New session" testID="new-session" class={phone ? "bg-raised" : ""} onPress={() => { dismissSessionSearch(); if (phone) phonePage.set("conversation"); void run("session.new"); }} disabled={!availability(byId.get("session.new")!).ok} reason={availability(byId.get("session.new")!).ok ? hint("session.new") ?? undefined : availability(byId.get("session.new")!).reason} />
        </View>
        </If>
        <If when={!railShown()}>
          <View class="flex-row items-center gap-1">
            <Chip label="Project" on={!scopeAll()} onPress={() => scopeAll.set(false)} />
            <Chip label="All projects" on={scopeAll()} onPress={() => scopeAll.set(true)} />
          </View>
        </If>
      </View>
      <If when={rowItems().length > 0} else={
        <If when={(m(), model.projects.size === 0)} else={<Empty title={sessionQuery().trim() ? "No matching sessions" : archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button or the New session command."} />}>
          <Empty title="No projects yet" body="Add a project directory to start a session." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
        </If>
      }>
        <List each={rowItems()} keyExtractor={(x) => x.key} class="flex-1" contentStyle={{ paddingLeft: phone ? 16 : 0, paddingRight: phone ? 16 : 8, paddingBottom: 8, gap: 2 }} testID="session-list">
          {(x) => (
            <If when={x().kind === "new"} else={
              <If when={x().kind === "header"} else={<SessionRow s={() => (x() as any).s as Session} />}>
                <View class="px-2 pt-3 pb-1" testID="session-group">
                  <Text style={{ numberOfLines: 1 }} class={`${TS.base} font-medium text-muted-foreground`}>{(x() as any).title}</Text>
                </View>
              </If>
            }>
              <Button onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} testID="new-session-row" accessibilityLabel="New session"
                class={`flex-row items-center gap-2 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
                <View class="w-4 items-center justify-center"><Text class={`${TS.base} text-muted-foreground`}>+</Text></View>
                <Text class={`${TS.base} text-muted-foreground`}>New session</Text>
              </Button>
            </If>
          )}
        </List>
      </If>
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
          onPress={() => { dismissSessionSearch(); focusedSidebarRow.set(ref()); void selectSession(s().id); }} accessibilityState={{ selected: sel() }} testID={`session-${s().id}`} accessibilityLabel={`${s().title || "Untitled"}, ${s().status === "busy" ? "running" : "idle"}`}
          class={`group/row relative rounded-none px-2 hover:bg-accent ${sel() ? "bg-active hover:bg-active" : ""} ${phone ? "min-h-[72px] py-3 pr-14" : isNarrow() ? (two() ? "min-h-[64px] py-2" : "min-h-11 justify-center") : (two() ? "h-16 py-2" : "h-8 justify-center")} ${FOCUS}`}
        >
          <If when={sel()}><ActiveBar testID={`session-active-${s().id}`} /></If>
          <View class="flex-row items-center gap-2 h-6">
            <If when={!phone}><View class="w-4 items-center justify-center">
              <If when={att()}><AttentionMark kind="waiting" /></If>
              <If when={!att() && s().status === "busy"}><Status kind="running" /></If>
            </View></If>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.base} ${sel() ? "text-highlight" : "text-foreground"}`}>{s().title || "Untitled"}</Text>
            <If when={!phone}>
              <View class="flex-row items-center gap-1">
                <Text class={`${T.meta} ${touch ? "opacity-0" : "group-hover/row:opacity-0"}`}>{relTime(s().updated, (tick(), Date.now()))}</Text>
              </View>
            </If>
          </View>
          <If when={two()}>
            <View class={`flex-row items-center gap-1 h-6 ${phone ? "" : "pl-6"}`} testID={`session-line-${s().id}`}>
              <If when={place()}><Text style={{ numberOfLines: 1 }} testID={`session-place-${s().id}`} class={`${T.meta} min-w-0`}>{place()}</Text></If>
              <If when={phone}><Text class={T.meta}>{` · ${relTime(s().updated, (tick(), Date.now()))}`}</Text></If>
              <If when={snippetOf(s().id)}>{(t) => <Text style={{ numberOfLines: 1 }} testID={`session-snippet-${s().id}`} class={`${T.meta} flex-1 min-w-0`}>{`· ${t()}`}</Text>}</If>
              <View class="flex-1" />
              <If when={n() > 0}><Text class={T.meta}>{`${n()} ↳`}</Text></If>
            </View>
          </If>
          <If when={phone}><View class="absolute left-2 right-2 bottom-0 h-px bg-border" testID={`session-divider-${s().id}`} /></If>
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
  const items = () => commands.filter((c) => c.placement.includes("row_menu") && c.id.startsWith(`${p.row().kind}.`));
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
        <If when={root()?.error}><Text class={`${T.sec} px-2`}>{root()!.error!}</Text></If>
        <If when={!root()?.error && rows().length === 0}><Text class={`${T.sec} px-2 pt-1`} testID="files-empty">This folder is empty.</Text></If>
        <Each of={rows()} key={(row) => row.path}>{(row) =>
          <View style={{ paddingLeft: 8 + row().depth * 16 }}>
          <Button onPress={() => { const r = row(); if (r.kind === "directory") toggleFileDir(r.path); else if (r.kind === "file") void showFile(r.path); }}
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
          </Button>
          </View>
        }</Each>
      </View>}
    </If>
  </Scroll>;
}
function ProjectsTab() {
  const rows = () => { m(); return model.projectsRecent(); };
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
          onPress={() => { focusedSidebarRow.set(ref()); selectProject(project().id); }} accessibilityState={{ selected: selected() }} testID={`project-${project().id}`} accessibilityLabel={`Project ${project().name}`}
          class={`gap-0.5 rounded-none px-2.5 py-1.5 hover:bg-accent ${selected() ? "bg-selection" : ""} ${FOCUS}`}>
          <View class="flex-row items-center gap-2">
            <Text style={{ numberOfLines: 1 }} class={`flex-1 ${TS.base} ${selected() ? "text-foreground font-medium" : "text-foreground"}`}>{project().name}</Text>
            <Text class={T.meta}>{`${p.count(project().id)} session${p.count(project().id) === 1 ? "" : "s"} · ${relTime(project().updated)}`}</Text>
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
