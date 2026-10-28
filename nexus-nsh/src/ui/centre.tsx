import { Scroll, Text, View } from "nsh";
import { Button } from "nsh/base";
import { Banners, Toasts } from "./alerts";
import { Composer, WaitingCard } from "./composer";
import { NewSessionView } from "./newsession";
import { SettingsView } from "./settings";
import { Timeline } from "./timeline";
import { centre, openFile, viewed } from "../store";

export function Centre() {
  return (
    <View class="flex-1 min-w-0 min-h-0 bg-background" testID="centre">
      <Banners />
      <If when={centre() === "settings"}><SettingsView /></If>
      <If when={centre() === "file"}><View class="flex-1 min-h-0"><Button onPress={() => centre.set(viewed() ? "session" : "new_session")} accessibilityLabel="Back to session"><Text class="text-foreground">← {openFile()?.path}</Text></Button><Scroll class="flex-1"><Text selectable class="font-mono text-xs text-foreground">{openFile()?.error ?? openFile()?.content ?? "Loading file…"}</Text></Scroll></View></If>
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
