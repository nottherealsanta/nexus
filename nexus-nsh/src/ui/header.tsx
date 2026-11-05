import { Scroll, Text, View, platform, safeArea } from "nsh";
import { titleBar } from "nsh/desktop";
import { Button } from "nsh/base";
import { Icon, IconButton, Spinner, Status, FOCUS, TONE, TS } from "./kit";
import { RAIL_PX, SidebarTabs, attention, leftSidebarWidth } from "./sidebar";
import { byId, closeTab, keyLabel, run, rightIsOpen, rightWidthHint, tabIds, waitItems } from "../commands";
import { attention as attentionGlyph, statusGlyphs } from "../gen/theme";
import { centre, connection, coreUrl, drawer, isNarrow, leftOpen, m, model, native, railShown, touch, unread, viewed, rootOf, selectSession, subagentsOf, ancestorsOf, width } from "../store";
import { agentColor } from "../agent-color";
import { ManagerHeaderIndicator, managerHeaderIndicatorVisible } from "./manager/stage";
import { call } from "../core/manager";
import { coreEditing } from "./connect";
import { currentAlerts } from "./alerts";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * Header tab geometry: one compact box for every tab — an open session, New session, a parent in
 * the subagent strip and a child there. A fixed width keeps the strip still as titles and states
 * change, and a long title ellipsises inside its tab instead of widening it.
 */
const tabWidth = () => isNarrow() ? "w-[176px]" : "w-[208px]";
const tabHeight = () => isNarrow() ? "h-10" : "h-7";
const tabBox = () => `shrink-0 flex-row items-center rounded-(--radius) ${tabHeight()}`;

/**
 * One session tab (D02): the header's fixed tab box (208 × 28 px, 176 × 40 narrow) with square
 * corners. The viewed tab sits on the canvas surface with primary text; the others are muted on
 * the chrome band with a hover fill and reveal their × on hover.
 */
function SessionTab(p: { id: string }) {
  const session = () => { m(); return model.sessions.get(p.id); };
  const title = () => session()?.title || "Untitled";
  const active = () => centre() !== "settings" && centre() !== "new_session" && rootOf(viewed()?.id) === p.id;
  const onViewedSession = () => active() && viewed()?.id === p.id;
  const status = () => {
    m();
    const waiting = attention(p.id) === "waiting" ? attentionGlyph.waiting : null;
    if (waiting) return { glyph: waiting.unicode, tone: TONE[waiting.token], label: "waiting", spin: false };
    if (session()?.status === "busy") return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running", spin: true };
    if (model.failedSessions.has(p.id)) return { glyph: statusGlyphs.error.unicode, tone: TONE[statusGlyphs.error.token], label: "failed", spin: false };
    if (model.unreadSessions.has(p.id)) return { glyph: attentionGlyph.unread.unicode, tone: TONE[attentionGlyph.unread.token], label: "unread", spin: false };
    // An idle tab shows no glyph; the label still says it is idle.
    return { glyph: "", tone: "text-muted-foreground", label: "idle", spin: false };
  };
  const activate = () => {
    if (onViewedSession()) void run("session.rename");
    else void selectSession(p.id);
  };
  return (
    <View
      class={`group/tab min-w-0 overflow-hidden ${tabBox()} ${tabWidth()} ${active() ? "bg-selection" : "hover:bg-accent"}`}
    >
      <Button
        onPress={activate} selected={active()} accessibilityLabel={`${onViewedSession() ? "Rename" : "Open"} ${title()}, ${status().label}`}
        testID={`session-tab-${p.id}`} class={`flex-row min-w-0 flex-1 items-center gap-1.5 rounded-l-(--radius) px-2.5 ${tabHeight()} ${FOCUS}`}
      >
        <If when={!!status().glyph}>
          <If when={status().spin} else={<Text class={`shrink-0 ${TS.base} ${status().tone}`} accessibilityLabel={status().label}>{status().glyph}</Text>}>
            <Spinner class={`shrink-0 ${TS.base} ${status().tone}`} label={status().label} />
          </If>
        </If>
        <Text testID={`tab-title-${p.id}`} style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.base} ${active() ? "font-medium text-foreground" : "text-muted-foreground"}`}>{title()}</Text>
      </Button>
      <Button onPress={() => closeTab(p.id)} accessibilityLabel={`Close ${title()} tab`} testID={`tab-close-${p.id}`} class={`shrink-0 items-center justify-center rounded-r-(--radius) ${isNarrow() ? "h-10 px-2.5" : "h-7 px-1.5"} text-muted-foreground hover:text-foreground ${active() || touch ? "opacity-100" : "opacity-0 group-hover/tab:opacity-100 focus:opacity-100"} ${FOCUS}`}>
        <Icon name="x" size={14} class="tint-muted-foreground" />
      </Button>
    </View>
  );
}

/** The session tab strip; the viewed tab is the only one on the canvas surface. */
function WorkspaceTabs() {
  const ids = () => { m(); return tabIds(); };
  const current = () => centre();
  return (
    <View class="flex-1 min-w-0 flex-row items-center gap-1" accessibilityLabel="Session tabs" testID="workspace-tabs">
      <If when={!isNarrow()} else={<Text testID="narrow-workspace-title" style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${TS.base} font-medium text-foreground`}>{current() === "settings" ? "Settings" : current() === "new_session" ? "New session" : viewed()?.title || "New session"}</Text>}>
      <Scroll horizontal scrollbar={false} class="flex-1 min-w-0">
        <View class="flex-row items-center gap-1">
        <Each of={ids()} key={(id) => id}>{(id) => <SessionTab id={id()} />}</Each>
        <If when={current() === "new_session" || (current() === "session" && !viewed())}>
          <Button selected onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab" class={`${tabBox()} ${tabWidth()} bg-selection px-2.5 ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.base} font-medium text-foreground`}>New session</Text>
          </Button>
        </If>
        <Button onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab-add" class={`shrink-0 items-center justify-center rounded-(--radius) ${isNarrow() ? "size-10" : "size-7"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
          <Icon name="plus" size={14} class="tint-muted-foreground" />
        </Button>
        </View>
      </Scroll>
      </If>
    </View>
  );
}

type ChildState = { glyph: string; tone: string; label: "waiting" | "running" | "failed" | "completed"; spin: boolean };

/** A subagent's own state: waits in its tree first, then its run, failure or finished turn. */
function childState(id: string): ChildState {
  m();
  const session = model.sessions.get(id);
  if (attention(id) === "waiting") return { glyph: attentionGlyph.waiting.unicode, tone: TONE[attentionGlyph.waiting.token], label: "waiting", spin: false };
  if (session?.status === "busy" || model.running.has(id)) return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running", spin: true };
  if (model.failedSessions.has(id) || session?.status === "failed") return { glyph: statusGlyphs.error.unicode, tone: TONE[statusGlyphs.error.token], label: "failed", spin: false };
  return { glyph: statusGlyphs.done.unicode, tone: TONE[statusGlyphs.done.token], label: "completed", spin: false };
}

const capitalize = (text: string) => text ? `${text[0].toUpperCase()}${text.slice(1)}` : "";

/**
 * The subagent strip (TUI parity): shown under the header while a child is viewed. The root and
 * the path down to the parent come first as parent tabs, then the sessions below the root, with
 * the viewed child selected. Every tab uses the header's one fixed size, so the strip reads as a
 * single tab row instead of a breadcrumb beside a loose title. The parent tab is the back
 * control; Esc still goes back to the parent.
 */
export function SubagentStrip() {
  const session = () => { m(); return viewed(); };
  const chain = () => { m(); const current = session(); return current ? ancestorsOf(current.id) : []; };
  const parent = () => { const c = chain(); return c.length > 1 ? c[c.length - 2] : null; };
  const root = () => chain()[0] ?? null;
  const ancestors = () => chain().slice(0, -1);
  const children = () => {
    const r = root();
    if (!r) return [];
    // The path above the viewed child already has its own tabs.
    const above = new Set(ancestors().map((s) => s.id));
    return subagentsOf(r.id).filter((s) => !above.has(s.id));
  };
  return (
    <If when={!!parent()}>
      <View class="w-full bg-background" testID="subagent-strip" role="toolbar" accessibilityLabel="Subagent navigation">
        <View class={`w-full max-w-[760px] mx-auto flex-row items-center gap-1.5 px-2 ${isNarrow() ? "min-h-12" : "min-h-8"} min-w-0`}>
          <Scroll horizontal scrollbar={false} class="flex-1 min-w-0">
            <View class="flex-row items-center gap-1">
            <Each of={ancestors()} key={(ancestor) => ancestor.id}>
              {(ancestor) => {
                const isParent = () => ancestor().id === parent()?.id;
                const isRoot = () => ancestor().id === root()?.id;
                return (
                  <Button
                    onPress={() => void selectSession(ancestor().id)}
                    testID={isParent() ? "subagent-back" : isRoot() ? "subagent-root" : `subagent-crumb-${ancestor().id}`}
                    accessibilityLabel={isParent() ? `Back to ${ancestor().title || "parent session"}` : `Open ${isRoot() ? "root" : "ancestor"} session ${ancestor().title || "Untitled"}`}
                    class={`${tabBox()} ${tabWidth()} gap-1.5 px-2.5 text-muted-foreground hover:bg-accent hover:text-foreground ${FOCUS}`}
                  >
                    <Text testID={isRoot() ? "subagent-root-title" : undefined} style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.base}`}>{ancestor().title || "Untitled"}</Text>
                  </Button>
                );
              }}
            </Each>
            <View class="mx-0.5 h-4 w-px shrink-0 bg-border" accessible={false} />
            <Each of={children()} key={(child) => child.id}>
              {(child) => {
                const state = () => childState(child().id);
                const open = () => session()?.id === child().id;
                return (
                  <Button
                    onPress={() => void selectSession(child().id)} selected={open()}
                    testID={`subagent-${child().id}`}
                    accessibilityLabel={`${capitalize(child().agent)} ${child().title || "Untitled"}, ${state().label}${open() ? ", open" : ""}`}
                    class={`${tabBox()} ${tabWidth()} gap-1.5 px-2.5 ${open() ? "bg-accent" : "hover:bg-accent"} ${FOCUS}`}
                  >
                    <If when={state().spin} else={<Text class={`shrink-0 ${TS.base} ${state().tone}`} accessibilityLabel={state().label}>{state().glyph}</Text>}>
                      <Spinner class={`shrink-0 ${TS.base} ${state().tone}`} label={state().label} />
                    </If>
                    <Text class={`shrink-0 ${TS.base} ${agentColor(child().agent)}`}>{capitalize(child().agent)}</Text>
                    <Text testID={open() ? `subagent-current-${child().id}` : undefined} style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.base} ${open() ? "font-semibold text-foreground" : "text-foreground-secondary"}`}>{child().title || "Untitled"}</Text>
                    <If when={!isNarrow()}>
                      <Text class={`shrink-0 ${TS.base} text-muted-foreground tabular-nums`}>{state().label}</Text>
                    </If>
                  </Button>
                );
              }}
            </Each>
            </View>
          </Scroll>
        </View>
      </View>
    </If>
  );
}

/**
 * The 44 px chrome band (56 px narrow). Three slots match the sidebars: 264 px over the left
 * sidebar, the centre column (tabs and run status), and 300 px over
 * the right sidebar when it is docked. Narrow, the sidebars are drawers and the band is 56 px.
 */
export function Header() {
  const conn = () => connection();
  const leftDocked = () => !isNarrow() && leftOpen();
  // The banner under the header already says it when Core is gone; the header keeps its own mark only
  // for a connection state with no banner (an incompatible Core, a start that another banner outranks).
  const showStatus = () => conn().kind !== "live" && conn().kind !== "connecting" && currentAlerts()[0]?.key !== "conn";
  const rightDocked = () => !isNarrow() && rightIsOpen(width());
  // A hidden macOS title bar keeps its traffic lights in the band: the first control starts after them.
  const lights = () => platform.os === "macos" ? titleBar().left : 0;
  return (
    <View class={`flex-row items-center bg-sidebar border-b border-border ${isNarrow() ? "min-h-14" : "min-h-11"}`} role="toolbar" accessibilityLabel="Workspace header" testID="header"
      {...(platform.os === "macos" ? { desktop: { windowDrag: true } } : {})}
      style={{ paddingTop: safeArea().top, ...(lights() ? { paddingLeft: lights() + 8 } : {}) }}>
      <View class="flex-row items-center gap-0.5 shrink-0" style={leftDocked() ? { width: leftSidebarWidth() + (railShown() ? RAIL_PX : 0) } : {}}>
        <View class="items-center justify-center" style={{ width: railShown() ? RAIL_PX : 48 }}><IconButton icon="panel-left" label="Toggle left sidebar" testID="toggle-left" selected={isNarrow() ? drawer() === "left" : leftOpen()} onPress={() => run("sidebar.left.toggle")} reason={key("sidebar.left.toggle") ?? undefined} /></View>
        <If when={leftDocked()}><View class="mx-1 h-4 w-px shrink-0 bg-border" accessible={false} /><SidebarTabs /></If>
      </View>
      <View class="flex-1 min-w-0 h-full flex-row items-center px-2">
        <View class="flex-1 min-w-0 flex-row items-center gap-2">
          <WorkspaceTabs />
          <View class="flex-row items-center gap-2 shrink-0">
            <If when={conn().kind === "live" && !!viewed() && (m(), waitItems().length > 0)}>
              <View class="flex-row items-center gap-1 pr-1" testID="header-waiting">
                <Status kind="waiting" word={false} />
                <Text class={`${TS.base} text-primary`}>waiting for you</Text>
              </View>
            </If>
            <If when={showStatus()}>
              <View class="flex-row items-center gap-1 pr-1" testID="header-status">
                <Status kind={conn().kind === "starting" ? "running" : "error"} word={false} />
                <Text class={`${TS.base} ${conn().kind === "starting" ? "text-muted-foreground" : "text-error"}`}>{conn().kind === "starting" ? "starting" : conn().kind === "incompatible" ? "incompatible" : conn().kind === "auth_failed" ? "sign-in failed" : conn().kind === "reconnecting" ? "reconnecting" : "offline"}</Text>
              </View>
            </If>
          </View>
        </View>
      </View>
      <View class={`flex-row items-center justify-end gap-1 shrink-0 px-2 ${rightDocked() ? "w-[300px]" : ""}`}>
        <If when={native && platform.os !== "macos"}>
          <Button onPress={() => coreEditing.set(true)} accessibilityLabel={`Core connection, ${coreUrl() ?? ""}`} testID="core-connect"
            class={`shrink-0 items-center justify-center rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "h-7"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
            <Text class={`${TS.base} font-medium`}>Core</Text>
          </Button>
        </If>
        <View>
          <IconButton icon="bell" label="Notifications" testID="open-notifications" onPress={() => run("notifications.open")} reason={key("notifications.open") ?? undefined} />
          <If when={unread() > 0}><View class="absolute top-1 right-1 size-2 rounded-full bg-info" accessible={false} /></If>
        </View>
        <If when={!managerHeaderIndicatorVisible()}>
        <Button onPress={() => void run("manager.header")}  accessibilityLabel="Manager" testID="open-manager-context"
          tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{"Manager: messages, cards and session"}</Text>}
          class={`shrink-0 items-center justify-center rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "h-7"} ${call() ? "text-info" : "text-muted-foreground"} hover:bg-accent ${FOCUS}`}>
          <Text class={`${TS.base} font-medium`}>Manager</Text>
        </Button>
        </If>
        <ManagerHeaderIndicator />
        <IconButton icon="panel-right" label="Toggle right sidebar" testID="toggle-right" selected={isNarrow() ? drawer() === "right" : rightIsOpen(width())} onPress={() => { rightWidthHint.w = width(); void run("sidebar.right.toggle"); }} reason={key("sidebar.right.toggle") ?? undefined} />
      </View>
    </View>
  );
}
