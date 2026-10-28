import type { JSX } from "nsh";
import { List, Scroll, Text, View } from "nsh";
import { Button, ContextMenu, Menu, MenuItem, MenuSeparator, Tab, Tabs, Input } from "nsh/base";
import { Btn, Icon, IconButton, Status, AttentionMark, T, FOCUS, relTime, abbrev, midTruncate } from "./kit";
import { manifest } from "../gen/manifest";
import { archive, askDelete, availability, byId, keyLabel, newSession, run, toggleLeft } from "../commands";
import { archivedFilter, centre, connection, currentProject, isNarrow, leftTab, m, model, scopeAll, selectProject, selectSession, tick, live, viewed, savePrefs } from "../store";
import { Session } from "../core/api";
import { fileListing, loadFiles, showFile } from "../store";
import { icons, IconName } from "../icons";
import { fold } from "../gen/theme";

const TAB_ICON: Record<string, IconName> = { sessions: "sessions", files: "files", projects: "projects" };
const tabs = manifest.navigation.left_sidebar.tab as unknown as { id: "sessions" | "files" | "projects"; label: string; command: string; order: number }[];

function hint(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

export function LeftSidebar(p: { drawer?: boolean }) {
  return (
    <View class={`bg-background ${p.drawer ? "flex-1" : "w-[264px] border-r border-border"}`} testID="left-sidebar" role="none" accessibilityLabel="Left sidebar">
      <Tabs class={`flex-row items-center gap-1 px-2 ${isNarrow() ? "h-14" : "h-11"} border-b border-border`}>
        <Each of={[...tabs].sort((a, b) => a.order - b.order)} key={(t) => t.id}>
          {(t) => {
            const c = () => byId.get(t().command)!;
            const dis = () => !availability(c()).ok && t().id !== "sessions" && t().id !== "projects" && !!m() && !currentProjectOk();
            return (
              <Tab
                selected={leftTab() === t().id} accessibilityLabel={t().label} testID={`tab-${t().id}`} disabled={false}
                onPress={() => { leftTab.set(t().id); if (t().id === "files") void loadFiles(); savePrefs(); }}
                tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{t().label}{hint(t().command) ? `  ${hint(t().command)}` : ""}</Text>}
                class={`flex-row items-center gap-1.5 rounded-(--radius) px-2.5 hover:bg-accent selected:bg-selection ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}
              >
                <Icon name={TAB_ICON[t().id]} class={leftTab() === t().id ? "tint-foreground" : "tint-muted-foreground"} />
                <If when={leftTab() === t().id}><Text class="text-[13px] leading-5 text-foreground font-medium">{t().label}</Text></If>
              </Tab>
            );
          }}
        </Each>
      </Tabs>
      <View class="flex-1 min-h-0">
        <If when={leftTab() === "sessions"}><SessionsTab /></If>
        <If when={leftTab() === "files"}><FilesTab /></If>
        <If when={leftTab() === "projects"}><ProjectsTab /></If>
      </View>
      <View class="border-t border-border p-2">
        <Button
          onPress={() => run("app.settings")} testID="open-settings" accessibilityLabel="Settings" selected={centre() === "settings"}
          class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 hover:bg-accent selected:bg-selection ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}
        >
          <Icon name="settings" />
          <Text class="flex-1 text-[13px] leading-5 text-foreground">Settings</Text>
          <Text class={T.meta}>{hint("app.settings") ?? ""}</Text>
        </Button>
      </View>
    </View>
  );
}
const currentProjectOk = () => !!currentProject();

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

type Item = { key: string; kind: "header"; title: string; n: number } | { key: string; kind: "session"; s: Session };
/** All-projects scope groups sessions under their project; one project is a flat list. */
function rowItems(): Item[] {
  const ses = rowSessions();
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
function attention(id: string): "waiting" | "error" | null {
  const tree = model.tree(id);
  for (const a of model.approvals.values()) if (tree.includes(a.session_id)) return "waiting";
  for (const q of model.questions.values()) if (tree.includes(q.session_id)) return "waiting";
  return null;
}

function SessionsTab() {
  return (
    <View class="flex-1 min-h-0">
      <View class="gap-2 p-2">
        <View class="flex-row items-center gap-2">
          <Button onPress={() => run("project.switch")} accessibilityLabel="Current project" class={`flex-1 min-w-0 flex-row items-center gap-1.5 rounded-(--radius) px-2 hover:bg-accent ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}>
            <Text numberOfLines={1} class="flex-1 text-[13px] leading-5 font-medium text-foreground">{scopeAll() ? "All projects" : currentProject() ? projectName(currentProject()!) : "No project"}</Text>
            <Icon name="chevron-down" size={14} class="tint-muted-foreground" />
          </Button>
          <IconButton icon="plus" label="New session" testID="new-session" onPress={() => run("session.new")} disabled={!availability(byId.get("session.new")!).ok} reason={availability(byId.get("session.new")!).ok ? hint("session.new") ?? undefined : availability(byId.get("session.new")!).reason} />
        </View>
        <View class="flex-row items-center gap-1">
          <Chip label="Current project" on={!scopeAll()} onPress={() => run("sessions.scope_toggle")} />
          <Chip label="All" on={scopeAll()} onPress={() => run("sessions.scope_toggle")} />
          <View class="flex-1" />
          <Chip label="Archived" on={archivedFilter()} onPress={() => run("sessions.archived_filter")} />
        </View>
      </View>
      <If when={rowItems().length > 0} else={
        <If when={(m(), model.projects.size === 0)} else={<Empty title={archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button or the New session command."} />}>
          <Empty title="No projects yet" body="Add a project directory to start a session." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
        </If>
      }>
        <List each={rowItems()} keyExtractor={(x) => x.key} class="flex-1" contentClass="px-2 pb-2 gap-0.5" testID="session-list">
          {(x) => (
            <If when={x().kind === "header"} else={<SessionRow s={() => (x() as any).s as Session} />}>
              <View class="flex-row items-center gap-2 px-1.5 pt-3 pb-1">
                <Text numberOfLines={1} class="flex-1 text-xs leading-4 font-medium text-muted-foreground">{(x() as any).title}</Text>
                <Text class={T.meta}>{String((x() as any).n)}</Text>
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
    <Button onPress={p.onPress} selected={p.on} accessibilityLabel={p.label} testID={`chip-${p.label.toLowerCase().replace(/ /g, "-")}`} class={`rounded-(--radius) px-2 min-h-6 justify-center hover:bg-accent selected:bg-selection ${FOCUS}`}>
      <Text class={`text-xs leading-4 ${p.on ? "text-foreground font-medium" : "text-muted-foreground"}`}>{p.label}</Text>
    </Button>
  );
}

function SessionRow(p: { s: () => Session }) {
  const s = p.s;
  const sel = () => model.selected?.sessionId === s().id && centre() === "session";
  const n = () => childCount(s().id);
  const att = () => { m(); return attention(s().id); };
  return (
    <ContextMenu
      popupClass="min-w-[188px] rounded-(--radius-card) border border-border bg-popover p-1 shadow-lg"
      trigger={
        <Button
          onPress={() => void selectSession(s().id)} selected={sel()} testID={`session-${s().id}`} accessibilityLabel={`${s().title || "Untitled"}, ${s().status === "busy" ? "running" : "idle"}`}
          class={`flex-row items-center gap-1 rounded-(--radius) px-1.5 hover:bg-accent selected:bg-selection ${isNarrow() ? "min-h-11" : "min-h-8"} ${FOCUS}`}
        >
          <View class="w-3.5 items-center">
            <If when={att()} else={<If when={s().status === "busy"} else={<Text class="text-[13px] leading-5 text-transparent"> </Text>}><Status kind="running" /></If>}><AttentionMark kind="waiting" /></If>
          </View>
          <Text numberOfLines={1} class={`flex-1 min-w-0 text-[13px] leading-5 ${sel() ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{s().title || "Untitled"}</Text>
          <View class="flex-row items-center gap-1.5">
            <If when={n() > 0}><Text class={T.meta}>{`${n()}↳`}</Text></If>
            <Text class={`${T.meta} w-7 text-right`}>{relTime(s().updated, (tick(), Date.now()))}</Text>
          </View>
        </Button>
      }
    >
      <SessionMenuItems id={s().id} />
    </ContextMenu>
  );
}
function SessionMenuItems(p: { id: string }) {
  const item = (label: string, act: () => void, id?: string, danger = false) => {
    const c = id ? byId.get(id) : undefined;
    const a = c ? availability(c) : { ok: true };
    return (
      <MenuItem onSelect={act} disabled={!a.ok} class={`flex-row items-center gap-2 rounded-(--radius) px-2 min-h-8 hover:bg-accent disabled:opacity-50`}>
        <Text class={`flex-1 text-[13px] leading-5 ${danger ? "text-error" : "text-popover-foreground"}`}>{label}</Text>
        <Text class={T.meta}>{!a.ok ? (a as any).reason?.slice(0, 0) : ""}</Text>
      </MenuItem>
    );
  };
  const s = model.sessions.get(p.id);
  return (
    <View>
      {item("Open", () => void selectSession(p.id))}
      {item("Rename", () => { void selectSession(p.id).then(() => run("session.rename")); }, "session.rename")}
      {s?.archived ? item("Unarchive", () => void archive(p.id, true), "session.unarchive") : item("Archive", () => void archive(p.id), "session.archive")}
      {item("Copy id", () => { void selectSession(p.id).then(() => run("session.copy_id")); }, "session.copy_id")}
      <MenuSeparator class="my-1 h-px bg-border" />
      {item("Delete…", () => askDelete(p.id), "session.delete", true)}
    </View>
  );
}

function FilesTab() {
  return <Scroll class="flex-1" contentClass="p-2">
    <If when={fileListing()} else={<Text class={T.sec}>Loading files…</Text>}>
      {() => <View>
        <If when={fileListing()!.path}><Button onPress={() => void loadFiles(fileListing()!.path.split("/").slice(0, -1).join("/"))} accessibilityLabel="Parent directory"><Text class={T.sec}>← {fileListing()!.path}</Text></Button></If>
        <If when={fileListing()!.error}><Text class={T.sec}>{fileListing()!.error}</Text></If>
        <Each of={fileListing()!.entries} key={(entry) => entry.name}>{(entry) =>
          <Button onPress={() => { const path = [fileListing()!.path, entry().name].filter(Boolean).join("/"); if (entry().kind === "directory") void loadFiles(path); else if (entry().kind === "file") void showFile(path); }} accessibilityLabel={entry().name} class={`flex-row px-2 py-1 hover:bg-accent ${FOCUS}`}>
            <Text class={entry().ignored ? T.meta : T.sec}>{entry().kind === "directory" ? "▸ " : "  "}{entry().name}{entry().git_status ? `  ${entry().git_status}` : ""}</Text>
          </Button>
        }</Each>
      </View>}
    </If>
  </Scroll>;
}
function ProjectsTab() {
  const rows = () => { m(); return model.projectsRecent(); };
  const count = (id: string) => { let n = 0; for (const s of model.sessions.values()) if (s.project_id === id && !s.parent_id && !s.archived) n++; return n; };
  return (
    <View class="flex-1 min-h-0">
      <View class="flex-row items-center gap-2 p-2">
        <Text class="flex-1 text-[13px] leading-5 font-medium text-foreground px-1">Projects</Text>
        <Btn label="Add project" icon="plus" onPress={() => run("project.add")} disabled={!availability(byId.get("project.add")!).ok} reason={(availability(byId.get("project.add")!) as any).reason} testID="add-project" />
      </View>
      <If when={rows().length > 0} else={<Empty title="No projects yet" body="Add a project by its directory path. Core creates it if it is new." />}>
        <List each={rows()} keyExtractor={(x) => x.id} class="flex-1" contentClass="px-2 pb-2 gap-0.5" testID="project-list">
          {(pr) => {
            const sel = () => currentProject() === pr().id;
            return (
              <Button onPress={() => selectProject(pr().id)} selected={sel()} testID={`project-${pr().id}`} accessibilityLabel={`Project ${pr().name}`}
                class={`gap-0.5 rounded-(--radius) px-2.5 py-1.5 hover:bg-accent selected:bg-selection ${FOCUS}`}>
                <View class="flex-row items-center gap-2">
                  <Text numberOfLines={1} class={`flex-1 text-[13px] leading-5 ${sel() ? "text-foreground font-medium" : "text-foreground"}`}>{pr().name}</Text>
                  <Text class={T.meta}>{`${count(pr().id)} · ${relTime(pr().updated)}`}</Text>
                </View>
                <Text numberOfLines={1} class={`${T.meta} font-mono`}>{midTruncate(pr().path, 32)}</Text>
              </Button>
            );
          }}
        </List>
      </If>
    </View>
  );
}
void Input; void Scroll; void abbrev; void icons; void fold; void newSession; void toggleLeft; void Menu; void connection; void live; void viewed;
