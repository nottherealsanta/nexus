import { Text, View, platform, safeArea, signal } from "nsh";
import { titleBar } from "nsh/desktop";
import { Button } from "nsh/base";
import { Icon, IconButton, Status, FOCUS, TS } from "./kit";
import { RAIL_PX, SidebarTabs, leftSidebarWidth } from "./sidebar";
import { byId, keyLabel, run, rightIsOpen, rightWidthHint, waitItems } from "../commands";
import { attention as attentionGlyph } from "../gen/theme";
import { agentCatalog, centre, config, connection, coreUrl, drawer, isNarrow, leftOpen, m, model, modelCatalog, native, railShown, unread, viewed, rootOf, width } from "../store";
import { agentLabel } from "../agent-color";
import { ManagerHeaderIndicator, managerHeaderIndicatorVisible } from "./manager/stage";
import { call } from "../core/manager";
import { coreEditing } from "./connect";
import { currentAlerts } from "./alerts";
import { rightSidebarWidth, textCellPoints } from "./geometry";

function key(id: string) { const c = byId.get(id); return c ? keyLabel(c) : null; }

/**
 * Header tab geometry: one compact box for every tab — an open session, New session, a parent in
 * the subagent strip and a child there. A fixed width keeps the strip still as titles and states
 * change, and a long title ellipsises inside its tab instead of widening it.
 */
export const tabHeight = () => isNarrow() ? "h-10" : "h-8";
export const tabBox = () => `shrink-0 flex-row items-center rounded-(--radius) ${tabHeight()}`;
/** The title's dedicated cells, after status, padding and the fixed close slot. */
export const tabTitleRoomCells = (widthPx: number) => Math.max(1, Math.round(widthPx / textCellPoints()) - 7);
export const tabTitleOverflow = (titleWidth: number, roomWidth: number) => Math.max(0, titleWidth - roomWidth);

/** Hover marquee timing: a short hold, then the title's overflow at a readable speed. */
export const MARQUEE_HOLD_MS = 600;
export const MARQUEE_SPEED = 60;

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
          <View class="flex-1 min-w-0" testID="workspace-tabs" />
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
