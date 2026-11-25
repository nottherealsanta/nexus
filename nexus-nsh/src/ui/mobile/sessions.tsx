import type { JSX } from "nsh";
import { Icon, Spinner, Empty, Btn, FOCUS, TS, relTime } from "../kit";
import { List, Text, View, safeArea } from "nsh";
import { Button, Input } from "nsh/base";
import { signal } from "nsh";
import { compactApproval, sessionSections, projectDot, subagentsText } from "../../mobile";
import { reply } from "../composer";
import type { Approval } from "../../core/api";
import { archivedFilter, currentProject, m, model, scopeAll, selectSession, tick } from "../../store";
import type { Session } from "../../core/api";
import { projectLabel, projectSheet, refreshSessions, rowSheet, leaveDrawer } from "./state";
import { run } from "../../commands";

type Item =
  | { key: string; kind: "header"; id: string; title: string; count: number }
  | { key: string; kind: "session"; s: Session; section: string; first: boolean; last: boolean };

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
  // A section's rows sit in one rounded card: the first and last row round the card's corners and carry its edges.
  return secs.flatMap((s) => [
    { key: `h-${s.id}`, kind: "header" as const, id: s.id, title: s.title, count: s.count },
    ...s.items.map((x, i) => ({ key: x.id, kind: "session" as const, s: x as Session, section: s.id, first: i === 0, last: i === s.items.length - 1 })),
  ]);
}

/** The project switcher: a chip with the current project's colour, name and a chevron; it opens the project sheet. */
export function ProjectTitle() {
  const dot = () => { m(); const id = scopeAll() ? null : currentProject(); return id ? projectDot(id) : "bg-faint"; };
  return (
    <Button onPress={() => projectSheet.set(true)} accessibilityLabel={`Project: ${projectLabel()}. Switch project`} testID="project-selector"
      class={`self-start min-h-8 pr-2 rounded-full flex-row items-center gap-1.5 min-w-0 active:opacity-60 ${FOCUS}`}>
      <View class={`size-2 shrink-0 rounded-full ${dot()}`} accessible={false} />
      <Text style={{ numberOfLines: 1 }} class="min-w-0 shrink font-(family-name:--font-ui) text-[15px] leading-5 text-foreground-secondary">{projectLabel()}</Text>
      <Icon name="chevron-down" size={13} class="tint-foreground-secondary" />
    </Button>
  );
}

function SectionHeader(p: { id: string; title: string; count: number }) {
  return (
    <View class="flex-row items-center gap-2 px-5 pt-6 pb-2" testID={`sessions-section-${p.id}`} accessibilityLabel={`${p.title}, ${p.count}`}>
      <Text class="font-(family-name:--font-ui) text-[13px] leading-[18px] font-semibold tracking-[0.5px] text-muted-foreground">{p.title.toUpperCase()}</Text>
      <Text class="font-(family-name:--font-ui) text-[13px] leading-[18px] font-medium text-muted-foreground tabular-nums">{String(p.count)}</Text>
    </View>
  );
}

/** The approval a waiting session asks for (its own or a child's), for the row's quick Allow once. */
function pendingApproval(id: string): Approval | undefined {
  m();
  const tree = new Set(model.tree(id));
  for (const a of model.approvals.values()) if (tree.has(a.session_id)) return a;
  return undefined;
}

function PhoneSessionRow(p: { s: () => Session; pane: SessionsPaneProps; section: string; first: boolean; last: boolean }) {
  const s = p.s;
  const att = () => { m(); return p.pane.attention(s().id); };
  const busy = () => p.section === "running";
  // Search hits show the matching text; across all projects the row names its project; otherwise one line is enough.
  const sub = () => p.pane.snippetOf(s().id) ?? (scopeAll() ? projectName(s().project_id) : "");
  const ask = () => (att() === "waiting" ? pendingApproval(s().id) : undefined);
  const allowing = signal(false);
  // Its subagents, as one quiet note on the row (user, 2026-11-24): how many, and how many are working.
  const kids = () => {
    m();
    const ids = model.tree(s().id).filter((x) => x !== s().id);
    const running = ids.filter((x) => model.running.has(x) || model.sessions.get(x)?.status === "busy").length;
    return subagentsText(ids.length, running);
  };
  const radius = () => ({
    borderTopLeftRadius: p.first ? 16 : 0, borderTopRightRadius: p.first ? 16 : 0,
    borderBottomLeftRadius: p.last ? 16 : 0, borderBottomRightRadius: p.last ? 16 : 0,
  });
  return (
    <View role="button" focusable testID={`session-${s().id}`}
      accessibilityLabel={`${s().title || "Untitled"}, ${att() === "waiting" ? "needs your input" : busy() ? "running" : "idle"}`}
      onPress={() => { leaveDrawer(); void selectSession(s().id); }}
      onLongPress={() => rowSheet.set({ id: s().id, renaming: false })}
      style={radius()}
      class={`relative mx-4 bg-phone-card px-4 ${p.first ? "pt-3.5" : "pt-3"} ${p.last ? "pb-3.5" : "pb-3"} active:bg-phone-control ${FOCUS}`}>
      <View class="flex-row items-start gap-3">
        <View class="w-3 h-6 items-center justify-center" accessible={false}>
          <If when={att() === "waiting"}>
            <Text class="text-[12px] leading-6 text-warning">◆</Text>
          </If>
          <If when={att() !== "waiting" && busy()}>
            <Spinner class="font-(family-name:--font-ui) text-[12px] leading-6 text-info" />
          </If>
          <If when={att() !== "waiting" && !busy()}>
            <View class="size-2 rounded-full bg-muted-foreground" testID={`session-dot-${s().id}`} accessible={false} />
          </If>
        </View>
        <View class="flex-1 min-w-0">
          <View class="flex-row items-center gap-2 min-h-6">
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 font-(family-name:--font-ui) text-[17px] leading-6 text-foreground ${att() === "waiting" || busy() ? "font-semibold" : "font-medium"}`}>{s().title || "Untitled"}</Text>
            <Text class="font-(family-name:--font-ui) text-[13px] leading-6 text-muted-foreground tabular-nums">{relTime(s().updated, (tick(), Date.now()))}</Text>
          </View>
          <View class={`flex-row items-center gap-1.5 ${att() !== "waiting" && !busy() && !sub() && !s().archived && !kids() ? "" : "min-h-5"}`} testID={`session-line-${s().id}`}>
            <If when={att() === "waiting"}>
              <Text testID={`session-needs-${s().id}`} class="font-(family-name:--font-ui) text-[14px] leading-5 text-warning">{ask() ? `Wants to run ${ask()!.tool}` : "Needs your input"}</Text>
            </If>
            <If when={att() !== "waiting" && busy()}>
              <Text class="font-(family-name:--font-ui) text-[14px] leading-5 text-info">Working</Text>
            </If>
            <If when={att() !== "waiting" && !busy()}>
              <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[14px] leading-5 text-muted-foreground shrink min-w-0">{sub()}</Text>
            </If>
            <If when={s().archived}><Text class="font-(family-name:--font-ui) text-[14px] leading-5 text-muted-foreground">archived</Text></If>
            <If when={!!kids() && att() !== "waiting"}>
              <Text style={{ numberOfLines: 1 }} testID={`session-subagents-${s().id}`} class="shrink min-w-0 font-(family-name:--font-ui) text-[14px] leading-5 text-muted-foreground">{`${busy() || sub() ? "· " : ""}${kids()}`}</Text>
            </If>
          </View>
          <If when={ask()}>{() => <>
            <Text style={{ numberOfLines: 1 }} testID={`session-ask-${s().id}`} class="font-mono text-[13px] leading-[19px] text-foreground-secondary pt-0.5">{compactApproval(ask()!)}</Text>
            <View class="flex-row gap-2 pt-2.5 pb-0.5">
              <Button onPress={() => { leaveDrawer(); void selectSession(s().id); }} accessibilityLabel={`Open ${s().title || "Untitled"}`} testID={`session-open-${s().id}`}
                class={`flex-1 min-h-10 items-center justify-center rounded-full bg-phone-control active:opacity-70 ${FOCUS}`}>
                <Text class="font-(family-name:--font-ui) text-[15px] leading-5 font-medium text-foreground">Open</Text>
              </Button>
              <Button onPress={() => { const a = ask(); if (!a || allowing()) return; allowing.set(true); void reply(a, true).finally(() => allowing.set(false)); }} disabled={allowing()}
                accessibilityLabel={`Allow once: ${compactApproval(ask()!)}`} testID={`session-allow-${s().id}`}
                class={`flex-1 min-h-10 items-center justify-center rounded-full bg-foreground active:opacity-80 disabled:opacity-50 ${FOCUS}`}>
                <Text class="font-(family-name:--font-ui) text-[15px] leading-5 font-semibold text-background">Allow once</Text>
              </Button>
            </View>
          </>}</If>
        </View>
      </View>
      <If when={!p.last}>
        <View class="absolute left-[44px] right-0 bottom-0 h-px bg-border" accessible={false} />
      </If>
    </View>
  );
}

/**
 * The phone's session list: the Sessions tab full screen and the conversation's left drawer share it. A header with the
 * wordmark and the new-session button, the project switcher chip, a search field, then Needs you / Running / Recent
 * sections, each a rounded card of rows; long-press for the row menu; pull to refresh.
 */
export function PhoneSessions(p: SessionsPaneProps) {
  const refreshing = signal(false);
  const refresh = () => { refreshing.set(true); refreshSessions(); setTimeout(() => refreshing.set(false), 900); };
  const list = () => items(p);
  return (
    <View class="flex-1 min-h-0 bg-background" testID={p.variant === "drawer" ? "phone-drawer-sessions" : "phone-sessions"}>
      <View class="flex-row items-center justify-between pl-5 pr-4 min-h-[52px]" style={{ paddingTop: safeArea().top + 6 }}>
        <Text class={`font-(family-name:--font-ui) ${p.variant === "drawer" ? "text-[28px] leading-[34px]" : "text-[34px] leading-[41px]"} font-bold tracking-[-0.4px] text-foreground`} testID="phone-sessions-title">Sessions</Text>
        {p.actions}
      </View>
      <View class="px-4 gap-2">
        <View class="pl-1"><ProjectTitle /></View>
        <View class="flex-row items-center gap-2 px-3 h-[38px] rounded-[12px] bg-phone-control">
          <Icon name="search" size={16} class="tint-muted-foreground" />
          <Input ref={p.inputRef} value={p.query()} placeholder="Search" accessibilityLabel="Search sessions" testID="session-search"
            onChange={({ value }) => p.setQuery(value)}
            class={`h-full flex-1 min-w-0 rounded-none px-0 ${TS.body} text-foreground placeholder:text-muted-foreground bg-phone-control`} />
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
            {(x) => <If when={x().kind === "header"} else={<PhoneSessionRow s={() => (x() as any).s as Session} section={(x() as any).section} first={(x() as any).first} last={(x() as any).last} pane={p} />}>
              <SectionHeader id={(x() as any).id} title={(x() as any).title} count={(x() as any).count} />
            </If>}
          </List>
        </If>
      </View>
    </View>
  );
}
