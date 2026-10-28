import { Scroll, Text, View } from "nsh";
import { Button, Input } from "nsh/base";
import { IconButton, Status, FOCUS } from "./kit";
import { byId, closeTab, keyLabel, renameSession, run, rightIsOpen, rightWidthHint, tabIds } from "../commands";
import { centre, connection, drawer, isNarrow, leftOpen, m, model, renaming, unread, viewed, selectSession, width } from "../store";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

function SessionTab(p: { id: string }) {
  const session = () => { m(); return model.sessions.get(p.id); };
  const title = () => session()?.title || "Untitled";
  const active = () => centre() !== "settings" && centre() !== "new_session" && viewed()?.id === p.id;
  const status = () => {
    m();
    if (session()?.status === "busy") return { glyph: "◐", tone: "text-primary", label: "running" };
    if (model.failedSessions.has(p.id)) return { glyph: "✕", tone: "text-error", label: "failed" };
    if (model.unreadSessions.has(p.id)) return { glyph: "●", tone: "text-info", label: "unread" };
    return { glyph: "○", tone: "text-muted-foreground", label: "idle" };
  };
  const activate = () => {
    if (active()) void run("session.rename");
    else void selectSession(p.id);
  };
  return (
    <View class={`flex-row min-w-0 max-w-[224px] items-center rounded-(--radius) border ${active() ? "border-border bg-card" : "border-transparent bg-background hover:bg-accent"}`}>
      <Button
        onPress={activate} selected={active()} accessibilityLabel={`${active() ? "Rename" : "Open"} ${title()}, ${status().label}`}
        testID={`session-tab-${p.id}`} class={`flex-row min-w-0 flex-1 items-center gap-1.5 rounded-l-(--radius) px-2 h-8 ${FOCUS}`}
      >
        <Text class={`shrink-0 text-[13px] leading-5 ${status().tone}`} accessibilityLabel={status().label}>{status().glyph}</Text>
        <If when={active() && renaming()} else={
          <Text testID={`tab-title-${p.id}`} numberOfLines={1} ellipsizeMode="tail" class={`min-w-0 flex-1 text-[13px] leading-5 ${active() ? "font-semibold text-foreground" : "text-foreground-secondary"}`}>{title()}</Text>
        }>
          <RenameField initial={title()} />
        </If>
      </Button>
      <Button onPress={() => closeTab(p.id)} accessibilityLabel={`Close ${title()} tab`} testID={`tab-close-${p.id}`} class={`shrink-0 items-center justify-center rounded-r-(--radius) px-1 h-8 text-muted-foreground hover:text-foreground ${FOCUS}`}>
        <Text class="text-[15px] leading-5">×</Text>
      </Button>
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
      class="min-w-[100px] max-w-[170px] rounded-(--radius) border border-ring bg-card px-1 h-7 text-[13px] text-foreground"
    />
  );
}

function WorkspaceTabs() {
  const ids = () => { m(); return tabIds(); };
  const current = () => centre();
  return (
    <View class="flex-1 min-w-0 flex-row items-center gap-1" accessibilityLabel="Session tabs" testID="workspace-tabs">
      <Scroll horizontal scrollbar={false} class="flex-1 min-w-0" contentClass="flex-row items-center gap-1 py-1">
        <Each of={ids()} key={(id) => id}>{(id) => <SessionTab id={id()} />}</Each>
        <If when={current() === "new_session" || (current() === "session" && !viewed())}>
          <Button selected onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab" class={`shrink-0 rounded-(--radius) border border-border bg-card px-2 h-8 ${FOCUS}`}>
            <Text class="text-[13px] leading-5 font-semibold text-foreground">New session</Text>
          </Button>
        </If>
        <If when={current() === "settings"}>
          <Button selected onPress={() => run("app.settings")} accessibilityLabel="Settings" testID="settings-tab" class={`shrink-0 rounded-(--radius) border border-border bg-card px-2 h-8 ${FOCUS}`}>
            <Text class="text-[13px] leading-5 font-semibold text-foreground">Settings</Text>
          </Button>
        </If>
      </Scroll>
      <Button onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab-add" class={`shrink-0 rounded-(--radius) px-2 h-8 text-muted-foreground hover:bg-accent ${FOCUS}`}>
        <Text class="text-[17px] leading-5">+</Text>
      </Button>
    </View>
  );
}

export function Header() {
  const conn = () => connection();
  return (
    <View class={`flex-row items-center gap-2 border-b border-border bg-background ${isNarrow() ? "h-14 px-2" : "h-12 px-3"}`} role="toolbar" accessibilityLabel="Workspace header" testID="header">
      <IconButton icon="panel-left" label="Toggle left sidebar" testID="toggle-left" selected={isNarrow() ? drawer() === "left" : leftOpen()} onPress={() => run("sidebar.left.toggle")} reason={key("sidebar.left.toggle") ?? undefined} />
      <WorkspaceTabs />
      <View class="flex-row items-center gap-1 shrink-0">
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
