import { mount, View, windowSize, effect, colorScheme } from "nsh";
import { Header } from "./src/ui/header";
import { LeftSidebar, SessionRail } from "./src/ui/sidebar";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { ContextDetailModal } from "./src/ui/newsession";
import { AlertDetail } from "./src/ui/alerts";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker, RenameSession, SkillChecklist } from "./src/ui/overlays";
import { bindKeys, rightIsOpen, rightWidthHint } from "./src/commands";
import { isNarrow, leftOpen, loadPrefs, startApp, themeChoice } from "./src/store";
import { setColorScheme } from "nsh";

function App() {
  void loadPrefs().then(() => { if (themeChoice() !== "system") setColorScheme(themeChoice()); });
  startApp();
  bindKeys();
  effect(() => { rightWidthHint.w = windowSize().width; });
  void colorScheme;
  const leftPersistent = () => !isNarrow() && leftOpen();
  const rightPersistent = () => !isNarrow() && rightIsOpen(windowSize().width);
  return (
    <View class="flex-1 bg-background" testID="app">
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
      <RenameSession />
      <Confirm />
      <AddProject />
      <Blocking />
    </View>
  );
}
mount(App);
