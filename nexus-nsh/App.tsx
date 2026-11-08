import { installFileWatch } from "./src/file-watch";
import { mount, View, Text, platform, safeArea, windowSize, effect, colorScheme, untracked } from "nsh";
import { Tray, Window } from "nsh/desktop";
import { Button } from "nsh/base";
import { Header } from "./src/ui/header";
import { LeftSidebar, NewSessionButton, ProjectSelector, SessionRail, SessionsTab, attention } from "./src/ui/sidebar";
import { SettingsView } from "./src/ui/settings";
import { Icon, IconButton, FOCUS, T, TS } from "./src/ui/kit";
import { RightSidebar } from "./src/ui/right";
import { Centre } from "./src/ui/centre";
import { FilePanel } from "./src/ui/editor";
import { TerminalPanel } from "./src/ui/terminal";
import { terminalOpen } from "./src/terminal-panel";
import { ContextDetailModal } from "./src/ui/newsession";
import { ManagerContextModal } from "./src/ui/manager/context-modal";
import { ResourcesModal } from "./src/ui/resources";
import { InspectorModal } from "./src/ui/inspector";
import { AlertDetail } from "./src/ui/alerts";
import { UsageDialog } from "./src/ui/usage";
import { ImagePreview } from "./src/ui/image-preview";
import { Annotate } from "./src/ui/annotate";
import { ManagerStage, ManagerTab } from "./src/ui/manager/stage";
import { card, closeManagerDropdown, consent, dropdownOpen, installManager, toggleManagerDropdown } from "./src/core/manager";
import { PHONE_TABS, phoneStatus } from "./src/mobile";
import { cancelDictation, microphoneAvailable, startDictation, stopDictation } from "./src/dictation";
import { projectTitle, sessionTitle, trayModel } from "./src/tray-logic";
import trayIcon from "./src/icons/tray.svg";
import trayAttentionIcon from "./src/icons/tray-attention.svg";
import { ProjectsOverviewHost } from "./src/ui/projects-overview";
import { AddProject, Blocking, Confirm, Drawers, Palette, Picker, RenameSession, SkillChecklist, FileOperationDialog } from "./src/ui/overlays";
import { SessionSwitcher } from "./src/ui/switcher";
import { ConnectScreen, coreEditing } from "./src/ui/connect";
import { Splash } from "./src/ui/splash";
import { bindKeys, rightIsOpen, rightWidthHint } from "./src/commands";
import { coreReady, coreUrl, drawer, m, model, selectSession, isNarrow, leftOpen, loadPrefs, native, panelOpen, phone, phonePage, railShown, startApp, syncView, themeChoice, viewed } from "./src/store";
import { setColorScheme } from "nsh";
import "./fonts/Geist-Regular.ttf";
import "./fonts/Geist-Medium.ttf";
import "./fonts/Geist-SemiBold.ttf";
import "./fonts/Geist-Bold.ttf";

/** The phone's top bar: the project selector on Sessions, a back control and the status in a session, a title elsewhere. */
function PhoneTopBar() {
  const page = () => phonePage();
  const status = () => {
    m();
    const s = viewed();
    return s ? phoneStatus({ waiting: attention(s.id) === "waiting", running: model.tree(s.id).some((id) => model.running.has(id)) || s.status === "busy", archived: s.archived }) : null;
  };
  return <View class="flex-row items-center gap-2 px-3 pb-2 bg-sidebar" style={{ paddingTop: safeArea().top + 8 }} testID="phone-top-bar">
    <If when={page() === "sessions"}>
      <ProjectSelector />
      <NewSessionButton />
    </If>
    <If when={page() === "conversation"}>
      <Button onPress={() => phonePage.set("sessions")} accessibilityLabel="Back to sessions" testID="phone-back" class={`min-h-11 justify-center px-2 ${FOCUS}`}><Text class="text-foreground">‹ Sessions</Text></Button>
      <View class="flex-1 min-w-0">
        <Text style={{ numberOfLines: 1 }} class={T.title}>{viewed()?.title || "New session"}</Text>
        <If when={status()}>{() => <Text testID="phone-session-status" style={{ numberOfLines: 1 }} class={`${TS.base} ${status()!.tone === "warning" ? "text-warning" : status()!.tone === "info" ? "text-info" : "text-muted-foreground"}`}>{status()!.word}</Text>}</If>
      </View>
    </If>
    <If when={page() === "manager" || page() === "settings"}>
      <Text style={{ numberOfLines: 1 }} class={`flex-1 ${T.title} px-2`}>{page() === "manager" ? "Manager" : "Settings"}</Text>
    </If>
    <If when={page() === "settings"}><Button onPress={() => coreEditing.set(true)} accessibilityLabel="Change Core address" class={`min-h-11 justify-center px-3 ${FOCUS}`}><Text class="text-foreground">Core</Text></Button></If>
  </View>;
}

/** The phone client: Sessions, Manager and Settings tabs (AGENTS.md "Mobile is a cut-down client"); a session opens full screen. */
function PhoneShell() {
  // Leaving the Manager tab closes the Manager's panel; opening the tab opens it, as the header indicator does on a desktop.
  effect(() => {
    const onManager = phonePage() === "manager";
    if (onManager && !untracked(dropdownOpen)) toggleManagerDropdown();
    if (!onManager && untracked(dropdownOpen)) closeManagerDropdown();
  });
  const managerDot = () => phonePage() !== "manager" && (card() !== null || consent() !== null);
  return <View class="flex-1 min-h-0" testID="phone-shell">
    <PhoneTopBar />
    <View class="flex-1 min-h-0">
      <If when={phonePage() === "sessions"}><SessionsTab /></If>
      <If when={phonePage() === "manager"}><ManagerTab /></If>
      <If when={phonePage() === "settings"}><SettingsView inline /></If>
      <If when={phonePage() === "conversation"}><Centre /></If>
      <Drawers />
    </View>
    <If when={phonePage() !== "conversation"}>
      <View class="flex-row border-t border-border bg-sidebar" style={{ paddingBottom: safeArea().bottom }} testID="phone-tabs">
        <Each of={PHONE_TABS} key={(tab) => tab.id}>
          {(tab) => <Button onPress={() => phonePage.set(tab().id)} selected={phonePage() === tab().id} accessibilityLabel={tab().label} testID={`phone-tab-${tab().id}`} class={`relative flex-1 items-center justify-center gap-1 min-h-14 ${FOCUS}`}>
            <Icon name={tab().icon} class={phonePage() === tab().id ? "tint-foreground" : "tint-muted-foreground"} />
            <Text class={`${TS.base} ${phonePage() === tab().id ? "text-foreground font-semibold" : "text-muted-foreground"}`}>{tab().label}</Text>
            <If when={tab().id === "manager" && managerDot()}><View testID="phone-tab-manager-dot" class="absolute top-2 size-2 rounded-full bg-warning" style={{ marginLeft: 22 }} /></If>
          </Button>}
        </Each>
      </View>
    </If>
  </View>;
}

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
        <If when={terminalOpen()}><TerminalPanel /></If>
        </>}><PhoneShell /></If>
        <ContextDetailModal />
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
