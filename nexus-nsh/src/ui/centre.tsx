import { Scroll, Text, View } from "nsh";
import { Button } from "nsh/base";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { FileView } from "./editor";
import { centre, openFile, previousCentre, viewed } from "../store";

export function Centre() {
  // Settings is a modal (D13): the view it opened over stays underneath.
  const base = () => (centre() === "settings" ? previousCentre() : centre());
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <Banners />
      <If when={centre() === "settings"}><SettingsView /></If>
      <If when={base() === "file"}><FileView /></If>
      <If when={base() === "session" && !!viewed()}><Timeline /></If>
      <If when={base() === "new_session" || (base() === "session" && !viewed())}><NewSessionView /></If>
      <If when={true}>
        <WaitingCard />
        <Composer />
      </If>
      <Toasts />
    </View>
  );
}
