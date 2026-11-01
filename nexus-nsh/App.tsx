import { mount, View, windowSize, effect, colorScheme } from "nsh";
import { Header } from "./src/ui/header";
import { LeftSidebar, SessionRail } from "./src/ui/sidebar";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { ContextDetailModal } from "./src/ui/newsession";
import { AlertDetail } from "./src/ui/alerts";
import { UsageDialog } from "./src/ui/usage";
import { ImagePreview } from "./src/ui/image-preview";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker, RenameSession, SkillChecklist } from "./src/ui/overlays";
import { ConnectScreen, coreEditing } from "./src/ui/connect";
import { bindKeys, rightIsOpen, rightWidthHint } from "./src/commands";
import { coreReady, coreUrl, isNarrow, leftOpen, loadPrefs, native, startApp, themeChoice } from "./src/store";
import { setColorScheme } from "nsh";
import "./fonts/MonaspaceNeon-Regular.otf";
import "./fonts/MonaspaceNeon-Medium.otf";
import "./fonts/MonaspaceNeon-Bold.otf";

function App() {
  void loadPrefs().then(() => { if (themeChoice() !== "system") setColorScheme(themeChoice()); });
  startApp();
  bindKeys();
  effect(() => { rightWidthHint.w = windowSize().width; });
  void colorScheme;
  const leftPersistent = () => !isNarrow() && leftOpen();
  const rightPersistent = () => !isNarrow() && rightIsOpen(windowSize().width);
  const booting = () => native && !coreReady();
  const connect = () => native && (coreUrl() === null || coreEditing());
  return (
    <View class="flex-1 bg-background" testID="app">
      <If when={booting()} else={<If when={connect()} else={<View class="flex-1 min-h-0">
        <Header />
        <View class="flex-1 min-h-0">
          <View class="flex-1 flex-row min-h-0">
            <If when={leftPersistent()} else={<If when={!isNarrow()}><SessionRail /></If>}><LeftSidebar /></If>
            <Centre />
            <If when={rightPersistent()}><RightSidebar /></If>
          </View>
          <Drawers />
        </View>
        <ContextDetailModal />
        <Palette />
        <Picker />
        <SkillChecklist />
        <AlertDetail />
        <UsageDialog />
        <ImagePreview />
        <RenameSession />
        <Confirm />
        <AddProject />
        <Blocking />
      </View>}>
        <ConnectScreen />
      </If>}>
        <View class="flex-1 bg-background" />
      </If>
    </View>
  );
}
mount(App);
