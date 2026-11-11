import { Scroll, Text, View } from "nsh";
import { Button } from "nsh/base";
import { SessionBackRail, SessionHeading, sessionBackRailPx } from "./header";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { ProviderDetailModal, SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { SessionStatus } from "./session-status";
import { BtwModal } from "./btw";
import { centre, pendingFirstPrompt, phone, previousCentre, viewed } from "../store";

/** The content gutter that reserves the root-back strip's width; none for a root view. */
export const centreContentPadding = (back: boolean) => back ? { marginLeft: sessionBackRailPx() } : undefined;

export function Centre() {
  // Settings is a modal (D13): the view it opened over stays underneath.
  // A file lives in the right-hand panel; the session stays underneath it.
  const base = () => (centre() === "settings" ? previousCentre() : centre() === "file" ? "session" : centre());
  // A viewed subagent gets a two-cell full-height strip at the far left, beside the sidebar.
  const child = () => !phone && centre() === "session" && !!viewed()?.parent_id;
  return (
    <View class={`flex-1 min-w-0 min-h-0 bg-background ${child() ? "relative" : ""}`} testID="centre">
      <If when={child()}><SessionBackRail /></If>
      <View class="flex-1 min-w-0 min-h-0 flex-col" testID="centre-content" style={centreContentPadding(child())}>
        <If when={!phone}><SessionHeading /></If>
        <Banners />
        <If when={centre() === "settings"}><SettingsView /></If>
        <If when={centre() === "settings"}><ProviderDetailModal /></If>
        <If when={base() === "session" && !!viewed() && !pendingFirstPrompt()}><Timeline /></If>
        <If when={base() === "new_session" || (base() === "session" && (!viewed() || !!pendingFirstPrompt()))}><NewSessionView /></If>
        <If when={true}>
          <WaitingCard />
          <SessionStatus />
          <Composer />
        </If>
      </View>
      <Toasts />
      <BtwModal />
    </View>
  );
}
