// The phone client's shell (plans/05 sections 5.1 and 5.2): Sessions, Manager and Settings tabs, a conversation with a
// compact header and a left drawer of sessions. NSH phone idiom only; AGENTS.md "Mobile is a cut-down client".
import { Keyboard, PanResponder, Text, View, effect, keyboardFrame, safeArea, untracked, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Icon, IconButton, FOCUS, T, TS, Spinner } from "../kit";
import { Centre } from "../centre";
import { PhoneSessionsPane } from "../sidebar";
import { ManagerTab, ManagerWave, managerHeaderIndicatorVisible } from "../manager/stage";
import { Drawers, EdgeDrawer } from "../overlays";
import { card, closeManagerDropdown, consent, dropdownOpen, toggleManagerDropdown } from "../../core/manager";
import { PHONE_TABS, phoneStatus } from "../../mobile";
import { attention } from "../sidebar";
import { currentProject, m, model, phonePage, viewed } from "../../store";
import { PhoneSettings } from "./settings";
import { PhoneSheets } from "./sheets";
import { closeDrawer, openDrawer, phoneDrawer, rowSheet, settingsDrill } from "./state";

/** A bar title for the Manager and Settings tabs. */
function TabTitle(p: { title: string }) {
  return (
    <View class="px-4 pb-2 bg-sidebar" style={{ paddingTop: safeArea().top + 6 }} testID="phone-top-bar">
      <View class="flex-row items-center justify-between gap-3">
        <Text style={{ numberOfLines: 1 }} testID="phone-tab-title" class="font-(family-name:--font-ui) text-[22px] leading-8 font-semibold text-foreground">{p.title}</Text>
        <If when={p.title === "Manager" && managerHeaderIndicatorVisible()}>{() => <View style={{ width: 100, height: 20 }} testID="manager-tab-wave" accessible={false}><ManagerWave /></View>}</If>
      </View>
    </View>
  );
}

/** The status ring: a spinner while working, a warning ring while it needs you, a quiet ring when idle. */
function StatusRing(p: { tone: "warning" | "info" | "muted" }) {
  return (
    <View class="size-6 items-center justify-center" testID="phone-status-ring" accessible={false}>
      <If when={p.tone === "info"} else={<View class={`size-3.5 rounded-full border-2 ${p.tone === "warning" ? "border-warning bg-warning/30" : "border-border-strong"}`} />}>
        <Spinner class="font-(family-name:--font-ui) text-[16px] leading-6 text-info" />
      </If>
    </View>
  );
}

/** A conversation's header: the sessions drawer button, the title with its project and status, and the overflow menu. */
function ConversationHeader() {
  const s = () => { m(); return viewed(); };
  const status = () => {
    m();
    const v = viewed();
    return v ? phoneStatus({ waiting: attention(v.id) === "waiting", running: model.tree(v.id).some((id) => model.running.has(id)) || v.status === "busy", archived: !!v.archived }) : null;
  };
  const project = () => { m(); const id = s()?.project_id ?? currentProject(); return id ? model.projects.get(id)?.name ?? "" : ""; };
  return (
    <View class="flex-row items-center gap-1 px-1 pb-1 bg-sidebar" style={{ paddingTop: safeArea().top + 4 }} testID="phone-top-bar">
      <IconButton icon="panel-left" label="Sessions" testID="phone-drawer-button" onPress={() => { Keyboard.dismiss(); openDrawer(); }} />
      <View class="flex-1 min-w-0 px-1">
        <Text style={{ numberOfLines: 1 }} class={`${TS.title} font-semibold text-foreground`} testID="phone-session-title">{s()?.title || "New session"}</Text>
        <View class="flex-row items-center gap-1 min-w-0">
          <If when={!!project()}><Text style={{ numberOfLines: 1 }} class={`${T.meta} shrink min-w-0`} testID="phone-session-project">{project()}</Text></If>
          <If when={!!project() && !!status()}><Text class={T.meta}>·</Text></If>
          <If when={status()}>{() => <Text testID="phone-session-status" style={{ numberOfLines: 1 }} class={`font-(family-name:--font-ui) text-[12px] leading-5 ${status()!.tone === "warning" ? "text-warning font-medium" : status()!.tone === "info" ? "text-info" : "text-muted-foreground"}`}>{status()!.word}</Text>}</If>
        </View>
      </View>
      <If when={status()}>{() => <StatusRing tone={status()!.tone} />}</If>
      <If when={keyboardFrame().height > 0}><IconButton icon="chevron-down" onPress={() => Keyboard.dismiss()} label="Dismiss keyboard" testID="phone-dismiss-keyboard" /></If>
      <IconButton icon="more" label="Session actions" testID="phone-session-menu" disabled={!s()} onPress={() => { const v = s(); if (v) rowSheet.set({ id: v.id, renaming: false }); }} />
    </View>
  );
}

/**
 * A strip along the left edge of a conversation: a rightward drag opens the sessions drawer. It is 20 px wide and claims
 * only a mostly-horizontal move, so vertical scrolling and a code block's own horizontal scroll (which starts inside the
 * reading column) are untouched. The app has no navigation stack, so there is no system back-swipe to compete with.
 */
function EdgeSwipe() {
  const pan = PanResponder.create({
    onStartShouldSetPanResponder: () => false,
    onMoveShouldSetPanResponder: (_e, g) => g.dx > 10 && Math.abs(g.dx) > Math.abs(g.dy) * 1.6,
    onPanResponderRelease: (_e, g) => { if (g.dx > 56) openDrawer(); },
  });
  return <View {...pan.panHandlers} class="absolute left-0 top-0 bottom-0 w-5" testID="phone-edge-swipe" accessible={false} />;
}

/** The phone client: Sessions, Manager and Settings tabs; a session opens full screen with a drawer of sessions. */
export function PhoneShell() {
  // Leaving the Manager tab closes the Manager's panel; opening the tab opens it, as the header indicator does on a desktop.
  effect(() => {
    const onManager = phonePage() === "manager";
    if (onManager && !untracked(dropdownOpen)) toggleManagerDropdown();
    if (!onManager && untracked(dropdownOpen)) closeManagerDropdown();
  });
  // The drawer belongs to a conversation.
  effect(() => { if (phonePage() !== "conversation" && phoneDrawer()) closeDrawer(); });
  const managerDot = () => phonePage() !== "manager" && (card() !== null || consent() !== null);
  const conversation = () => phonePage() === "conversation";
  return <View class="flex-1 min-h-0" testID="phone-shell" style={{ paddingBottom: conversation() ? keyboardFrame().height : 0, paddingLeft: safeArea().left, paddingRight: safeArea().right }}>
    <If when={conversation()}><ConversationHeader /></If>
    <If when={phonePage() === "manager"}><TabTitle title="Manager" /></If>
    <If when={phonePage() === "settings" && settingsDrill() === null}><TabTitle title="Settings" /></If>
    <View class="flex-1 min-h-0">
      <If when={phonePage() === "sessions"}><PhoneSessionsPane variant="tab" /></If>
      <If when={phonePage() === "manager"}><ManagerTab /></If>
      <If when={phonePage() === "settings"}><PhoneSettings /></If>
      <If when={conversation()}>
        <View class="flex-1 min-h-0" testID="phone-conversation" style={{ transform: phoneDrawer() ? `translateX(${Math.min(340, Math.round(windowSize().width * 0.92))}px)` : undefined }}><Centre /></View>
      </If>
      <If when={conversation() && !phoneDrawer()}><EdgeSwipe /></If>
      <If when={conversation() && phoneDrawer()}>
        <EdgeDrawer side="left" width={340} label="Sessions drawer" testID="phone-drawer" onClose={closeDrawer}><PhoneSessionsPane variant="drawer" /></EdgeDrawer>
      </If>
      <Drawers />
    </View>
    <If when={!conversation()}>
      <View class="flex-row border-t border-border bg-sidebar" style={{ paddingTop: 10, paddingBottom: keyboardFrame().height > 0 ? 0 : safeArea().bottom }} testID="phone-tabs">
        <Each of={PHONE_TABS} key={(tab) => tab.id}>
          {(tab) => <Button onPress={() => phonePage.set(tab().id)} selected={phonePage() === tab().id} accessibilityLabel={tab().label} testID={`phone-tab-${tab().id}`} class={`relative flex-1 items-center justify-center gap-1 min-h-14 ${FOCUS}`}>
            <Icon name={tab().icon} size={22} class={phonePage() === tab().id ? "tint-foreground" : "tint-muted-foreground"} />
            <Text class={`font-(family-name:--font-ui) text-[12px] leading-4 ${phonePage() === tab().id ? "text-foreground font-semibold" : "text-muted-foreground font-medium"}`}>{tab().label}</Text>
            <If when={tab().id === "manager" && managerDot()}><View testID="phone-tab-manager-dot" class="absolute top-2 size-2 rounded-full bg-warning" style={{ marginLeft: 22 }} /></If>
          </Button>}
        </Each>
      </View>
    </If>
    <PhoneSheets />
  </View>;
}
