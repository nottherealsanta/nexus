// The tab area (plan 45, docs/layout.md): groups of tabs and the dividers between them, between the two sidebars.
// Every group is an absolutely placed box (its place comes from the layout tree and the measured area), and terminal
// screens live in one flat layer keyed by terminal, so moving a terminal tab between groups moves a mounted
// WebView instead of remounting it: the attachment and the screen survive the move.
import { PanResponder, Scroll, Text, View, effect, signal, type ViewRef } from "nsh";
import { Button, ContextMenu, MenuItem } from "nsh/base";
import { Icon, Spinner, FOCUS, TONE, TS, T } from "./kit";
import { attention } from "./sidebar";
import { MARQUEE_HOLD_MS, MARQUEE_SPEED, marqueeShift, tabBox, tabHeight, tabTitleOverflow, tabTitleRoomCells } from "./header";
import { FilePane, DiffPane } from "./editor";
import { SessionPane } from "./centre";
import { PeerTimeline } from "./timeline";
import { PeerComposer } from "./composer";
import { TerminalPane } from "./terminal";
import { byId, keyLabel, run } from "../commands";
import { attention as attentionGlyph, statusGlyphs } from "../gen/theme";
import { uiText } from "../gen/manifest";
import { config, isNarrow, m, model, rootOf, touch, viewed } from "../store";
import { textCellPoints, workspaceTabCells } from "./geometry";
import { terminalItems } from "../terminal-panel";
import { dividers, dropZone, tabKey, zoneRect, type Rect, type Tab, type Zone } from "../layout";
import {
  bodyOf, canJoinDown, canJoinRight, closeTabAt, dropOnBody, dropOnStrip, focusedGroupId, focusGroup, groupIds, groupOf, hiddenGroups, layout, layoutDrag, layoutNow,
  layoutSize, newTabShown, newTerminalTab, activateTab, registerStrip, resizeDivider, sessionOwner, shownRects, splitFocused, joinFocused, tabDirty, tabTitle, collapsedNow, layoutEnabled,
  groupMenuFor, pointerMods, pressMods, trackMods, type DragTarget,
} from "../layout-state";

/** A group's tab strip height, in points (the existing tab box, h-8). */
export const STRIP_H = 32;
const key = (id: string) => { const c = byId.get(id); return c ? keyLabel(c) : null; };
const origin = signal({ x: 0, y: 0 });

// --- one tab ------------------------------------------------------------------------------------------------------

function tabStatus(tab: Tab) {
  m();
  if (tab.kind === "session") {
    const session = model.sessions.get(tab.id);
    const waiting = attention(tab.id) === "waiting" ? attentionGlyph.waiting : null;
    if (waiting) return { glyph: waiting.unicode, tone: TONE[waiting.token], label: "waiting", spin: false };
    if (session?.status === "busy") return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running", spin: true };
    if (model.failedSessions.has(tab.id)) return { glyph: statusGlyphs.error.unicode, tone: TONE[statusGlyphs.error.token], label: "failed", spin: false };
    if (model.unreadSessions.has(tab.id)) return { glyph: attentionGlyph.unread.unicode, tone: TONE[attentionGlyph.unread.token], label: "unread", spin: false };
    return { glyph: "·", tone: "text-muted-foreground", label: "idle", spin: false };
  }
  if (tab.kind === "terminal") {
    const running = terminalItems().find((t) => t.id === tab.id)?.status === "running";
    return { glyph: running ? "●" : "○", tone: running ? "text-success" : "text-muted-foreground", label: running ? "running" : "exited", spin: false };
  }
  if (tabDirty(tab)) return { glyph: "●", tone: "text-warning", label: "unsaved changes", spin: false };
  return { glyph: "", tone: "text-muted-foreground", label: "", spin: false };
}

function GroupTab(p: { group: string; tab: Tab; width: number }) {
  const tab = p.tab;
  const k = tabKey(tab);
  const title = () => tabTitle(tab);
  const hovered = signal(false);
  const shift = signal(0);
  const measured = signal(0);
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  const titleRoom = () => tabTitleRoomCells(p.width);
  const overflow = () => tabTitleOverflow(measured(), titleRoom() * textCellPoints());
  effect(() => {
    const o = overflow();
    if (!hovered() || touch || reduced() || o <= 0) { shift.set(0); return; }
    const start = Date.now();
    const timer = setInterval(() => shift.set(marqueeShift(Date.now() - start, o)), 33);
    return () => clearInterval(timer);
  });
  const group = () => groupOf(p.group);
  const active = () => { layout(); return !newTabShown() || p.group !== focusedGroupId() ? group()?.active === k : false; };
  const focusedGroupActive = () => active() && p.group === focusedGroupId();
  const status = () => tabStatus(tab);
  const viewedSession = () => tab.kind === "session" && rootOf(viewed()?.id) === tab.id;
  const id = tab.kind === "session" ? tab.id : k;
  let lastPress = 0;
  const press = () => {
    const now = Date.now();
    // A press on the tab that is already the viewed session renames it; a quick second press on a terminal renames it too.
    if (tab.kind === "session" && active() && viewedSession() && p.group === focusedGroupId()) { void run("session.rename"); return; }
    if (tab.kind === "terminal" && active() && now - lastPress < 400) { void import("../terminal-panel").then((t) => { t.focusTerminal(tab.id); t.beginRenameTerminal(tab.id); }); }
    lastPress = now;
    activateTab(p.group, k);
  };
  // Dragging a tab: past a few points of travel the tab is lifted; the strip or body under the pointer is the target.
  const pan = PanResponder.create({
    onStartShouldSetPanResponder: () => false,
    onMoveShouldSetPanResponder: (_e, g) => Math.abs(g.dx) + Math.abs(g.dy) > 6,
    onMoveShouldSetPanResponderCapture: (_e, g) => Math.abs(g.dx) + Math.abs(g.dy) > 6,
    onPanResponderGrant: (e: any, g) => {
      activateTab(p.group, k);
      layoutDrag.set({ from: p.group, key: k, x: g.moveX - origin().x, y: g.moveY - origin().y, copy: false, target: null });
      refreshOrigin();
    },
    onPanResponderMove: (e: any, g) => {
      const d = layoutDrag();
      if (!d || d.key !== k) return;
      const x = g.moveX - origin().x, y = g.moveY - origin().y;
      // Alt at the press or on the pointer's latest move (a hosted view's pan carries no modifiers).
      const copy = pointerMods.alt;
      layoutDrag.set({ ...d, x, y, copy, target: targetAt(x, y, p.width) });
    },
    onPanResponderRelease: () => finishDrag(),
    onPanResponderTerminate: () => layoutDrag.set(null),
  });
  return (
    <View
      onMouseEnter={() => { if (!touch) hovered.set(true); }} onMouseLeave={() => { hovered.set(false); shift.set(0); }}
      tooltip={reduced() && !touch && overflow() > 0 ? <Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{title()}</Text> : undefined}
      style={{ width: p.width, opacity: layoutDrag()?.key === k && layoutDrag()?.from === p.group ? 0.5 : 1 }}
      class={`group/tab flex-row min-w-0 overflow-hidden ${tabBox()} ${active() ? "bg-background" : "hover:bg-accent"} ${focusedGroupActive() && multiple() ? "border-b-2 border-primary" : ""}`}
    >
      <View role="button" focusable {...pan.panHandlers} panActivation={{ activeOffsetX: 6, activeOffsetY: 6 }} {...pressMods}
        onPress={press} accessibilityState={{ selected: active() }} accessibilityLabel={`${viewedSession() && active() ? "Rename" : "Open"} ${title()}${status().label ? `, ${status().label}` : ""}`}
        testID={tab.kind === "session" ? `session-tab-${tab.id}` : `tab-${k}`}
        class={`flex-row min-w-0 flex-1 items-center gap-1.5 rounded-l-(--radius) px-2.5 ${tabHeight()} ${FOCUS}`}
      >
        <If when={!!status().glyph}>
          <If when={status().spin} else={<Text class={`shrink-0 ${TS.base} ${status().tone}`} accessibilityLabel={status().label}>{status().glyph}</Text>}>
            <Spinner class={`shrink-0 ${TS.base} ${status().tone}`} label={status().label} />
          </If>
        </If>
        <Text accessible={false} onLayout={(b: any) => measured.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}
          style={{ numberOfLines: 1, opacity: 0 }} class={`absolute ${TS.secondary} ${active() ? "font-medium" : ""}`}>{title()}</Text>
        <View testID={`tab-title-clip-${id}`} class="shrink-0 overflow-hidden" style={{ width: titleRoom() * textCellPoints() }}>
          <Text testID={`tab-title-${id}`} accessibilityLabel={title()}
            style={{ numberOfLines: 1, width: overflow() > 0 ? measured() : undefined, transform: shift() ? `translateX(${-shift()}px)` : undefined }}
            class={`${TS.secondary} ${active() ? "font-medium text-foreground" : "text-muted-foreground"}`}>{title()}</Text>
        </View>
      </View>
      <Button onPress={() => closeTabAt(p.group, k)} accessibilityLabel={`Close ${title()} tab`} testID={`tab-close-${id}`}
        class={`w-6 shrink-0 items-center justify-center rounded-r-(--radius) ${tabHeight()} text-muted-foreground hover:text-foreground ${FOCUS}`}>
        <Icon name="x" size={14} class="tint-muted-foreground" />
      </Button>
    </View>
  );
}
const multiple = () => { layout(); return groupIds().length > 1; };

// --- drag targets ---------------------------------------------------------------------------------------------------

let areaRef: ViewRef | undefined;
function refreshOrigin() {
  void areaRef?.measure().then((m2: any) => { if (m2?.pagePosition) origin.set({ x: m2.pagePosition.x, y: m2.pagePosition.y }); });
}
/** What a point of the layout area would drop on: a strip (at an index) or a body zone. */
function targetAt(x: number, y: number, tabW: number): DragTarget {
  for (const [gid, r] of shownRects()) {
    if (x < r.x || x >= r.x + r.w || y < r.y || y >= r.y + r.h) continue;
    if (y < r.y + STRIP_H) {
      const count = groupOf(gid)?.tabs.length ?? 0;
      const index = Math.max(0, Math.min(count, Math.round((x - r.x) / Math.max(1, tabW))));
      return { kind: "strip", group: gid, index };
    }
    return { kind: "body", group: gid, zone: dropZone({ x: r.x, y: r.y + STRIP_H, w: r.w, h: r.h - STRIP_H }, x, y) };
  }
  return null;
}
function finishDrag() {
  const d = layoutDrag();
  layoutDrag.set(null);
  if (!d?.target) return;
  if (d.target.kind === "strip") dropOnStrip(d.from, d.key, d.target.group, d.target.index, d.copy);
  else dropOnBody(d.from, d.key, d.target.group, d.target.zone, d.copy);
}

// --- strip ----------------------------------------------------------------------------------------------------------

function GroupMenu(p: { group: string }) {
  const open = () => groupMenuFor() === p.group;
  const setOpen = (v: boolean) => groupMenuFor.set(v ? p.group : groupMenuFor() === p.group ? null : groupMenuFor());
  const item = (testID: string, label: string, onSelect: () => void, hint?: string | null) => (
    <MenuItem onSelect={onSelect} testID={testID} class="rounded-(--radius) px-2 py-1.5 hover:bg-accent">
      <View class="flex-row items-center gap-2"><Text class={`flex-1 ${TS.base} text-popover-foreground`}>{label}</Text><Text class={T.meta}>{hint ?? ""}</Text></View>
    </MenuItem>
  );
  const here = (fn: () => void) => () => { focusGroup(p.group); fn(); };
  return (
    <ContextMenu open={open()} onOpenChange={({ open: o }) => setOpen(o)} popupClass="min-w-[188px] rounded-(--radius-card) bg-popover p-1 shadow-lg"
      trigger={
        <Button onPress={() => { focusGroup(p.group); setOpen(!open()); }} accessibilityLabel={uiText("layout.menu.tooltip")} testID={`group-menu-${p.group}`}
          class={`shrink-0 items-center justify-center rounded-(--radius) ${isNarrow() ? "size-10" : "size-8"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
          <Text class={`${TS.base} text-muted-foreground`}>⋯</Text>
        </Button>
      }>
      {item("group-menu-split-right", uiText("layout.menu.split_right"), here(() => void splitFocused("right")), key("layout.split_right"))}
      {item("group-menu-split-down", uiText("layout.menu.split_down"), here(() => void splitFocused("down")), key("layout.split_down"))}
      <If when={canJoinRight()}>{item("group-menu-join-right", uiText("layout.menu.join_right"), here(() => joinFocused("right")), key("layout.join_right"))}</If>
      <If when={canJoinDown()}>{item("group-menu-join-down", uiText("layout.menu.join_down"), here(() => joinFocused("down")), key("layout.join_down"))}</If>
      {item("group-menu-new-terminal", uiText("layout.terminal.split"), here(() => void newTerminalTab()), null)}
      {item("group-menu-close-tab", uiText("layout.menu.close_tab"), here(() => void run("layout.tab_close")), key("layout.tab_close"))}
      <If when={multiple()}>{item("group-menu-close-group", uiText("layout.menu.close_group"), here(() => void run("layout.group_close")), key("layout.group_close"))}</If>
    </ContextMenu>
  );
}

function GroupStrip(p: { id: string; width: () => number }) {
  const room = signal(0);
  let strip: ViewRef | undefined;
  const g = () => groupOf(p.id);
  const titles = () => { m(); return (g()?.tabs ?? []).map((t) => ({ title: tabTitle(t), session: true })); };
  const tabW = () => { const unit = textCellPoints(); return workspaceTabCells(titles(), room() / unit || 160) * unit; };
  registerStrip(p.id, () => strip?.focus());
  effect(() => () => registerStrip(p.id, null));
  const dropMark = () => { const d = layoutDrag(); return d?.target?.kind === "strip" && d.target.group === p.id ? d.target.index : -1; };
  return (
    <View ref={(r: ViewRef) => { strip = r; }} focusable class="flex-row items-center bg-sidebar border-b border-border shrink-0" style={{ height: STRIP_H + 1 }} role="tablist" accessibilityLabel="Group tabs" testID={`group-strip-${p.id}`}>
      <View class="flex-1 min-w-0 flex-row items-center gap-1" onLayout={(b: any) => room.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
        <Scroll horizontal scrollbar={false} class="flex-1 min-w-0">
          <View class="flex-row items-center gap-1">
            <Each of={g()?.tabs ?? []} key={(t) => tabKey(t)}>{(t, i) =>
              <View class="flex-row items-center">
                <If when={dropMark() === i()}><View class="w-0.5 h-6 bg-primary" testID="drop-marker" /></If>
                <GroupTab group={p.id} tab={t()} width={tabW()} />
              </View>}
            </Each>
            <If when={newTabShown() && p.id === focusedGroupId()}>
              <Button selected onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab" style={{ width: tabW() }} class={`${tabBox()} bg-background px-2.5 ${FOCUS}`}>
                <Text style={{ numberOfLines: 1 }} class={`min-w-0 flex-1 ${TS.secondary} font-medium text-foreground`}>New session</Text>
              </Button>
            </If>
            <If when={p.id === focusedGroupId()}>
              <Button onPress={() => run("session.new")} accessibilityLabel="New session" testID="new-session-tab-add" class={`shrink-0 items-center justify-center rounded-(--radius) ${isNarrow() ? "size-10" : "size-8"} text-muted-foreground hover:bg-accent ${FOCUS}`}>
                <Icon name="plus" size={14} class="tint-muted-foreground" />
              </Button>
            </If>
          </View>
        </Scroll>
      </View>
      <If when={p.id === focusedGroupId() && hiddenGroups() > 0}>
        <Text class={`${T.meta} px-2`} testID="hidden-groups" accessibilityLabel={uiText("layout.hidden_groups.hint")}>{hiddenGroups() === 1 ? uiText("layout.hidden_groups.one") : uiText("layout.hidden_groups.many", { count: hiddenGroups() })}</Text>
      </If>
      <GroupMenu group={p.id} />
    </View>
  );
}

// --- body ---------------------------------------------------------------------------------------------------------

function GroupBody(p: { id: string }) {
  const shown = () => bodyOf(p.id);
  const kind = () => { const b = shown(); return b === "new" ? "new" : b?.kind; };
  const tab = () => { const b = shown(); return b && b !== "new" ? b : null; };
  const live = () => sessionOwner() === p.id;
  return (
    <View class="flex-1 min-h-0" testID={`group-body-${p.id}`}>
      <If when={kind() === "new" || (kind() === "session" && live())}><SessionPane /></If>
      {/* Another session (or the same one again): its own live timeline and composer; a press there makes it the viewed session. */}
      <If when={kind() === "session" && !live()}>
        <View class="flex-1 min-h-0 flex-col bg-background">
          <PeerTimeline sid={tab()?.id ?? ""} onFocus={() => activateTab(p.id, tabKey(tab()!))} />
          <If when={(m().sessions.get(tab()?.id ?? "") as any)?.kind !== "manager"}>
            <PeerComposer sid={tab()?.id ?? ""} onFocus={() => activateTab(p.id, tabKey(tab()!))} />
          </If>
        </View>
      </If>
      <If when={kind() === "file"}><FilePane id={tab()?.id ?? ""} group={p.id} /></If>
      <If when={kind() === "diff"}><DiffPane id={tab()?.id ?? ""} /></If>
      <If when={kind() === "browser"}><View class="flex-1 items-center justify-center"><Text class={T.sec}>The browser is not available yet.</Text></View></If>
      <If when={!kind()}><View class="flex-1 items-center justify-center" testID="group-empty"><Text class={T.meta}>No open tabs</Text></View></If>
    </View>
  );
}

function GroupBox(p: { id: string }) {
  const r = (): Rect => shownRects().get(p.id) ?? { x: 0, y: 0, w: 0, h: 0 };
  return (
    <View style={{ position: "absolute", left: r().x, top: r().y, width: r().w, height: r().h }} class="flex-col bg-background min-w-0 min-h-0 overflow-hidden" testID={`group-${p.id}`}
      onTouchStart={() => { if (focusedGroupId() !== p.id) focusGroup(p.id); }}>
      <GroupStrip id={p.id} width={() => r().w} />
      <GroupBody id={p.id} />
      {/* Which group holds the focus (and the tab highlight), for tests and assistive tech. */}
      <If when={focusedGroupId() === p.id}><View testID={`group-focused-${p.id}`} accessible={false} class="absolute w-px h-px" style={{ pointerEvents: "none" }} /></If>
    </View>
  );
}

// --- terminals (one flat layer, keyed by terminal) ----------------------------------------------------------------------

/** Terminals shown right now: the active terminal tab of each drawn group, with where it goes. */
function visibleTerminals(): { id: string; group: string }[] {
  layout();
  const out: { id: string; group: string }[] = [];
  for (const [gid] of shownRects()) {
    const b = bodyOf(gid);
    if (b && b !== "new" && b.kind === "terminal") out.push({ id: b.id, group: gid });
  }
  return out;
}
function TerminalLayer() {
  return (
    <Each of={visibleTerminals()} key={(t) => t.id}>{(t) => {
      const r = (): Rect => shownRects().get(visibleTerminals().find((x) => x.id === t().id)?.group ?? "") ?? { x: 0, y: 0, w: 0, h: 0 };
      return (
        <View style={{ position: "absolute", left: r().x, top: r().y + STRIP_H + 1, width: r().w, height: Math.max(0, r().h - STRIP_H - 1), pointerEvents: layoutDrag() ? "none" : "auto" }} class="bg-background" testID={`terminal-slot-${t().id}`}
          onTouchStart={() => { const g = visibleTerminals().find((x) => x.id === t().id)?.group; if (g && focusedGroupId() !== g) focusGroup(g); }}>
          <TerminalPane id={t().id} />
        </View>
      );
    }}</Each>
  );
}

// --- dividers and drop feedback -------------------------------------------------------------------------------------

function Dividers() {
  const list = () => { layout(); const s = layoutSize(); return collapsedNow() || s.w <= 0 ? [] : dividers(layoutNow().root, { x: 0, y: 0, w: s.w, h: s.h }); };
  return (
    <Each of={list()} key={(d) => `${d.path.join(".")}/${d.index}`}>{(d) => {
      let base = layoutNow();
      const pan = PanResponder.create({
        onStartShouldSetPanResponder: () => true,
        onPanResponderGrant: () => { base = layoutNow(); },
        onPanResponderMove: (_e, g) => resizeDivider(d().path, d().index, d().dir === "row" ? g.dx : g.dy, base),
      });
      const row = () => d().dir === "row";
      return <View {...pan.panHandlers} style={{ position: "absolute", left: d().rect.x, top: d().rect.y, width: d().rect.w, height: d().rect.h, cursor: row() ? "col-resize" : "row-resize" }}
        class="z-30 hover:bg-primary" testID={`divider-${d().path.join("-")}-${d().index}`} accessibilityLabel="Resize groups">
        <View class={`absolute bg-border ${row() ? "left-[2px] top-0 bottom-0 w-px" : "top-[2px] left-0 right-0 h-px"}`} />
      </View>;
    }}</Each>
  );
}

function DropFeedback() {
  const target = () => layoutDrag()?.target ?? null;
  const zone = (): { r: Rect; z: Zone } | null => {
    const t = target();
    if (!t || t.kind !== "body") return null;
    const r = shownRects().get(t.group);
    if (!r) return null;
    return { r: zoneRect({ x: r.x, y: r.y + STRIP_H, w: r.w, h: r.h - STRIP_H }, t.zone), z: t.zone };
  };
  const label = () => { const d = layoutDrag(); const g = d && groupOf(d.from); const t = g?.tabs.find((x) => tabKey(x) === d?.key); return t ? tabTitle(t) : ""; };
  return (
    <If when={!!layoutDrag()}>
      <View class="absolute inset-0 z-50" style={{ pointerEvents: "none" }} testID="drag-layer">
        <If when={!!zone()}>
          <View testID={`drop-zone-${zone()?.z}`} style={{ position: "absolute", left: zone()?.r.x ?? 0, top: zone()?.r.y ?? 0, width: zone()?.r.w ?? 0, height: zone()?.r.h ?? 0 }} class="bg-selection border-2 border-primary rounded-(--radius)" />
        </If>
        <View style={{ position: "absolute", left: (layoutDrag()?.x ?? 0) + 8, top: (layoutDrag()?.y ?? 0) + 8 }} class="bg-popover rounded-(--radius) px-2 py-1 shadow-lg" testID="drag-ghost">
          <Text class={`${TS.base} text-popover-foreground`}>{`${layoutDrag()?.copy ? "+ " : ""}${label()}`}</Text>
        </View>
      </View>
    </If>
  );
}

export function Groups() {
  if (!layoutEnabled) return <View />;
  return (
    <View ref={(r: ViewRef) => { areaRef = r; }} class="flex-1 min-w-0 min-h-0 relative bg-background" testID="groups" {...trackMods}
      onLayout={(b: any) => { const w = b.bounds?.size?.width ?? b.size?.width ?? 0, h = b.bounds?.size?.height ?? b.size?.height ?? 0; layoutSize.set({ w, h }); refreshOrigin(); }}>
      <Each of={groupIds()} key={(id) => id}>{(id) => <If when={shownRects().has(id())}><GroupBox id={id()} /></If>}</Each>
      <TerminalLayer />
      <Dividers />
      <DropFeedback />
    </View>
  );
}
