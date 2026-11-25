// The phone client's shell (plans/05 sections 5.1 and 5.2): Sessions, Manager and Settings tabs, a conversation with a
// compact header, a back control to Sessions and a left drawer of sessions. NSH phone idiom only; AGENTS.md "Mobile is a
// cut-down client".
import { Keyboard, PanResponder, Text, View, effect, keyboardFrame, safeArea, untracked, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Icon, IconButton, FOCUS, T, TS, Spinner } from "../kit";
import { Centre } from "../centre";
import { PhoneSessionsPane } from "../sidebar";
import { ManagerTab, ManagerWave, managerHeaderIndicatorVisible } from "../manager/stage";
import { Drawers } from "../overlays";
import { card, closeManagerDropdown, consent, dropdownOpen, showManagerPanel } from "../../core/manager";
import { PHONE_TABS, phoneShellBottomInset, phoneStatus, phoneTabBarVisible } from "../../mobile";
import { attention } from "../sidebar";
import { currentProject, m, model, phonePage, tick, viewed } from "../../store";
import { fmtDuration } from "../timeline";
import { PhoneSettings } from "./settings";
import { PhoneSheets } from "./sheets";
import { goBack, phoneParent, rowSheet, settingsDrill } from "./state";

/** A large title for the Manager and Settings tabs, as an iOS navigation bar. */
function TabTitle(p: { title: string }) {
  return (
    <View class="pl-5 pr-4 pb-2 bg-background" style={{ paddingTop: safeArea().top + 6 }} testID="phone-top-bar">
      <View class="flex-row items-center justify-between gap-3 min-h-11">
        <Text style={{ numberOfLines: 1 }} testID="phone-tab-title" class="font-(family-name:--font-ui) text-[34px] leading-[41px] font-bold tracking-[-0.4px] text-foreground">{p.title}</Text>
        <If when={p.title === "Manager" && managerHeaderIndicatorVisible()}>{() => <View style={{ width: 100, height: 20 }} testID="manager-tab-wave" accessible={false}><ManagerWave /></View>}</If>
      </View>
    </View>
  );
}

/**
 * A conversation's header: an icon-only back chevron to Sessions, the title with its project and live status below, a
 * status ring and the overflow menu. The root tab bar is hidden here, so Back is the way out; the session drawer stays
 * available by its left-edge swipe.
 */
function ConversationHeader() {
  const s = () => { m(); return viewed(); };
  const status = () => {
    m();
    const v = viewed();
    return v ? phoneStatus({ waiting: attention(v.id) === "waiting", running: model.tree(v.id).some((id) => model.running.has(id)) || v.status === "busy", archived: !!v.archived }) : null;
  };
  const running = () => status()?.tone === "info";
  const elapsed = () => {
    m(); tick();
    const msgs = model.selected?.messages ?? [];
    const u = [...msgs].reverse().find((x) => x.role === "user");
    return u ? Math.max(0, Date.now() / 1000 - u.created) : 0;
  };
  // The timer lives in the header now; tick once a second only while this session runs.
  effect(() => {
    if (!running()) return;
    const id = setInterval(() => tick.set(Date.now()), 1000);
    return () => clearInterval(id);
  });
  const project = () => { m(); const id = s()?.project_id ?? currentProject(); return id ? model.projects.get(id)?.name ?? "" : ""; };
  return (
    <View class="flex-row items-center gap-1 px-1 pb-1.5 bg-background border-b border-border" style={{ paddingTop: safeArea().top + 2 }} testID="phone-top-bar">
      <Button onPress={() => { Keyboard.dismiss(); goBack(); }} accessibilityLabel={phoneParent() ? `Back to ${phoneParent()!.title}` : "Back to Sessions"} testID="phone-back"
        class={`items-center justify-center size-11 rounded-full active:bg-selection ${FOCUS}`}>
        <Icon name="chevron-left" size={28} class="tint-foreground" />
      </Button>
      <View class="flex-1 min-w-0 px-1">
        <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[17px] leading-[22px] font-semibold text-foreground" testID="phone-session-title">{s()?.title || "New session"}</Text>
        <View class="flex-row items-center gap-1 min-w-0">
          {/* A subagent names its parent where a root names its project (user, 2026-11-24). */}
          <If when={!!phoneParent()} else={<If when={!!project()}><Text style={{ numberOfLines: 1 }} class={`${T.meta} shrink min-w-0`} testID="phone-session-project">{project()}</Text></If>}>
            <Text style={{ numberOfLines: 1 }} class={`${T.meta} shrink min-w-0`} testID="phone-session-parent">{`Subagent of ${phoneParent()?.title ?? ""}`}</Text>
          </If>
          <If when={(!!project() || !!phoneParent()) && !!status()}><Text class={T.meta}>·</Text></If>
          <If when={status()?.tone === "warning"}><Text class="text-[9px] leading-5 text-warning" accessible={false} testID="phone-status-mark">◆</Text></If>
          <If when={status()?.tone === "info"}><Spinner class="font-(family-name:--font-ui) text-[12px] leading-5 text-info" /></If>
          <If when={status()}>{() => <Text testID="phone-session-status" style={{ numberOfLines: 1 }} class={`font-(family-name:--font-ui) text-[12px] leading-5 ${status()!.tone === "warning" ? "text-warning font-medium" : status()!.tone === "info" ? "text-info" : "text-muted-foreground"}`}>{status()!.word}</Text>}</If>
          <If when={running()}>
            <Text class={T.meta}>·</Text>
            <Text testID="phone-session-timer" style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[12px] leading-5 font-medium text-info tabular-nums">{fmtDuration(elapsed())}</Text>
          </If>
        </View>
      </View>
      <If when={keyboardFrame().height > 0}><IconButton icon="chevron-down" onPress={() => Keyboard.dismiss()} label="Dismiss keyboard" testID="phone-dismiss-keyboard" /></If>
      <IconButton icon="more" label="Session actions" testID="phone-session-menu" disabled={!s()} onPress={() => { const v = s(); if (v) rowSheet.set({ id: v.id, renaming: false }); }} />
    </View>
  );
}

/**
 * A strip along the left edge of a conversation: a rightward drag goes back, as an iOS back-swipe does (user,
 * 2026-11-24: no drawer): a subagent to its parent conversation, a root session to Sessions. It is 20 px wide and claims only a mostly-horizontal move, so vertical scrolling and a
 * code block's own horizontal scroll (which starts inside the reading column) are untouched.
 */
function EdgeSwipe() {
  const pan = PanResponder.create({
    onStartShouldSetPanResponder: () => false,
    onMoveShouldSetPanResponder: (_e, g) => g.dx > 10 && Math.abs(g.dx) > Math.abs(g.dy) * 1.6,
    onPanResponderRelease: (_e, g) => { if (g.dx > 56) { Keyboard.dismiss(); goBack(); } },
  });
  return <View {...pan.panHandlers} class="absolute left-0 top-0 bottom-0 w-5" testID="phone-edge-swipe" accessible={false} />;
}

/** The phone client: the three root tabs, or one conversation with Back to Sessions instead of the bar. */
export function PhoneShell() {
  // Leaving the Manager tab closes the Manager's panel; opening the tab shows it.
  effect(() => {
    const onManager = phonePage() === "manager";
    // Opening the tab only shows the panel: it never starts the Manager or connects the voice (user, 2026-11-24).
    if (onManager && !untracked(dropdownOpen)) showManagerPanel();
    if (!onManager && untracked(dropdownOpen)) closeManagerDropdown();
  });
  const managerDot = () => phonePage() !== "manager" && (card() !== null || consent() !== null);
  const conversation = () => phonePage() === "conversation";
  // Sessions waiting on you, as the Sessions tab's badge.
  const needsYou = () => { m(); return [...model.sessions.values()].filter((x) => !x.parent_id && !x.archived && attention(x.id) === "waiting").length; };
  return <View class="flex-1 min-h-0" testID="phone-shell" style={{ paddingBottom: phoneShellBottomInset(phonePage(), keyboardFrame().height, safeArea().bottom), paddingLeft: safeArea().left, paddingRight: safeArea().right }}>
    <If when={conversation()}><ConversationHeader /></If>
    <If when={phonePage() === "manager"}><TabTitle title="Manager" /></If>
    <If when={phonePage() === "settings" && settingsDrill() === null}><TabTitle title="Settings" /></If>
    <View class="flex-1 min-h-0">
      <If when={phonePage() === "sessions"}><PhoneSessionsPane variant="tab" /></If>
      <If when={phonePage() === "manager"}><ManagerTab /></If>
      <If when={phonePage() === "settings"}><PhoneSettings /></If>
      <If when={conversation()}>
        <View class="flex-1 min-h-0" testID="phone-conversation"><Centre /></View>
      </If>
      <If when={conversation()}><EdgeSwipe /></If>
      <Drawers />
    </View>
    <If when={phoneTabBarVisible(phonePage())}>
      <View class="flex-row items-stretch px-1 bg-background border-t border-border" style={{ paddingTop: 6, paddingBottom: keyboardFrame().height > 0 ? 0 : safeArea().bottom }} testID="phone-tabs">
        <Each of={PHONE_TABS} key={(tab) => tab.id}>
          {(tab) => <Button onPress={() => { Keyboard.dismiss(); phonePage.set(tab().id); }} selected={phonePage() === tab().id} accessibilityLabel={tab().id === "sessions" && needsYou() > 0 ? `${tab().label}, ${needsYou()} waiting on you` : tab().label} testID={`phone-tab-${tab().id}`} class={`relative flex-1 items-center justify-center gap-0.5 min-h-14 rounded-2xl active:bg-selection ${FOCUS}`}>
            <Icon name={tab().icon} size={24} class={phonePage() === tab().id ? "tint-foreground" : "tint-muted-foreground"} />
            <Text class={`font-(family-name:--font-ui) text-[11px] leading-[13px] ${phonePage() === tab().id ? "text-foreground font-semibold" : "text-muted-foreground font-medium"}`}>{tab().label}</Text>
            <If when={tab().id === "sessions" && needsYou() > 0}>
              <View testID="phone-tab-sessions-badge" accessible={false} class="absolute top-1 min-w-[18px] h-[18px] px-1 rounded-full bg-warning items-center justify-center" style={{ marginLeft: 26 }}>
                <Text class="font-(family-name:--font-ui) text-[11px] leading-[14px] font-bold text-background tabular-nums">{String(needsYou())}</Text>
              </View>
            </If>
            <If when={tab().id === "manager" && managerDot()}><View testID="phone-tab-manager-dot" class="absolute top-2 size-2 rounded-full bg-warning" style={{ marginLeft: 22 }} /></If>
          </Button>}
        </Each>
      </View>
    </If>
    <PhoneSheets />
  </View>;
}
