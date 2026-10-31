import type { JSX } from "nsh";
import { List, PanResponder, Scroll, Text, View, effect, signal } from "nsh";
import { Button, ContextMenu, MenuItem, Tab, Tabs, Input } from "nsh/base";
import { Btn, Icon, IconButton, Status, AttentionMark, T, FOCUS, relTime, abbrev, midTruncate } from "./kit";
import { manifest } from "../gen/manifest";
import { availability, byId, commands, keyLabel, newSession, run, toggleLeft } from "../commands";
import { archivedFilter, centre, connection, currentProject, focusedSidebarRow, isNarrow, leftTab, m, model, scopeAll, selectProject, selectSession, sidebarRowMenu, tick, live, viewed, savePrefs, SidebarRowTarget, loadProjectWorktrees, projectWorktrees, worktreeForDirectory } from "../store";
import { Project, Session } from "../core/api";
import { request } from "../core/transport";
import { fileListing, loadFiles, showFile } from "../store";
import { icons, IconName } from "../icons";
import { fold } from "../gen/theme";
import { ProjectMcpPanel } from "./project-mcp";

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
            tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{t().label}{hint(t().command) ? `  ${hint(t().command)}` : ""}</Text>}
            class={`flex-row items-center justify-center gap-1.5 rounded-(--radius) hover:bg-accent ${p.labels ? "px-2.5 min-h-11" : isNarrow() ? "size-11" : "size-8"} ${FOCUS}`}
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
    <View class={`bg-sidebar ${p.drawer ? "flex-1" : "shrink-0"}`} style={p.drawer ? {} : { width: leftSidebarWidth() }} testID="left-sidebar" role="none" accessibilityLabel="Left sidebar">
      <If when={!!p.drawer}>
        <View class="flex-row items-center h-14 px-2"><SidebarTabs labels /></View>
      </If>
      <View class="flex-1 min-h-0">
        <If when={leftTab() === "sessions"}><SessionsTab /></If>
        <If when={leftTab() === "files"}><FilesTab /></If>
        <If when={leftTab() === "projects"}><ProjectsTab /></If>
      </View>
      <View class="border-t border-border p-2">
        <Button
          onPress={() => run("app.settings")} testID="open-settings" accessibilityLabel="Settings" selected={centre() === "settings"}
          class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 hover:bg-accent ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}
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
/** Compact session rail shown when the docked sidebar is collapsed. */
export function SessionRail() {
  return (
    <View class="w-12 bg-sidebar items-center pt-2" testID="session-rail" accessibilityLabel="Session rail">
      <IconButton icon="search" label="Search sessions" testID="rail-search" onPress={() => { leftTab.set("sessions"); toggleLeft(); }} />
      <IconButton icon="plus" label="New session" testID="rail-new-session" onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} />
      <Scroll class="flex-1" contentClass="items-center gap-1 pb-2">
        <Each of={rowSessions()} key={(s) => s.id}>
          {(s) => <Button onPress={() => void selectSession(s().id)} accessibilityLabel={`Open ${s().title || "Untitled"}`}
            selected={(m(), model.selected?.sessionId === s().id && centre() === "session")} testID={`rail-session-${s().id}`}
            tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{s().title || "Untitled"}</Text>}
            class={`relative size-9 items-center justify-center rounded-(--radius) hover:bg-accent ${FOCUS}`}>
            <If when={(m(), model.selected?.sessionId === s().id && centre() === "session")}><View class="absolute left-0 top-1 bottom-1 w-0.5 rounded-full bg-primary" /></If>
            <Text class={`text-[13px] ${(m(), model.selected?.sessionId === s().id) ? "font-semibold text-foreground" : "text-muted-foreground"}`}>{(s().title || "Untitled").trim().charAt(0).toUpperCase()}</Text>
          </Button>}
        </Each>
      </Scroll>
    </View>
  );
}

function Empty(p: { title: string; body?: string; action?: JSX.Element }) {
  return (
    <View class="items-start gap-2 p-4">
      <Text class="text-[13px] leading-5 font-medium text-foreground">{p.title}</Text>
      <If when={p.body}><Text class={T.sec}>{p.body}</Text></If>
      {p.action}
    </View>
  );
}

function projectName(id: string) { return model.projects.get(id)?.name ?? "project"; }

const sessionQuery = signal("");
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

function SessionsTab() {
  return (
    <View class="flex-1 min-h-0">
      <View class="gap-2 p-2">
        <Input value={sessionQuery()} placeholder="Search sessions" accessibilityLabel="Search sessions" testID="session-search" onChange={({ value }) => setSessionQuery(value)} class="h-8 rounded-(--radius) bg-background px-2 text-[13px] text-foreground placeholder:text-muted-foreground" />
        <View class="flex-row items-center gap-2">
          <Button onPress={() => run("project.switch")} accessibilityLabel="Current project" class={`flex-1 min-w-0 flex-row items-center gap-1.5 rounded-(--radius) px-2 hover:bg-accent ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class="flex-1 text-[13px] leading-5 font-medium text-foreground">{scopeAll() ? "All projects" : currentProject() ? projectName(currentProject()!) : "No project"}</Text>
            <Icon name="chevron-down" size={14} class="tint-muted-foreground" />
          </Button>
          <IconButton icon="plus" label="New session" testID="new-session" onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} reason={availability(byId.get("session.new")!).ok ? hint("session.new") ?? undefined : availability(byId.get("session.new")!).reason} />
        </View>
        <View class="flex-row items-center gap-1">
          <Chip label="Project" on={!scopeAll()} onPress={() => scopeAll.set(false)} />
          <Chip label="Global" on={scopeAll()} onPress={() => scopeAll.set(true)} />
          <View class="flex-1" />
          <Chip label="Archived" on={archivedFilter()} onPress={() => run("sessions.archived_filter")} />
        </View>
      </View>
      <If when={rowItems().length > 0} else={
        <If when={(m(), model.projects.size === 0)} else={<Empty title={sessionQuery().trim() ? "No matching sessions" : archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button or the New session command."} />}>
          <Empty title="No projects yet" body="Add a project directory to start a session." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
        </If>
      }>
        <List each={rowItems()} keyExtractor={(x) => x.key} class="flex-1" contentStyle={{ paddingHorizontal: 8, paddingBottom: 8, gap: 2 }} testID="session-list">
          {(x) => (
            <If when={x().kind === "header"} else={<SessionRow s={() => (x() as any).s as Session} />}>
              <View class="px-2 pt-4 pb-1" testID="session-group">
                <Text style={{ numberOfLines: 1 }} class="text-[11px] leading-4 font-semibold uppercase tracking-[0.06em] text-muted-foreground">{(x() as any).title}</Text>
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
    <Button onPress={p.onPress} selected={p.on} accessibilityLabel={p.label} testID={`chip-${p.label.toLowerCase().replace(/ /g, "-")}`} class={`rounded-(--radius) px-2 min-h-6 justify-center hover:bg-accent ${FOCUS}`}>
      <Text class={`text-xs leading-4 ${p.on ? "text-foreground font-semibold" : "text-muted-foreground"}`}>{p.on ? "✓ " : ""}{p.label}</Text>
    </Button>
  );
}

function SessionRow(p: { s: () => Session }) {
  const s = p.s;
  effect(() => { if (!scopeAll()) loadProjectWorktrees(s().project_id); });
  const place = () => {
    if (scopeAll()) return projectName(s().project_id);
    const tree = worktreeForDirectory(s().directory, projectWorktrees()[s().project_id]);
    return tree?.branch || (tree?.path && tree.main === false ? tree.path.replace(/\/$/, "").split("/").pop() : "") || "";
  };
  const ref = () => ({ kind: "session" as const, id: s().id });
  const sel = () => model.selected?.sessionId === s().id && centre() === "session";
  const n = () => childCount(s().id);
  const att = () => { m(); return attention(s().id); };
  return (
    <ContextMenu
      open={sidebarRowMenu()?.kind === "session" && sidebarRowMenu()?.id === s().id}
      onOpenChange={({ open }) => {
        if (open) {
          focusedSidebarRow.set(ref());
          sidebarRowMenu.set(ref());
          void selectSession(s().id);
        } else if (sidebarRowMenu()?.id === s().id) sidebarRowMenu.set(null);
      }}
      popupClass="min-w-[188px] rounded-(--radius-card) border border-border bg-popover p-1 shadow-lg"
      trigger={
        <View role="button"
          onFocus={() => focusedSidebarRow.set(ref())}
          onBlur={() => { if (sidebarRowMenu()?.id !== s().id && focusedSidebarRow()?.id === s().id) focusedSidebarRow.set(null); }}
          onPress={() => { focusedSidebarRow.set(ref()); void selectSession(s().id); }} accessibilityState={{ selected: sel() }} testID={`session-${s().id}`} accessibilityLabel={`${s().title || "Untitled"}, ${s().status === "busy" ? "running" : "idle"}`}
          class={`group/row relative rounded-(--radius) px-2 py-2 hover:bg-accent ${isNarrow() ? "min-h-[56px]" : "h-[48px]"} ${FOCUS}`}
        >
          <If when={focusedSidebarRow()?.id === s().id || sel()}>
            {() => <View class="absolute left-0 top-2 bottom-2 w-0.5 rounded-full bg-primary" testID="row-focus-bar" />}
          </If>
          <View class="flex-row items-center gap-2 h-5">
            <View class="w-3.5 items-center">
              <If when={att()} else={<If when={s().status === "busy"} else={<Text class="text-[13px] leading-5 text-transparent"> </Text>}><Status kind="running" /></If>}><AttentionMark kind="waiting" /></If>
            </View>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[13px] leading-5 ${sel() ? "text-foreground font-semibold" : "text-foreground font-medium"}`}>{s().title || "Untitled"}</Text>
            <Text class={`${T.meta} group-hover/row:opacity-0`}>{relTime(s().updated, (tick(), Date.now()))}</Text>
          </View>
          <View class="flex-row items-center gap-1 pl-[22px] h-4">
            <If when={place()}><Text style={{ numberOfLines: 1 }} testID={`session-place-${s().id}`} class={`${T.meta} min-w-0`}>{place()}</Text></If>
            <If when={snippetOf(s().id)}>{(t) => <Text style={{ numberOfLines: 1 }} testID={`session-snippet-${s().id}`} class={`${T.meta} flex-1 min-w-0`}>{`· ${t()}`}</Text>}</If>
            <View class="flex-1" />
            <If when={n() > 0}><Text class={T.meta}>{`${n()} ↳`}</Text></If>
          </View>
          <Button onPress={() => { focusedSidebarRow.set(ref()); sidebarRowMenu.set(ref()); void selectSession(s().id); }}
            accessibilityLabel={`Actions for ${s().title || "Untitled"}`} testID={`row-menu-${s().id}`}
            class={`absolute right-1.5 top-1.5 size-6 items-center justify-center rounded-(--radius) opacity-0 group-hover/row:opacity-100 focus:opacity-100 hover:bg-hover ${FOCUS}`}>
            <Text class="text-[15px] leading-5 text-foreground-secondary">⋯</Text>
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

function FilesTab() {
  return <Scroll class="flex-1" contentStyle={{ padding: 8 }}>
    <If when={fileListing()} else={<Text class={T.sec}>Loading files…</Text>}>
      {() => <View>
        <If when={fileListing()!.path}><Button onPress={() => void loadFiles(fileListing()!.path.split("/").slice(0, -1).join("/"))} accessibilityLabel="Parent directory"><Text class={T.sec}>← {fileListing()!.path}</Text></Button></If>
        <If when={fileListing()!.error}><Text class={T.sec}>{fileListing()!.error}</Text></If>
        <Each of={fileListing()!.entries} key={(entry) => entry.name}>{(entry) =>
          <Button onPress={() => { const path = [fileListing()!.path, entry().name].filter(Boolean).join("/"); if (entry().kind === "directory") void loadFiles(path); else if (entry().kind === "file") void showFile(path); }} accessibilityLabel={entry().name} testID={`file-entry-${entry().name}`} class={`flex-row px-2 py-1 hover:bg-accent ${FOCUS}`}>
            <Text class={entry().ignored ? T.meta : T.sec}>{entry().kind === "directory" ? "▸ " : "  "}{entry().name}{entry().git_status ? `  ${entry().git_status}` : ""}</Text>
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
        <Text class="flex-1 text-[13px] leading-5 font-medium text-foreground px-1">Projects</Text>
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
      popupClass="min-w-[188px] rounded-(--radius-card) border border-border bg-popover p-1 shadow-lg"
      trigger={
        <View role="button" onFocus={() => focusedSidebarRow.set(ref())}
          onBlur={() => { if (sidebarRowMenu()?.id !== project().id && focusedSidebarRow()?.id === project().id) focusedSidebarRow.set(null); }}
          onPress={() => { focusedSidebarRow.set(ref()); selectProject(project().id); }} accessibilityState={{ selected: selected() }} testID={`project-${project().id}`} accessibilityLabel={`Project ${project().name}`}
          class={`gap-0.5 rounded-(--radius) px-2.5 py-1.5 hover:bg-accent ${FOCUS}`}>
          <View class="flex-row items-center gap-2">
            <Text style={{ numberOfLines: 1 }} class={`flex-1 text-[13px] leading-5 ${selected() ? "text-foreground font-medium" : "text-foreground"}`}>{project().name}</Text>
            <Text class={T.meta}>{`${p.count(project().id)} · ${relTime(project().updated)}`}</Text>
          </View>
          <Text style={{ numberOfLines: 1 }} class={`${T.meta} font-mono`}>{midTruncate(project().path, 32)}</Text>
        </View>
      }
    >
      <RowMenuItems row={ref} />
    </ContextMenu>
  );
}
void Input; void Scroll; void abbrev; void icons; void fold; void newSession; void toggleLeft; void connection; void live; void viewed;
