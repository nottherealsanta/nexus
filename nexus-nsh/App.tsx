import { installFileWatch } from "./src/file-watch";
import { mount, View, platform, windowSize, effect, colorScheme } from "nsh";
import { Tray, Window } from "nsh/desktop";
import { Header } from "./src/ui/header";
import { LeftSidebar, SessionRail } from "./src/ui/sidebar";
import { PhoneShell } from "./src/ui/mobile/shell";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { FilePanel } from "./src/ui/editor";
import { TerminalPanel } from "./src/ui/terminal";
import { terminalOpen } from "./src/terminal-panel";
import { ContextDetailModal } from "./src/ui/newsession";
import { SubagentDoneModal } from "./src/ui/timeline";
import { ManagerContextModal } from "./src/ui/manager/context-modal";
import { ResourcesModal } from "./src/ui/resources";
import { InspectorModal } from "./src/ui/inspector";
import { AlertDetail } from "./src/ui/alerts";
import { UsageDialog } from "./src/ui/usage";
import { ImagePreview } from "./src/ui/image-preview";
import { Annotate } from "./src/ui/annotate";
import { ManagerStage } from "./src/ui/manager/stage";
import { installManager } from "./src/core/manager";
import { cancelDictation, microphoneAvailable, startDictation, stopDictation } from "./src/dictation";
import { projectTitle, sessionTitle, trayModel } from "./src/tray-logic";
import trayIcon from "./src/icons/tray.svg";
import trayAttentionIcon from "./src/icons/tray-attention.svg";
import { ProjectsOverviewHost } from "./src/ui/projects-overview";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker, RenameSession, SkillChecklist, FileOperationDialog, WorktreeInfo } from "./src/ui/overlays";
import { SessionSwitcher } from "./src/ui/switcher";
import { ConnectScreen, coreEditing } from "./src/ui/connect";
import { Splash } from "./src/ui/splash";
import { bindKeys, rightIsOpen, rightWidthHint } from "./src/commands";
import { coreReady, coreUrl, m, model, selectSession, isNarrow, leftOpen, loadPrefs, native, panelOpen, phone, phonePage, railShown, startApp, syncView, themeChoice, viewed } from "./src/store";
import { setColorScheme } from "nsh";
// A value import, though nothing is read from it: `nsh package` links a native UI set into the web runtime only
// when the bundle imports it, and the modules look it up by name (`nativeModule`), which the packager cannot see.
import "nexus/native";
import "./fonts/GoogleSans-Variable.ttf";

/** The system tray: projects and session status as a menu, and the icon marks a session that needs attention. */
function installTray() {
  m();
  const t = trayModel(model.projects.values(), model.sessions.values(), model.approvals.values(), model.questions.values(), model.running, model.unreadSessions);
  const open = (id: string) => { Window.orderFront(); void selectSession(id); };
  Tray.set({
    icon: t.attention ? trayAttentionIcon : trayIcon,
    tooltip: t.tooltip,
    onClick: () => Window.orderFront(),
    menu: [
      { label: "Open Nexus", onSelect: () => Window.orderFront() },
      { type: "separator" },
      ...t.projects.map((p) => ({
        label: projectTitle(p),
        submenu: p.sessions.length
          ? [...p.sessions.map((s) => ({ label: sessionTitle(s), onSelect: () => open(s.id) })), ...(p.more ? [{ label: `${p.more} more…`, disabled: true }] : [])]
          : [{ label: "No sessions", disabled: true }],
      })),
      ...(t.projects.length ? [{ type: "separator" as const }] : []),
      { role: "quit" as const },
    ],
  });
}

function App() {
  void loadPrefs().then(() => { if (themeChoice() !== "system") setColorScheme(themeChoice()); });
  startApp();
  bindKeys();
  installManager({ available: microphoneAvailable, start: startDictation, stop: stopDictation, cancel: cancelDictation });
  effect(() => { rightWidthHint.w = windowSize().width; });
  effect(() => { syncView(); });
  installFileWatch();
  effect(() => { if (platform.os === "macos") Window.setTitleBar({ style: "hidden", height: 44 }); });
  effect(() => { if (!phone) installTray(); });
  void colorScheme;
  const leftPersistent = () => !isNarrow() && leftOpen();
  const rightPersistent = () => !isNarrow() && rightIsOpen(windowSize().width);
  const booting = () => native && !coreReady();
  const connect = () => (native && platform.os !== "macos" && (coreUrl() === null || coreEditing())) || (phone && coreEditing());
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
        <If when={terminalOpen()}><TerminalPanel /></If>
        </>}><PhoneShell /></If>
        <ContextDetailModal />
        <SubagentDoneModal />
        <ResourcesModal />
        <InspectorModal />
        <Palette />
        <SessionSwitcher />
        <Picker />
        <ProjectsOverviewHost />
        <SkillChecklist />
        <AlertDetail />
        <UsageDialog />
        <ImagePreview />
        <Annotate />
        <RenameSession />
        <WorktreeInfo />
        <FileOperationDialog />
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
