import { Scroll, Text, View, effect, platform, safeArea, signal } from "nsh";
import { titleBar } from "nsh/desktop";
import { Button } from "nsh/base";
import { Icon, IconButton, Spinner, Status, FOCUS, TONE, TS } from "./kit";
import { RAIL_PX, SidebarTabs, attention, leftSidebarWidth } from "./sidebar";
import { byId, closeTab, keyLabel, run, rightIsOpen, rightWidthHint, tabIds, waitItems } from "../commands";
import { attention as attentionGlyph, statusGlyphs } from "../gen/theme";
import { agentCatalog, centre, config, connection, coreUrl, drawer, isNarrow, leftOpen, m, model, modelCatalog, native, railShown, touch, unread, viewed, rootOf, selectSession, width } from "../store";
import { agentLabel } from "../agent-color";
import { ManagerHeaderIndicator, managerHeaderIndicatorVisible } from "./manager/stage";
import { call } from "../core/manager";
import { coreEditing } from "./connect";
import { currentAlerts } from "./alerts";
import { rightSidebarWidth, textCellPoints, workspaceTabCells } from "./geometry";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * Header tab geometry: one compact box for every tab — an open session, New session, a parent in
 * the subagent strip and a child there. A fixed width keeps the strip still as titles and states
 * change, and a long title ellipsises inside its tab instead of widening it.
 */
const tabRoom = signal(0);
const tabWidth = () => {
  m();
  const titles = tabIds().map((id) => ({ title: model.sessions.get(id)?.title || "Untitled", session: true }));
  if (centre() === "new_session" || !viewed()) titles.push({ title: "New session", session: false });
  const unit = textCellPoints();
  return workspaceTabCells(titles, tabRoom() / unit || 160) * unit;
};
const tabHeight = () => isNarrow() ? "h-10" : "h-8";
const tabBox = () => `shrink-0 flex-row items-center rounded-(--radius) ${tabHeight()}`;
/** The title's dedicated cells, after status, padding and the fixed close slot. */
export const tabTitleRoomCells = (widthPx: number) => Math.max(1, Math.round(widthPx / textCellPoints()) - 7);
export const tabTitleOverflow = (titleWidth: number, roomWidth: number) => Math.max(0, titleWidth - roomWidth);

/** Hover marquee timing: a short hold, then the title's overflow at a readable speed. */
const MARQUEE_HOLD_MS = 600;
const MARQUEE_SPEED = 60;

/** How far a hovered title has scrolled after `elapsed` ms, never past its overflow. */
export function marqueeShift(elapsed: number, overflow: number): number {
  if (overflow <= 0) return 0;
  const travel = overflow / MARQUEE_SPEED * 1000;
  const cycle = travel + MARQUEE_HOLD_MS * 2;
  const at = elapsed % cycle;
  if (at < MARQUEE_HOLD_MS) return 0;
  if (at < MARQUEE_HOLD_MS + travel) return (at - MARQUEE_HOLD_MS) / 1000 * MARQUEE_SPEED;
  return overflow;
}

/**
 * One session tab (D02): the header's fixed tab box (208 × 28 px, 176 × 40 narrow) with square
 * corners. The viewed tab sits on the canvas surface with primary text; the others are muted on
 * the chrome band with a hover fill and reveal their × on hover. A title wider than its tab
 * scrolls on hover until its end is visible; reduced motion leaves it still and puts the full
 * title in a tooltip and its accessible name.
 */
function SessionTab(p: { id: string }) {
  const session = () => { m(); return model.sessions.get(p.id); };
  const title = () => session()?.title || "Untitled";
  const hovered = signal(false);
  const shift = signal(0);
  const measuredTitleWidth = signal(0);
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  // The shared tab width leaves seven cells for the status mark, close button and padding.
  const titleRoom = () => tabTitleRoomCells(tabWidth());
  const titleOverflow = () => tabTitleOverflow(measuredTitleWidth(), titleRoom() * textCellPoints());
  effect(() => {
    const overflow = titleOverflow();
    // Touch has no hover to leave behind, and the reduced-motion setting is the
    // shared Nexus animation preference; both keep the title still.
    if (!hovered() || touch || reduced() || overflow <= 0) {
      shift.set(0);
      return;
    }
    const start = Date.now();
    const timer = setInterval(() => shift.set(marqueeShift(Date.now() - start, overflow)), 33);
    return () => clearInterval(timer);
  });
  const active = () => centre() !== "settings" && centre() !== "new_session" && rootOf(viewed()?.id) === p.id;
  const onViewedSession = () => active() && viewed()?.id === p.id;
  const status = () => {
    m();
    const waiting = attention(p.id) === "waiting" ? attentionGlyph.waiting : null;
    if (waiting) return { glyph: waiting.unicode, tone: TONE[waiting.token], label: "waiting", spin: false };
    if (session()?.status === "busy") return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running", spin: true };
    if (model.failedSessions.has(p.id)) return { glyph: statusGlyphs.error.unicode, tone: TONE[statusGlyphs.error.token], label: "failed", spin: false };
    if (model.unreadSessions.has(p.id)) return { glyph: attentionGlyph.unread.unicode, tone: TONE[attentionGlyph.unread.token], label: "unread", spin: false };
    return { glyph: "·", tone: "text-muted-foreground", label: "idle", spin: false };
  };
  const activate = () => {
    if (onViewedSession()) void run("session.rename");
    else void selectSession(p.id);
  };
  return (
    <View
      onMouseEnter={() => { if (!touch) hovered.set(true); }} onMouseLeave={() => { hovered.set(false); shift.set(0); }}
      tooltip={reduced() && !touch && titleOverflow() > 0 ? <Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{title()}</Text> : undefined}
      style={{ width: tabWidth() }} class={`group/tab flex-row min-w-0 overflow-hidden ${tabBox()} ${active() ? "bg-background" : "hover:bg-accent"}`}
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
        <Text accessible={false} onLayout={(b: any) => measuredTitleWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}
          style={{ numberOfLines: 1, opacity: 0 }} class={`absolute ${TS.secondary} ${active() ? "font-medium" : ""}`}>{title()}</Text>
        <View testID={`tab-title-clip-${p.id}`} class="shrink-0 overflow-hidden" style={{ width: titleRoom() * textCellPoints() }}>
          <Text testID={`tab-title-${p.id}`} accessibilityLabel={title()}
            style={{ numberOfLines: 1, width: titleOverflow() > 0 ? measuredTitleWidth() : undefined, transform: shift() ? `translateX(${-shift()}px)` : undefined }}
            class={`${TS.secondary} ${active() ? "font-medium text-foreground" : "text-muted-foreground"}`}>{title()}</Text>
        </View>
      </Button>
      <Button onPress={() => closeTab(p.id)} accessibilityLabel={`Close ${title()} tab`} testID={`tab-close-${p.id}`} class={`w-6 shrink-0 items-center justify-center rounded-r-(--radius) ${isNarrow() ? "h-10" : "h-8"} text-muted-foreground hover:text-foreground ${FOCUS}`}>
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
    <View class="flex-1 min-w-0 flex-row items-center gap-1" accessibilityLabel="Session tabs" testID="workspace-tabs" onLayout={(b: any) => tabRoom.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
      <If when={!isNarrow()} else={<Text testID="narrow-workspace-title" style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${TS.base} font-medium text-foreground`}>{current() === "settings" ? "Settings" : current() === "new_session" ? "New session" : viewed()?.title || "New session"}</Text>}>
      <Scroll horizontal scrollbar={false} class="flex-1 min-w-0">
        <View class="flex-row items-center gap-1">
        <Each of={ids()} key={(id) => id}>{(id) => <SessionTab id={id()} />}</Each>
        <If when={current() === "new_session" || (current() === "session" && !viewed())}>
          <Button selected onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab" style={{ width: tabWidth() }} class={`${tabBox()} bg-background px-2.5 ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.secondary} font-medium text-foreground`}>New session</Text>
          </Button>
        </If>
        <Button onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab-add" class={`shrink-0 items-center justify-center rounded-(--radius) ${isNarrow() ? "size-10" : "size-8"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
          <Icon name="plus" size={14} class="tint-muted-foreground" />
        </Button>
        </View>
      </Scroll>
      </If>
    </View>
  );
}

/** The root-back strip is thicker than a tab: four text cells, full height. */
export const SESSION_BACK_CELLS = 4;
export const sessionBackRailPx = () => textCellPoints() * SESSION_BACK_CELLS;

/**
 * A viewed subagent's full-height root-back strip: four text cells of muted surface at the
 * centre's far left, beside the sidebar, with the arrow centred in it. The whole strip jumps
 * straight to the root (Esc still goes to the parent).
 */
export function SessionBackRail() {
  const rootTitle = () => { m(); return model.sessions.get(rootOf(viewed()?.id) ?? "")?.title || "Untitled"; };
  return (
    <View class="absolute left-0 top-0 bottom-0 bg-raised" style={{ width: sessionBackRailPx() }} testID="session-back-rail">
      <Button
        onPress={() => void run("nav.root")} testID="session-back"
        accessibilityLabel={`Back to root session ${rootTitle()}`}
        tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{`Back to ${rootTitle()}`}</Text>}
        class={`w-full h-full items-center ${FOCUS}`}
      >
        <View class="w-full h-full items-center justify-center" testID="session-back-arrow">
          <Icon name="arrow-left" size={14} class="tint-muted-foreground" />
        </View>
      </Button>
    </View>
  );
}

/**
 * The first row of an open session's page. A root session keeps it blank: the full title is
 * already the active tab. A viewed subagent is named here, shifted right by the full-height
 * root-back strip.
 */
const capitalize = (text: string) => text ? `${text[0].toUpperCase()}${text.slice(1)}` : "";
const headingCells = (text: string) => Array.from(text).length;

/** Shorten to `room` cells, with an ellipsis when it does not fit. */
function headingEllipsis(text: string, room: number): string {
  const chars = Array.from(text);
  if (chars.length <= room) return text;
  if (room <= 0) return "";
  if (room === 1) return "…";
  return `${chars.slice(0, room - 1).join("")}…`;
}

/**
 * Join the heading metadata in stable order: spaced separators, then compact ones, then each
 * field shortened to an equal share. A row too narrow for four fields falls back to one
 * ellipsised line.
 */
/** Metadata room after reserving a readable title share (40%, at least 14 cells). */
export function headingMetaRoom(cells: number): number {
  return Math.max(0, cells - Math.max(14, Math.floor(cells * 0.4)) - 1);
}

export function headingMeta(parts: string[], room: number): string {
  if (room <= 0 || !parts.length) return "";
  const full = parts.join(" · ");
  if (headingCells(full) <= room) return full;
  const tight = parts.join("·");
  if (headingCells(tight) <= room) return tight;
  const budget = room - (parts.length - 1);
  if (budget < parts.length) return headingEllipsis(tight, room);
  const base = Math.floor(budget / parts.length);
  const extra = budget % parts.length;
  return parts.map((p, i) => headingEllipsis(p, base + (i < extra ? 1 : 0))).join("·");
}

/** The viewed child's four metadata values, using the composer's catalog/ref fallbacks. */
function headingMetaFields(): string[] {
  m();
  const s = viewed();
  if (!s) return [];
  const agent = capitalize(agentLabel(s));
  const ref = s.model
    ? String(s.model)
    : agentCatalog().agents.find((a) => a.name === s.agent)?.model || "";
  let name = "default";
  let provider = "default";
  if (ref) {
    const entry = modelCatalog().find((item) => item.ref === ref);
    name = entry?.name || ref.split("/").at(-1) || ref || "default";
    provider = entry?.provider || (ref.includes("/") ? ref.split("/")[0] : "") || "default";
  }
  const effort = (s as any).effort || (s as any).extra?.effort || "default";
  return [agent, name, provider, effort];
}

export function SessionHeading() {
  const current = () => { m(); return viewed(); };
  const room = signal(0);
  const shown = () => !isNarrow() && centre() === "session" && !!current();
  const meta = () => headingMeta(headingMetaFields(), headingMetaRoom(Math.floor(room() / textCellPoints())));
  return (
    <If when={shown()}>
      <View class="w-full h-7 shrink-0 flex-row items-center bg-background pr-3" testID="session-heading"
        onLayout={(b: any) => room.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
        <If when={!!current()?.parent_id}>
          <Text testID="session-heading-title" style={{ numberOfLines: 1, textOverflow: "ellipsis" }} class={`min-w-0 flex-1 ${TS.base} text-foreground-secondary`}>
            {current()?.title || "Untitled"}
          </Text>
          <If when={!!meta()}>
            <Text testID="session-heading-meta" style={{ numberOfLines: 1, textOverflow: "ellipsis" }} class={`ml-2 shrink min-w-0 ${TS.base} text-muted-foreground`}>{meta()}</Text>
          </If>
        </If>
      </View>
    </If>
  );
}

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
      <View class="flex-row items-center justify-end gap-1 shrink-0 px-2" style={rightDocked() ? { width: rightSidebarWidth(width()) } : {}}>
        <If when={native && platform.os !== "macos"}>
          <Button onPress={() => coreEditing.set(true)} accessibilityLabel={`Core connection, ${coreUrl() ?? ""}`} testID="core-connect"
            class={`shrink-0 items-center justify-center rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "h-7"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
            <Text class={`${TS.base} font-medium`}>Core</Text>
          </Button>
        </If>
        <If when={unread() > 0}>
          <Button onPress={() => run("notifications.open")} accessibilityLabel={`Notifications, ${unread()} unread`} testID="open-notifications" class={`shrink-0 px-2 justify-center ${isNarrow() ? "min-h-11" : "h-7"} hover:bg-accent ${FOCUS}`}>
            <Text class={`${TS.base} text-info`}>{`${attentionGlyph.unread.unicode} ${unread()}`}</Text>
          </Button>
        </If>
        <If when={config()?.values?.manager?.enabled !== false}>
        <If when={!managerHeaderIndicatorVisible()}>
        <Button onPress={() => void run("manager.header")}  accessibilityLabel="Manager" testID="open-manager-context"
          tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{"Manager: messages, cards and session"}</Text>}
          class={`shrink-0 items-center justify-center rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "h-7"} ${call() ? "text-info" : "text-muted-foreground"} hover:bg-accent ${FOCUS}`}>
          <Text class={`${TS.base} font-medium`}>Manager</Text>
        </Button>
        </If>
        <ManagerHeaderIndicator />
        </If>
        <IconButton icon="panel-right" label="Toggle right sidebar" testID="toggle-right" selected={isNarrow() ? drawer() === "right" : rightIsOpen(width())} onPress={() => { rightWidthHint.w = width(); void run("sidebar.right.toggle"); }} reason={key("sidebar.right.toggle") ?? undefined} />
      </View>
    </View>
  );
}
