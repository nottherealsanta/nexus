import { Scroll, Text, View } from "nsh";
import { Button } from "nsh/base";
import { SessionHeading, SubagentStrip } from "./header";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { ProviderDetailModal, SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { centre, pendingFirstPrompt, phone, previousCentre, viewed } from "../store";

export function Centre() {
  // Settings is a modal (D13): the view it opened over stays underneath.
  // A file lives in the right-hand panel; the session stays underneath it.
  const base = () => (centre() === "settings" ? previousCentre() : centre() === "file" ? "session" : centre());
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <If when={!phone}><SessionHeading /><SubagentStrip /></If>
      <Banners />
      <If when={centre() === "settings"}><SettingsView /></If>
      <If when={centre() === "settings"}><ProviderDetailModal /></If>
      <If when={base() === "session" && !!viewed() && !pendingFirstPrompt()}><Timeline /></If>
      <If when={base() === "new_session" || (base() === "session" && (!viewed() || !!pendingFirstPrompt()))}><NewSessionView /></If>
      <If when={true}>
        <WaitingCard />
        <Composer />
      </If>
      <Toasts />
    </View>
  );
}
