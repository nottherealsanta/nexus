import type { JSX } from "nsh";
import { List, PanResponder, Scroll, Text, View, effect, safeArea, signal } from "nsh";
import { Button, ContextMenu, MenuItem, Tab, Tabs, Input, Tooltip } from "nsh/base";
import { Btn, Empty, Icon, IconButton, Status, AttentionMark, T, FOCUS, relTime, abbrev, midTruncate, shortPath } from "./kit";
import { manifest } from "../gen/manifest";
import { availability, byId, commands, keyLabel, newSession, run, toggleLeft } from "../commands";
import { archivedFilter, centre, connection, currentProject, focusedSidebarRow, info, isNarrow, leftTab, m, model, scopeAll, selectProject, selectSession, sidebarRowMenu, tick, live, touch, userHome, viewed, savePrefs, SidebarRowTarget, loadProjectWorktrees, loadSessionWorktrees, projectWorktrees, worktreeForDirectory, phone, phonePage } from "../store";
import { Project, Session } from "../core/api";
import { request } from "../core/transport";
import { fileListing, loadFiles, openFile, showFile } from "../store";
import { icons, IconName } from "../icons";
import { fold } from "../gen/theme";
import { ProjectMcpPanel } from "./project-mcp";
import { projectActivity, projectCounts, projectLabels, railCount, railProjects } from "../project-rail";

const TAB_ICON: Record<string, IconName> = { sessions: "sessions", files: "files", projects: "projects" };
export const leftSidebarWidth = signal(264);
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
            tooltip={<Text class="text-xs text-popover-foreground bg-popover rounded-(--radius) px-2 py-1">{t().label}{hint(t().command) ? `  ${hint(t().command)}` : ""}</Text>}
            class={`flex-row items-center justify-center gap-1.5 rounded-(--radius) hover:bg-accent ${leftTab() === t().id ? "bg-selection" : ""} ${p.labels ? "px-2.5 min-h-11" : isNarrow() ? "size-11" : "size-8"} ${FOCUS}`}
          >
            <Icon name={TAB_ICON[t().id]} class={leftTab() === t().id ? "tint-foreground" : "tint-muted-foreground"} />
            <If when={p.labels && leftTab() === t().id}><Text class="text-[13px] leading-5 text-foreground font-medium">{t().label}</Text></If>
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
    onPanResponderGrant: () => { startWidth = leftSidebarWidth(); },
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
          <Icon name="settings" />
          <Text class="flex-1 text-[13px] leading-5 text-foreground">Settings</Text>
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
  // The rail popup: shorter wording than the TUI, two rows of two, in aligned columns.
  const details = (id: string) => {
    const c = counts(id);
    return [`${c[0]} attention`, `${c[1]} unread`, `${c[2]} running`, `${c[3]} idle`];
  };
  const railTip = signal<string | null>(null);
  const railFocus = signal<string | null>(null);
  const tones = ["text-multi", "text-info", "text-success", "text-foreground"];
  const kinds = ["attention", "finished", "running", "idle"];
  return (
    <View class="w-12 shrink-0 bg-sidebar border-r border-border items-center pt-2" testID="session-rail" accessibilityLabel="Project rail">
      <IconButton icon="search" label="Search sessions" testID="rail-search" onPress={() => { leftTab.set("sessions"); toggleLeft(); }} />
      <IconButton icon="plus" label="New session" testID="rail-new-session" onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} />
      <Scroll class="flex-1">
        <View class="items-center gap-2 pb-2">
        <Each of={projects()} key={(p) => p.id}>
          {(p) => {
            const shown = () => counts(p().id).map((n, i) => ({ n, tone: tones[i], kind: kinds[i] }));
            // Attention and finished sit above the initials, running and idle below.
            const half = (first: number) => shown().slice(first, first + 2).filter((c) => c.n > 0);
            return (
              <View role="button" focusable
                onPress={() => { scopeAll.set(false); selectProject(p().id); }}
                onFocus={() => { railFocus.set(p().id); railTip.set(p().id); }}
                onBlur={() => { if (railFocus() === p().id) railFocus.set(null); if (railTip() === p().id) railTip.set(null); }}
                accessibilityState={{ selected: currentProject() === p().id }}
                accessibilityLabel={`${p().name} · ${details(p().id).join(", ")}`}
                testID={`rail-project-${p().id}`}
                tooltip={<Tooltip placement="right" offset={8} delay={120} open={railTip() === p().id || railFocus() === p().id}
                  onOpenChange={({ open }) => { if (!open) railTip.set(null); else railTip.set(p().id); }}>
                  <View class="gap-1 rounded-(--radius) bg-raised px-3 py-2" testID={`rail-popup-${p().id}`}>
                    <Text class="text-[13px] leading-5 font-semibold text-foreground">{p().name}</Text>
                    <View class="flex-row gap-3" testID={`rail-popup-${p().id}-above`}>
                      <Text class="w-[84px] text-xs leading-4 text-multi tabular-nums">{details(p().id)[0]}</Text>
                      <Text class="w-[64px] text-xs leading-4 text-info tabular-nums">{details(p().id)[1]}</Text>
                    </View>
                    <View class="flex-row gap-3" testID={`rail-popup-${p().id}-below`}>
                      <Text class="w-[84px] text-xs leading-4 text-success tabular-nums">{details(p().id)[2]}</Text>
                      <Text class="w-[64px] text-xs leading-4 text-foreground tabular-nums">{details(p().id)[3]}</Text>
                    </View>
                  </View>
                </Tooltip>}
                class={`relative size-10 flex-col gap-0 items-center justify-center rounded-(--radius) hover:bg-accent ${currentProject() === p().id ? "bg-selection" : ""} ${FOCUS}`}>
                <View testID={`rail-counts-${p().id}-above`} class="flex-row items-center gap-px h-3">
                  <Each of={half(0)} key={(c) => c.kind}>
                    {(c) => <Text testID={`rail-count-${p().id}-${c().kind}`} class={`text-[10px] leading-3 tabular-nums ${c().tone}`}>{railCount(c().n)}</Text>}
                  </Each>
                </View>
                <Text class={`text-[13px] leading-4 font-medium ${activity(p().id).active ? "text-foreground opacity-70" : "text-muted-foreground opacity-50"}`}>{labels().get(p().id)}</Text>
                <View testID={`rail-counts-${p().id}-below`} class="flex-row items-center gap-px h-3">
                  <Each of={half(2)} key={(c) => c.kind}>
                    {(c) => <Text testID={`rail-count-${p().id}-${c().kind}`} class={`text-[10px] leading-3 tabular-nums ${c().tone}`}>{railCount(c().n)}</Text>}
                  </Each>
                  <If when={half(0).length === 0 && half(2).length === 0}>
                    <View testID={`rail-placeholder-${p().id}`} class="w-3 h-px bg-muted-foreground opacity-50" />
                  </If>
                </View>
              </View>
            );
          }}
        </Each>
        </View>
      </Scroll>
      <View class="border-t border-border w-full items-center pt-2" style={{ paddingBottom: safeArea().bottom + 8 }}>
        <IconButton icon="settings" label="Settings" testID="rail-settings" selected={centre() === "settings"} class={centre() === "settings" ? "bg-selection" : ""} onPress={() => run("app.settings")} reason={hint("app.settings") ?? undefined} />
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

type Item = { key: string; kind: "header"; title: string; n: number } | { key: string; kind: "session"; s: Session };
/** All-projects scope groups sessions under their project; one project is a flat list. */
function rowItems(): Item[] {
  const query = sessionQuery().trim().toLowerCase();
  const hits = searchHits();
  const byMessage = hits && hits.q === sessionQuery().trim() ? hits.snippets : null;
  const ses = rowSessions().filter((s) => !query || s.title.toLowerCase().includes(query) || byMessage?.has(s.id));
  if (!scopeAll()) return ses.map((s) => ({ key: s.id, kind: "session" as const, s }));
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
  return all.filter((s) => (scopeAll() || !p || s.project_id === p) && (archivedFilter() ? s.archived : !s.archived));
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
      <View class={phone ? "gap-3 px-4 pt-4 pb-3" : "gap-2 p-2"}>
        <View class={`relative w-full ${phone ? "h-11" : "h-8"}`}>
          <Input ref={(n: any) => { sessionSearchInput = n; }} value={sessionQuery()} placeholder="Search sessions" accessibilityLabel="Search sessions" testID="session-search" onChange={({ value }) => setSessionQuery(value)} class={`h-full w-full rounded-(--radius) pl-7 pr-2 text-[13px] text-foreground placeholder:text-muted-foreground ${phone ? "bg-raised" : "bg-hover"}`} />
          {/* Painted after the input so its well does not cover the glyph. */}
          <View class="absolute left-2 top-0 bottom-0 items-center justify-center" accessible={false}>
            <Icon name="search" size={14} class="tint-muted-foreground" />
          </View>
        </View>
        <View class="flex-row items-center gap-2">
          <Button onPress={() => run("project.switch")} accessibilityLabel="Current project" class={`flex-1 min-w-0 flex-row items-center gap-1.5 rounded-(--radius) hover:bg-accent ${phone ? "min-h-11 bg-raised px-4" : isNarrow() ? "min-h-11 px-2" : "min-h-8 px-2"} ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class="flex-1 text-[13px] leading-5 font-medium text-foreground">{scopeAll() ? "All projects" : currentProject() ? projectName(currentProject()!) : "No project"}</Text>
            <Icon name="chevron-down" size={14} class="tint-muted-foreground" />
          </Button>
          <IconButton icon="plus" label="New session" testID="new-session" class={phone ? "bg-raised" : ""} onPress={() => { dismissSessionSearch(); if (phone) phonePage.set("conversation"); void run("session.new"); }} disabled={!availability(byId.get("session.new")!).ok} reason={availability(byId.get("session.new")!).ok ? hint("session.new") ?? undefined : availability(byId.get("session.new")!).reason} />
        </View>
        <View class="flex-row items-center gap-1">
          <Chip label="Project" on={!scopeAll()} onPress={() => scopeAll.set(false)} />
          <Chip label="All projects" on={scopeAll()} onPress={() => scopeAll.set(true)} />
          <View class="flex-1" />
          <Chip label="Archived" on={archivedFilter()} onPress={() => run("sessions.archived_filter")} />
        </View>
      </View>
      <If when={rowItems().length > 0} else={
        <If when={(m(), model.projects.size === 0)} else={<Empty title={sessionQuery().trim() ? "No matching sessions" : archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button or the New session command."} />}>
          <Empty title="No projects yet" body="Add a project directory to start a session." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
        </If>
      }>
        <List each={rowItems()} keyExtractor={(x) => x.key} class="flex-1" contentStyle={{ paddingHorizontal: phone ? 16 : 8, paddingBottom: 8, gap: 2 }} testID="session-list">
          {(x) => (
            <If when={x().kind === "header"} else={<SessionRow s={() => (x() as any).s as Session} />}>
              <View class="px-2 pt-3 pb-1" testID="session-group">
                <Text style={{ numberOfLines: 1 }} class="text-[12.5px] leading-4 font-medium text-muted-foreground">{(x() as any).title}</Text>
              </View>
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
      <Text class={`text-xs leading-4 ${p.on ? "text-foreground font-medium" : "text-muted-foreground"}`}>{p.label}</Text>
    </Button>
  );
}

function SessionRow(p: { s: () => Session }) {
  const s = p.s;
  effect(() => { loadProjectWorktrees(s().project_id); });
  // Under a project heading the name is already above the row: show the branch, as the project scope does.
  const place = () => {
    const tree = worktreeForDirectory(s().directory, projectWorktrees()[s().project_id]);
    return tree?.branch || (tree?.path && tree.main === false ? tree.path.replace(/\/$/, "").split("/").pop() : "") || "";
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
          class={`group/row relative rounded-(--radius) px-2 hover:bg-accent ${sel() ? "bg-selection" : ""} ${phone ? "min-h-[72px] py-3 pr-14" : isNarrow() ? (two() ? "min-h-[56px] py-2" : "min-h-11 justify-center") : (two() ? "h-12 py-2" : "h-8 justify-center")} ${FOCUS}`}
        >
          <View class="flex-row items-center gap-2 h-5">
            <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 font-medium text-foreground">{s().title || "Untitled"}</Text>
            <If when={!phone}>
              <View class="flex-row items-center gap-1">
                <If when={att()}><AttentionMark kind="waiting" /></If>
                <If when={!att() && s().status === "busy"}><Status kind="running" /></If>
                <Text class={`${T.meta} ${touch ? "opacity-0" : "group-hover/row:opacity-0"}`}>{relTime(s().updated, (tick(), Date.now()))}</Text>
              </View>
            </If>
          </View>
          <If when={two()}>
            <View class="flex-row items-center gap-1 h-4" testID={`session-line-${s().id}`}>
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
            <Text class={`flex-1 text-[13px] leading-5 ${danger ? "text-error" : "text-popover-foreground"}`}>{c.title}</Text>
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
  const currentPath = () => {
    const path = fileListing()?.path ?? "";
    return path;
  };
  const isOpen = (path: string) => { m(); return openFile()?.path === path; };
  return <Scroll class="flex-1">
    <If when={fileListing()} else={<Text class={`${T.sec} p-4`}>Loading files…</Text>}>
      {() => <View class="p-2">
        <View class="flex-row items-center gap-2 px-2 pb-2" testID="files-header">
          <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 font-medium text-foreground">{currentProject() ? projectName(currentProject()!) : "Files"}</Text>
        </View>
        <If when={fileListing()!.path}>
          <Button onPress={() => void loadFiles(currentPath().split("/").slice(0, -1).join("/"))} accessibilityLabel="Parent directory" testID="file-parent"
            class={`flex-row items-center gap-1.5 h-7 rounded-(--radius) px-2 hover:bg-accent ${FOCUS}`}>
            <Icon name="arrow-left" size={12} class="tint-muted-foreground" />
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${T.meta}`}>{currentPath()}</Text>
          </Button>
        </If>
        <If when={fileListing()!.error}><Text class={`${T.sec} px-2`}>{fileListing()!.error}</Text></If>
        <If when={!fileListing()!.error && fileListing()!.entries.length === 0}><Text class={`${T.sec} px-2 pt-1`} testID="files-empty">This folder is empty.</Text></If>
        <Each of={fileListing()!.entries} key={(entry) => entry.name}>{(entry) =>
          <Button onPress={() => { const path = [fileListing()!.path, entry().name].filter(Boolean).join("/"); if (entry().kind === "directory") void loadFiles(path); else if (entry().kind === "file") void showFile(path); }}
            accessibilityLabel={entry().name} testID={`file-entry-${entry().name}`}
            class={`flex-row items-center gap-1.5 h-7 rounded-(--radius) px-2 hover:bg-accent ${isOpen([fileListing()!.path, entry().name].filter(Boolean).join("/")) ? "bg-selection" : ""} ${FOCUS}`}>
            <View class="w-3.5 shrink-0 items-center justify-center">
              <If when={entry().kind === "directory"}><Icon name="chevron-right" size={14} class="tint-muted-foreground" /></If>
            </View>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[13px] leading-5 ${entry().ignored ? "text-faint" : "text-foreground"}`}>{entry().name}</Text>
            <View class="w-4 shrink-0 items-center justify-end">
              <If when={!!entry().git_status}>
                <If when={entry().kind === "directory"} else={
                  <Text class={`text-[13px] leading-5 font-medium ${GIT_TONE[(entry().git_status ?? "").trim().slice(-1)] ?? "text-muted-foreground"}`}>{(entry().git_status ?? "").trim().slice(-1)}</Text>
                }>
                  <View class="size-1.5 rounded-full bg-warning" accessibilityLabel="changed" />
                </If>
              </If>
            </View>
          </Button>
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
        <Text class="flex-1 text-[13px] leading-5 font-medium text-foreground px-2">Projects</Text>
      </View>
      <If when={rows().length === 0}><Empty title="No projects yet" body="Add a project by its directory path. Core creates it if it is new." /></If>
      <List each={items()} keyExtractor={(x) => x.id} class="flex-1" contentStyle={{ paddingHorizontal: 8, paddingBottom: 8, gap: 2 }} testID="project-list">
        {(item) => {
          const row = item();
          if (row.kind === "add") return <Button onPress={() => run("project.add")} disabled={!availability(byId.get("project.add")!).ok} testID="add-project" accessibilityLabel="+ Add project"
            class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 min-h-8 hover:bg-accent ${FOCUS}`}>
            <Text class="text-[13px] leading-5 text-primary">+</Text>
            <Text class="text-[13px] leading-5 text-foreground">Add project</Text>
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
          class={`gap-0.5 rounded-(--radius) px-2.5 py-1.5 hover:bg-accent ${selected() ? "bg-selection" : ""} ${FOCUS}`}>
          <View class="flex-row items-center gap-2">
            <Text style={{ numberOfLines: 1 }} class={`flex-1 text-[13px] leading-5 ${selected() ? "text-foreground font-medium" : "text-foreground"}`}>{project().name}</Text>
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
