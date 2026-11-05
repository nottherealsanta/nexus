import { mount, View, Text, platform, safeArea, windowSize, effect, colorScheme } from "nsh";
import { Window } from "nsh/desktop";
import { Button } from "nsh/base";
import { Header } from "./src/ui/header";
import { LeftSidebar, SessionRail, SessionsTab } from "./src/ui/sidebar";
import { SettingsView } from "./src/ui/settings";
import { Icon, IconButton, FOCUS, T, TS } from "./src/ui/kit";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { FilePanel } from "./src/ui/editor";
import { ContextDetailModal } from "./src/ui/newsession";
import { ManagerContextModal } from "./src/ui/manager/context-modal";
import { ResourcesModal } from "./src/ui/resources";
import { AlertDetail } from "./src/ui/alerts";
import { UsageDialog } from "./src/ui/usage";
import { ImagePreview } from "./src/ui/image-preview";
import { Annotate } from "./src/ui/annotate";
import { ManagerStage } from "./src/ui/manager/stage";
import { installManager } from "./src/core/manager";
import { cancelDictation, microphoneAvailable, startDictation, stopDictation } from "./src/dictation";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker, RenameSession, SkillChecklist } from "./src/ui/overlays";
import { SessionSwitcher } from "./src/ui/switcher";
import { ConnectScreen, coreEditing } from "./src/ui/connect";
import { Splash } from "./src/ui/splash";
import { bindKeys, rightIsOpen, rightWidthHint } from "./src/commands";
import { coreReady, coreUrl, drawer, isNarrow, leftOpen, loadPrefs, native, panelOpen, phone, phonePage, railShown, startApp, syncView, themeChoice, viewed } from "./src/store";
import { setColorScheme } from "nsh";
import "./fonts/Geist-Regular.ttf";
import "./fonts/Geist-Medium.ttf";
import "./fonts/Geist-SemiBold.ttf";
import "./fonts/Geist-Bold.ttf";

function PhoneShell() {
  return <View class="flex-1 min-h-0" testID="phone-shell">
    <View class="flex-row items-center px-3 pb-2 bg-sidebar" style={{ paddingTop: safeArea().top + 8 }}>
      <If when={phonePage() === "conversation"}>
        <Button onPress={() => phonePage.set("sessions")} accessibilityLabel="Back to sessions" testID="phone-back" class={`min-h-11 justify-center px-2 ${FOCUS}`}><Text class="text-foreground">‹ Sessions</Text></Button>
      </If>
      <Text style={{ numberOfLines: 1 }} class={`flex-1 ${T.title} px-2`}>{phonePage() === "sessions" ? "Sessions" : phonePage() === "settings" ? "Settings" : viewed()?.title || "New session"}</Text>
      <If when={phonePage() === "settings"}><Button onPress={() => coreEditing.set(true)} accessibilityLabel="Change Core address" class={`min-h-11 justify-center px-3 ${FOCUS}`}><Text class="text-foreground">Core</Text></Button></If>
      <If when={phonePage() === "conversation"}><IconButton icon="panel-right" label="Session details" onPress={() => drawer.set("right")} /></If>
    </View>
    <View class="flex-1 min-h-0">
      <If when={phonePage() === "sessions"}><SessionsTab /></If>
      <If when={phonePage() === "settings"}><SettingsView inline /></If>
      <If when={phonePage() === "conversation"}><Centre /></If>
      <Drawers />
    </View>
    <If when={phonePage() !== "conversation"}>
      <View class="flex-row border-t border-border bg-sidebar" style={{ paddingBottom: safeArea().bottom }}>
        <Each of={[{ id: "sessions" as const, label: "Sessions", icon: "sessions" as const }, { id: "settings" as const, label: "Settings", icon: "settings" as const }]} key={(tab) => tab.id}>
          {(tab) => <Button onPress={() => phonePage.set(tab().id)} selected={phonePage() === tab().id} accessibilityLabel={tab().label} testID={`phone-tab-${tab().id}`} class={`flex-1 items-center justify-center gap-1 min-h-14 ${FOCUS}`}>
            <Icon name={tab().icon} class={phonePage() === tab().id ? "tint-foreground" : "tint-muted-foreground"} />
            <Text class={`${TS.base} ${phonePage() === tab().id ? "text-foreground font-semibold" : "text-muted-foreground"}`}>{tab().label}</Text>
          </Button>}
        </Each>
      </View>
    </If>
  </View>;
}

function App() {
  void loadPrefs().then(() => { if (themeChoice() !== "system") setColorScheme(themeChoice()); });
  startApp();
  bindKeys();
  installManager({ available: microphoneAvailable, start: startDictation, stop: stopDictation, cancel: cancelDictation });
  effect(() => { rightWidthHint.w = windowSize().width; });
  effect(() => { syncView(); });
  effect(() => { if (platform.os === "macos") Window.setTitleBar({ style: "hidden", height: 44 }); });
  void colorScheme;
  const leftPersistent = () => !isNarrow() && leftOpen();
  const rightPersistent = () => !isNarrow() && rightIsOpen(windowSize().width);
  const booting = () => native && !coreReady();
  const connect = () => native && platform.os !== "macos" && (coreUrl() === null || coreEditing());
  return (
    <View class="flex-1 bg-background text-[16px] leading-6" testID="app">
      <If when={booting()} else={<If when={connect()} else={<View class="flex-1 min-h-0">
        <If when={phone} else={<>
        <Header />
        <View class="flex-1 min-h-0">
          <View class="flex-1 flex-row min-h-0">
            <If when={railShown()}><SessionRail /></If>
            <If when={leftPersistent()}><LeftSidebar /></If>
            <Centre />
            <If when={rightPersistent()}><RightSidebar /></If>
          </View>
          <Drawers />
        </View>
        <If when={panelOpen()}><FilePanel /></If>
        </>}><PhoneShell /></If>
        <ContextDetailModal />
        <ResourcesModal />
        <Palette />
        <SessionSwitcher />
        <Picker />
        <SkillChecklist />
        <AlertDetail />
        <UsageDialog />
        <ImagePreview />
        <Annotate />
        <RenameSession />
        <Confirm />
        <AddProject />
        <Blocking />
        <ManagerStage />
        <ManagerContextModal />
      </View>}>
        <ConnectScreen />
      </If>}>
        <View class="flex-1 bg-background" />
      </If>
      <Splash />
    </View>
  );
}
mount(App);
