import { Text, View } from "nsh";
import { Button, Input } from "nsh/base";
import { Icon, IconButton, Status, T, FOCUS } from "./kit";
import { byId, availability, keyLabel, renameSession, run, rightIsOpen, rightWidthHint } from "../commands";
import { centre, connection, drawer, isNarrow, leftOpen, m, model, renaming, unread, viewed, selectSession, width } from "../store";
import { structure } from "../gen/theme";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

export function Crumbs() {
  const crumbs = () => {
    m();
    const out: { id: string; title: string }[] = [];
    let s = viewed();
    while (s) { out.unshift({ id: s.id, title: s.title || "Untitled" }); s = s.parent_id ? model.sessions.get(s.parent_id) ?? null : null; }
    return out;
  };
  const view = () => centre();
  return (
    <View class="flex-1 min-w-0 overflow-hidden flex-row items-center justify-center gap-1.5" accessibilityLabel="Breadcrumbs">
      <If when={view() === "settings"}><Text class={T.title} numberOfLines={1}>Settings</Text></If>
      <If when={view() === "new_session" || (view() === "session" && !viewed())}><Text class={T.title} numberOfLines={1}>New session</Text></If>
      <If when={view() === "session" && !!viewed()}>
        <Each of={crumbs()} key={(c) => c.id}>
          {(c, i) => {
            const last = () => i() === crumbs().length - 1;
            return (
              <View class={`flex-row items-center gap-1.5 ${last() ? "shrink min-w-0 overflow-hidden" : "shrink min-w-0 overflow-hidden max-w-[30%]"}`}>
                <If when={i() > 0}><Text class="text-muted-foreground text-[13px]">{structure.breadcrumb.unicode}</Text></If>
                <If when={last() && renaming()} else={
                  <Button onPress={() => (last() ? void run("session.rename") : void selectSession(c().id))} accessibilityLabel={last() ? `Rename session ${c().title}` : `Go to ${c().title}`} class={`shrink min-w-0 overflow-hidden rounded-(--radius) px-1.5 hover:bg-accent ${FOCUS}`}>
                    <Text numberOfLines={1} ellipsizeMode="tail" class={last() ? T.title : "text-[13px] leading-5 text-muted-foreground"}>{c().title}</Text>
                  </Button>
                }>
                  <RenameField initial={c().title} />
                </If>
              </View>
            );
          }}
        </Each>
        
      </If>
    </View>
  );
}

function RenameField(p: { initial: string }) {
  let text = p.initial;
  const done = (commit: boolean) => { renaming.set(false); if (commit && text.trim() && text.trim() !== p.initial) void renameSession(text.trim()); };
  return (
    <Input
      autoFocus defaultValue={p.initial} onChange={(t) => (text = t)} onSubmit={() => done(true)} submitBehavior="submit"
      accessibilityLabel="Session title" testID="rename-input" onBlur={() => done(true)}
      class="min-w-[140px] max-w-[320px] rounded-(--radius) border border-ring bg-card px-2 h-7 text-[14px] text-foreground"
    />
  );
}

export function Header() {
  const conn = () => connection();
  return (
    <View class={`flex-row items-center gap-2 border-b border-border bg-background ${isNarrow() ? "h-14 px-2" : "h-12 px-3"}`} role="toolbar" accessibilityLabel="Workspace header" testID="header">
      <IconButton icon="panel-left" label="Toggle left sidebar" testID="toggle-left" selected={isNarrow() ? drawer() === "left" : leftOpen()} onPress={() => run("sidebar.left.toggle")} reason={key("sidebar.left.toggle") ?? undefined} />
      <Crumbs />
      <View class="flex-row items-center gap-1">
        <If when={viewed()?.status === "busy"}><Status kind="running" word /></If>
        <If when={conn().kind !== "live" && conn().kind !== "connecting"}>
          <View class="flex-row items-center gap-1 pr-1"><Status kind="error" word={false} /><Text class="text-xs leading-4 text-error">{conn().kind === "reconnecting" ? "reconnecting" : conn().kind === "starting" ? "starting" : "offline"}</Text></View>
        </If>
        <View>
          <IconButton icon="bell" label="Notifications" testID="open-notifications" onPress={() => run("notifications.open")} reason={key("notifications.open") ?? undefined} />
          <If when={unread() > 0}><View class="absolute top-1 right-1 size-2 rounded-full bg-info" accessible={false} /></If>
        </View>
        <IconButton icon="panel-right" label="Toggle right sidebar" testID="toggle-right" selected={isNarrow() ? drawer() === "right" : rightIsOpen(width())} onPress={() => { rightWidthHint.w = width(); void run("sidebar.right.toggle"); }} reason={key("sidebar.right.toggle") ?? undefined} />
      </View>
    </View>
  );
}
void Icon; void availability;
