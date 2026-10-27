import { View } from "nsh";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { centre, viewed } from "../store";

export function Centre() {
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <Banners />
      <If when={centre() === "settings"}><SettingsView /></If>
      <If when={centre() === "session" && !!viewed()}><Timeline /></If>
      <If when={centre() === "new_session" || (centre() === "session" && !viewed())}><NewSessionView /></If>
      <If when={centre() !== "settings"}>
        <WaitingCard />
        <Composer />
      </If>
      <Toasts />
    </View>
  );
}
