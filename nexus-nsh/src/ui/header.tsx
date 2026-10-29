import { Scroll, Text, View } from "nsh";
import { Button } from "nsh/base";
import { IconButton, Status, FOCUS, TONE } from "./kit";
import { SidebarTabs, attention } from "./sidebar";
import { byId, closeTab, keyLabel, run, rightIsOpen, rightWidthHint, tabIds } from "../commands";
import { attention as attentionGlyph, statusGlyphs, structure } from "../gen/theme";
import { centre, connection, drawer, isNarrow, leftOpen, m, model, unread, viewed, selectSession, width } from "../store";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * One session tab (D02): 28 px, 6 px radius. The viewed tab sits on the canvas surface with
 * primary text; the others are muted on the chrome band with a hover fill and reveal their ×
 * on hover.
 */
function SessionTab(p: { id: string }) {
  const session = () => { m(); return model.sessions.get(p.id); };
  const title = () => session()?.title || "Untitled";
  const active = () => centre() !== "settings" && centre() !== "new_session" && viewed()?.id === p.id;
  const status = () => {
    m();
    const waiting = attention(p.id) === "waiting" ? attentionGlyph.waiting : null;
    if (waiting) return { glyph: waiting.unicode, tone: TONE[waiting.token], label: "waiting" };
    if (session()?.status === "busy") return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running" };
    if (model.failedSessions.has(p.id)) return { glyph: statusGlyphs.error.unicode, tone: TONE[statusGlyphs.error.token], label: "failed" };
    if (model.unreadSessions.has(p.id)) return { glyph: attentionGlyph.unread.unicode, tone: TONE[attentionGlyph.unread.token], label: "unread" };
    return { glyph: structure.separator.unicode, tone: "text-muted-foreground", label: "idle" };
  };
  const activate = () => {
    if (active()) void run("session.rename");
    else void selectSession(p.id);
  };
  return (
    <View
      class={`group/tab flex-row min-w-0 max-w-[224px] h-7 items-center rounded-(--radius) ${active() ? "bg-background" : "hover:bg-accent"}`}
    >
      <Button
        onPress={activate} selected={active()} accessibilityLabel={`${active() ? "Rename" : "Open"} ${title()}, ${status().label}`}
        testID={`session-tab-${p.id}`} class={`flex-row min-w-0 flex-1 items-center gap-1.5 rounded-l-(--radius) px-2.5 h-7 ${FOCUS}`}
      >
        <Text class={`shrink-0 text-[13px] leading-5 ${status().tone}`} accessibilityLabel={status().label}>{status().glyph}</Text>
        <Text testID={`tab-title-${p.id}`} style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 text-[13px] leading-5 ${active() ? "font-semibold text-foreground" : "text-muted-foreground"}`}>{title()}</Text>
      </Button>
      <Button onPress={() => closeTab(p.id)} accessibilityLabel={`Close ${title()} tab`} testID={`tab-close-${p.id}`} class={`shrink-0 items-center justify-center rounded-r-(--radius) px-1.5 h-7 text-muted-foreground hover:text-foreground ${active() ? "opacity-100" : "opacity-0 group-hover/tab:opacity-100 focus:opacity-100"} ${FOCUS}`}>
        <Text class="text-[15px] leading-5">×</Text>
      </Button>
    </View>
  );
}

/** The session tab strip, on the reading column; the viewed tab is the only one on the canvas surface. */
function WorkspaceTabs() {
  const ids = () => { m(); return tabIds(); };
  const current = () => centre();
  return (
    <View class="flex-1 min-w-0 flex-row items-center gap-1" accessibilityLabel="Session tabs" testID="workspace-tabs">
      <Scroll horizontal scrollbar={false} class="flex-1 min-w-0" contentClass="flex-row items-center gap-1">
        <Each of={ids()} key={(id) => id}>{(id) => <SessionTab id={id()} />}</Each>
        <If when={current() === "new_session" || (current() === "session" && !viewed())}>
          <Button selected onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab" class={`shrink-0 rounded-(--radius) bg-background px-2.5 h-7 ${FOCUS}`}>
            <Text class="text-[13px] leading-5 font-semibold text-foreground">New session</Text>
          </Button>
        </If>
      </Scroll>
      <Button onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab-add" class={`shrink-0 items-center justify-center rounded-(--radius) w-7 h-7 text-muted-foreground hover:bg-accent ${FOCUS}`}>
        <Text class="text-[17px] leading-5">+</Text>
      </Button>
    </View>
  );
}

/**
 * The 44 px chrome band (56 px narrow). Three slots match the sidebars: 264 px over the left
 * sidebar, the centre column (tabs and run status on the 760 px reading column), and 300 px over
 * the right sidebar when it is docked. Narrow, the sidebars are drawers and the band is 56 px.
 */
export function Header() {
  const conn = () => connection();
  const leftDocked = () => !isNarrow() && leftOpen();
  const rightDocked = () => !isNarrow() && rightIsOpen(width());
  return (
    <View class={`flex-row items-center bg-sidebar ${isNarrow() ? "h-14" : "h-11"}`} role="toolbar" accessibilityLabel="Workspace header" testID="header">
      <View class={`flex-row items-center gap-0.5 shrink-0 px-2 ${leftDocked() ? "w-[264px]" : ""}`}>
        <IconButton icon="panel-left" label="Toggle left sidebar" testID="toggle-left" selected={isNarrow() ? drawer() === "left" : leftOpen()} onPress={() => run("sidebar.left.toggle")} reason={key("sidebar.left.toggle") ?? undefined} />
        <If when={leftDocked()}><SidebarTabs /></If>
      </View>
      <View class="flex-1 min-w-0 h-full flex-row items-center justify-center px-2">
        <View class="flex-1 min-w-0 max-w-[760px] flex-row items-center gap-2">
          <WorkspaceTabs />
          <View class="flex-row items-center gap-2 shrink-0">
            <If when={conn().kind !== "live" && conn().kind !== "connecting"}>
              <View class="flex-row items-center gap-1 pr-1"><Status kind="error" word={false} /><Text class="text-xs leading-4 text-error">{conn().kind === "reconnecting" ? "reconnecting" : conn().kind === "starting" ? "starting" : "offline"}</Text></View>
            </If>
          </View>
        </View>
      </View>
      <View class={`flex-row items-center justify-end gap-1 shrink-0 px-2 ${rightDocked() ? "w-[300px]" : ""}`}>
        <View>
          <IconButton icon="bell" label="Notifications" testID="open-notifications" onPress={() => run("notifications.open")} reason={key("notifications.open") ?? undefined} />
          <If when={unread() > 0}><View class="absolute top-1 right-1 size-2 rounded-full bg-info" accessible={false} /></If>
        </View>
        <IconButton icon="panel-right" label="Toggle right sidebar" testID="toggle-right" selected={isNarrow() ? drawer() === "right" : rightIsOpen(width())} onPress={() => { rightWidthHint.w = width(); void run("sidebar.right.toggle"); }} reason={key("sidebar.right.toggle") ?? undefined} />
      </View>
    </View>
  );
}
