import { mount, View, windowSize, effect, colorScheme } from "nsh";
import { Header } from "./src/ui/header";
import { LeftSidebar } from "./src/ui/sidebar";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker } from "./src/ui/overlays";
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
          <If when={leftPersistent()}><LeftSidebar /></If>
          <Centre />
          <If when={rightPersistent()}><RightSidebar /></If>
        </View>
        <Drawers />
      </View>
      <Palette />
      <Picker />
      <Confirm />
      <AddProject />
      <Blocking />
    </View>
  );
}
mount(App);
