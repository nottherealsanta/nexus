import { View } from "nsh";
import { SessionBackRail, SessionHeading, sessionBackRailPx } from "./header";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { ProviderDetailModal, SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { SessionStatus } from "./session-status";
import { BtwModal } from "./btw";
import { Groups } from "./groups";
import { centre, pendingFirstPrompt, phone, previousCentre, viewed } from "../store";

/** The content gutter that reserves the root-back strip's width; none for a root view. */
export const centreContentPadding = (back: boolean) => back ? { marginLeft: sessionBackRailPx() } : undefined;

/**
 * The live session: the timeline (or the new-session view), what waits for the user and the composer. It is the
 * body of the session tab that owns the viewed session; the layout (groups.tsx) puts it in exactly one group.
 */
export function SessionPane() {
  // A viewed subagent gets a two-cell full-height strip at the far left, beside the sidebar.
  const child = () => !phone && centre() === "session" && !!viewed()?.parent_id;
  const fresh = () => centre() === "new_session" || !viewed() || !!pendingFirstPrompt();
  return (
    <View class={`flex-1 min-w-0 min-h-0 bg-background ${child() ? "relative" : ""}`} testID="session-pane">
      <If when={child()}><SessionBackRail /></If>
      <View class="flex-1 min-w-0 min-h-0 flex-col" testID="centre-content" style={centreContentPadding(child())}>
        <SessionHeading />
        <If when={!fresh()}><Timeline /></If>
        <If when={fresh()}><NewSessionView /></If>
        <WaitingCard />
        <SessionStatus />
        <Composer />
      </View>
    </View>
  );
}

/** The phone's conversation page: unchanged by plan 45 (no groups, no tabs), one session at a time. */
function PhoneCentre() {
  // Settings is a modal (D13): the view it opened over stays underneath.
  const base = () => (centre() === "settings" ? previousCentre() : centre());
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <View class="flex-1 min-w-0 min-h-0 flex-col" testID="centre-content">
        <Banners />
        <If when={centre() === "settings"}><SettingsView /></If>
        <If when={centre() === "settings"}><ProviderDetailModal /></If>
        <If when={base() === "session" && !!viewed() && !pendingFirstPrompt()}><Timeline /></If>
        <If when={base() === "new_session" || (base() === "session" && (!viewed() || !!pendingFirstPrompt()))}><NewSessionView /></If>
        <WaitingCard />
        <SessionStatus />
        <Composer />
      </View>
      <Toasts />
      <BtwModal />
    </View>
  );
}

export function Centre() {
  if (phone) return <PhoneCentre />;
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <Banners />
      <View class="flex-1 min-w-0 min-h-0 relative">
        <Groups />
        {/* Settings is a modal (D13): the groups stay mounted underneath, terminals included. */}
        <If when={centre() === "settings"}><SettingsView /></If>
        <If when={centre() === "settings"}><ProviderDetailModal /></If>
      </View>
      <Toasts />
      <BtwModal />
    </View>
  );
}
