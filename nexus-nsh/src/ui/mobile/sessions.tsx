import type { JSX } from "nsh";
import { Icon, Spinner, Empty, Btn, FOCUS, T, TS, relTime } from "../kit";
import { List, Text, View, safeArea } from "nsh";
import { Button, Input } from "nsh/base";
import { signal } from "nsh";
import { sessionSections, projectDot } from "../../mobile";
import { archivedFilter, currentProject, m, model, scopeAll, selectSession, tick } from "../../store";
import type { Session } from "../../core/api";
import { projectLabel, projectSheet, refreshSessions, rowSheet, leaveDrawer } from "./state";
import { run } from "../../commands";

type Item =
  | { key: string; kind: "header"; id: string; title: string; count: number }
  | { key: string; kind: "session"; s: Session; section: string };

export interface SessionsPaneProps {
  variant: "tab" | "drawer";
  query: () => string;
  setQuery: (v: string) => void;
  matches: (s: Session) => boolean;
  snippetOf: (id: string) => string | undefined;
  attention: (id: string) => "waiting" | "error" | null;
  inputRef: (n: any) => void;
  /** The `+` new-session control (the Sessions tab and the drawer both show it). */
  actions?: JSX.Element;
}

const projectName = (id: string) => model.projects.get(id)?.name ?? "";

/** The sessions as the phone lists them: roots of the chosen project (or all), the archive filter and the search applied. */
function visible(p: SessionsPaneProps): Session[] {
  m();
  const proj = currentProject();
  return model.sessionsRecent().filter((s) => !s.parent_id
    && (scopeAll() || !proj || s.project_id === proj)
    && (archivedFilter() || !s.archived)
    && p.matches(s));
}

function items(p: SessionsPaneProps): Item[] {
  m();
  const secs = sessionSections(visible(p), {
    waiting: (id) => p.attention(id) === "waiting",
    running: (id) => model.tree(id).some((x) => model.running.has(x)) || model.sessions.get(id)?.status === "busy",
  });
  return secs.flatMap((s) => [
    { key: `h-${s.id}`, kind: "header" as const, id: s.id, title: s.title, count: s.count },
    ...s.items.map((x) => ({ key: x.id, kind: "session" as const, s: x as Session, section: s.id })),
  ]);
}

/** The project switcher: the current project's name and a chevron; it opens the project sheet. */
export function ProjectTitle() {
  return (
    <Button onPress={() => projectSheet.set(true)} accessibilityLabel={`Project: ${projectLabel()}. Switch project`} testID="project-selector"
      class={`flex-1 min-w-0 flex-row items-center gap-1.5 min-h-11 ${FOCUS}`}>
      <Text style={{ numberOfLines: 1 }} class="min-w-0 shrink text-[22px] leading-8 font-semibold text-foreground">{projectLabel()}</Text>
      <Icon name="chevron-down" size={16} class="tint-muted-foreground" />
    </Button>
  );
}

function SectionHeader(p: { id: string; title: string; count: number }) {
  const needs = p.id === "needs";
  return (
    <View class="flex-row items-center gap-2 px-4 pt-5 pb-1" testID={`sessions-section-${p.id}`} accessibilityLabel={`${p.title}, ${p.count}`}>
      <Text class={`${T.section} ${needs ? "text-warning" : ""}`}>{p.title}</Text>
      <View class={`rounded-full px-1.5 min-w-5 items-center ${needs ? "bg-warning" : "bg-muted"}`}>
        <Text class={`text-[12px] leading-5 font-medium tabular-nums ${needs ? "text-on-accent" : "text-muted-foreground"}`}>{String(p.count)}</Text>
      </View>
    </View>
  );
}

function PhoneSessionRow(p: { s: () => Session; pane: SessionsPaneProps; section: string }) {
  const s = p.s;
  const att = () => { m(); return p.pane.attention(s().id); };
  const busy = () => p.section === "running";
  const sub = () => p.pane.snippetOf(s().id) ?? projectName(s().project_id);
  return (
    <View role="button" focusable testID={`session-${s().id}`}
      accessibilityLabel={`${s().title || "Untitled"}, ${att() === "waiting" ? "needs your input" : busy() ? "running" : "idle"}`}
      onPress={() => { leaveDrawer(); void selectSession(s().id); }}
      onLongPress={() => rowSheet.set({ id: s().id, renaming: false })}
      class={`relative px-4 py-3 min-h-[68px] justify-center gap-0.5 active:bg-selection ${FOCUS}`}>
      <View class="flex-row items-center gap-2.5 min-h-6">
        <View class={`size-2.5 shrink-0 rounded-full ${projectDot(s().project_id)}`} testID={`session-dot-${s().id}`} accessible={false} />
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.body} font-medium text-foreground`}>{s().title || "Untitled"}</Text>
        <Text class={T.meta}>{relTime(s().updated, (tick(), Date.now()))}</Text>
      </View>
      <View class="flex-row items-center gap-1.5 min-h-5 pl-5" testID={`session-line-${s().id}`}>
        <If when={att() === "waiting"}>
          <Text testID={`session-needs-${s().id}`} class="text-[14px] leading-5 font-medium text-warning">Needs your input</Text>
        </If>
        <If when={att() !== "waiting" && busy()}>
          <Spinner class="text-[14px] leading-5 text-info" />
          <Text class="text-[14px] leading-5 text-info">Working</Text>
        </If>
        <If when={att() !== "waiting" && !busy()}>
          <Text style={{ numberOfLines: 1 }} class={`${T.meta} flex-1 min-w-0`}>{sub()}</Text>
        </If>
        <If when={s().archived}><Text class={T.meta}>archived</Text></If>
      </View>
      <View class="absolute left-4 right-0 bottom-0 h-px bg-border" accessible={false} />
    </View>
  );
}

/**
 * The phone's session list: the Sessions tab full screen and the conversation's left drawer share it. A header with the
 * project switcher, a search field, then Needs you / Running / Recent sections; long-press for the row menu; pull to refresh.
 */
export function PhoneSessions(p: SessionsPaneProps) {
  const refreshing = signal(false);
  const refresh = () => { refreshing.set(true); refreshSessions(); setTimeout(() => refreshing.set(false), 900); };
  const list = () => items(p);
  return (
    <View class="flex-1 min-h-0 bg-sidebar" testID={p.variant === "drawer" ? "phone-drawer-sessions" : "phone-sessions"}>
      <View class="flex-row items-center gap-1 pl-4 pr-2" style={{ paddingTop: safeArea().top + 6 }}>
        <ProjectTitle />
        {p.actions}
      </View>
      <View class="px-4 pt-1 pb-1">
        <View class="flex-row items-center gap-2 px-3 h-11 rounded-(--radius) bg-raised">
          <Icon name="search" size={16} class="tint-muted-foreground" />
          <Input ref={p.inputRef} value={p.query()} placeholder="Search sessions" accessibilityLabel="Search sessions" testID="session-search"
            onChange={({ value }) => p.setQuery(value)}
            class={`h-full flex-1 min-w-0 rounded-none px-0 ${TS.body} text-foreground placeholder:text-muted-foreground bg-raised`} />
        </View>
      </View>
      <View class="flex-1 min-h-0">
        <If when={list().length > 0} else={
          <If when={(m(), model.projects.size === 0)} else={<Empty title={p.query().trim() ? "No matching sessions" : archivedFilter() ? "No archived sessions" : "No sessions yet"} body={archivedFilter() ? "Archive a session to find it here." : "Start one with the + button."} />}>
            <Empty title="No projects yet" body="Add a project on a computer, then it shows here." action={<Btn label="Add project" icon="plus" onPress={() => run("project.add")} kind="primary" />} />
          </If>
        }>
          <List each={list()} keyExtractor={(x) => x.key} class="flex-1" refreshing={refreshing()} onRefresh={refresh} testID="session-list"
            contentStyle={{ paddingBottom: 16 }}>
            {(x) => <If when={x().kind === "header"} else={<PhoneSessionRow s={() => (x() as any).s as Session} section={(x() as any).section} pane={p} />}>
              <SectionHeader id={(x() as any).id} title={(x() as any).title} count={(x() as any).count} />
            </If>}
          </List>
        </If>
      </View>
    </View>
  );
}
