import { Clipboard, Text, View, WebView, colorScheme, effect, keymap, signal, type WebViewRef } from "nsh";
import theme from "../../gen/theme.json";
import { commands, draftKey, toKeystroke } from "../commands";
import { PRINTABLE_KEYS } from "../manager";
import { Button, Input } from "nsh/base";
import { terminalPage } from "../gen/terminal-page";
import { terminalEvidenceContext, beginRenameTerminal, controlTerminal, detachTerminal, disconnectTerminal, focusTerminal, inputTerminal, renameTerminal, requestTerminalCopy, resizeTerminal, setTerminalScreen, setTerminalScreenFocus, showTerminal, terminalError, terminalFocused, terminalItems, terminalSelected, terminalView } from "../terminal-panel";
import { focusGroup, focusStripOf, groupIdOfTab, openTerminalTab, setScreenFocus } from "../layout-state";
import { confirmDialog, connection, viewed } from "../store";
import { terminalEvidence, terminalEvidenceAction, type TerminalEvidenceContext } from "../terminal-evidence";
import { previewTerminalEvidence } from "../terminal-evidence-preview";
import { Btn, FOCUS, Icon, T } from "./kit";
import { terminalNameSet, terminalRenameCancel } from "../terminal-panel";
import type { IconName } from "../icons";

function TerminalButton(p: {icon: IconName; label: string; onPress: () => void; disabled?: boolean; testID: string}) {
  const hovered = signal(false);
  return <View class="relative" onMouseEnter={() => hovered.set(true)} onMouseLeave={() => hovered.set(false)}><Button onPress={p.onPress} disabled={p.disabled} accessibilityLabel={p.label} testID={p.testID}
    class={`size-8 items-center justify-center rounded-(--radius) hover:bg-accent active:bg-selection disabled:opacity-50 ${FOCUS}`}>
    <View testID={`terminal-icon-${p.icon}`}><Icon name={p.icon} /></View>
  </Button><If when={hovered()}><View style={{width:224}} class="absolute right-0 top-8 z-50 bg-popover rounded-(--radius) px-2 py-1" testID={`${p.testID}-tooltip`}><Text class="text-[12px] leading-4 font-(family-name:--font-ui) text-popover-foreground">{p.label}</Text></View></If></View>;
}

/** One terminal tab's body: the toolbar, the banners and the screen. Its controller (attachment, control, input) is the terminal's own. */
export function TerminalPane(p: { id: string }) {
  const id = p.id;
  let web: WebViewRef | undefined;
  const view = terminalView(id);
  const item = () => terminalItems().find(t => t.id === id);
  const selected = () => terminalSelected() === id;
  const tabTip = signal<{id: string; label: string} | null>(null);
  let copyPending = false;
  let evidenceRequest = 0;
  let evidencePending: {request: number; id: string; name: string; target: string; generation: number; context: TerminalEvidenceContext} | null = null;
  const attachSelection = () => {
    const source = item();
    if (!source || viewed()?.kind === "manager" || connection().kind !== "live") return;
    focusTerminal(id);
    focusList();
    const watermark = terminalEvidenceContext();
    evidencePending = {request: ++evidenceRequest, id: source.id, name: source.name, target: draftKey(), generation:watermark.generation, context:{
      project_id:source.project_id, workspace:source.workspace, stream_seq:watermark.stream_seq,
    }};
    send({type:"capture", request:evidenceRequest});
  };
  effect(() => {
    if (!terminalFocused() || !selected()) return;
    const modified = ["ctrl", "alt", "cmd"].flatMap(mod => [..."abcdefghijklmnopqrstuvwxyz0123456789[]"].flatMap(k => [`${mod}+${k}`, `${mod}+shift+${k}`]));
    const keys = new Set([...PRINTABLE_KEYS, ...modified, "escape", "enter", "tab", "space", "up", "down", "left", "right", "pageup", "pagedown", ...commands.flatMap(c => [c.keys?.nsh, c.keys?.tui, ...(c.key_aliases?.nsh ?? []), ...(c.key_aliases?.tui ?? [])].filter((k): k is string => !!k).map(toKeystroke))]);
    const off: (() => void)[] = [];
    for (const key of keys) { try { off.push(keymap.bind(key, () => {}, {when:"always",passThrough:true})); } catch {} }
    return () => off.forEach(f => f());
  });
  effect(() => { if (selected()) { terminalEvidenceAction.run = attachSelection; return () => { if (terminalEvidenceAction.run === attachSelection) terminalEvidenceAction.run = null; }; } });
  let ready = false;
  let pending: string[] = [];
  let pendingBytes = 0, writeId = 0;
  const writes = new Map<number, {resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout>}>();
  const send = (message: {type: string; [key: string]: unknown}): void | Promise<void> => {
    if (message.type === "reset") evidencePending = null;
    let ack: Promise<void> | undefined;
    if (message.type === "write") {
      const wid = ++writeId; message = {...message, id:wid};
      ack = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => { writes.delete(wid); reject(new Error("Terminal screen stopped responding.")); }, 15000);
        writes.set(wid, {resolve, reject, timer});
      });
    }
    const payload = JSON.stringify(message);
    if (ready && web) web.postMessage(payload);
    else {
      pendingBytes += payload.length;
      if (pendingBytes > 1500000) { pending = []; pendingBytes = 0; disconnectTerminal(id); terminalError.set("Terminal screen did not load; select the terminal to resynchronize."); }
      else pending.push(payload);
    }
    return ack;
  };
  const focusList = () => { setTerminalScreenFocus(id, false); web?.injectJavaScript("document.activeElement.blur()"); focusStripOf(id); };
  setTerminalScreen(id, send, {copy: () => { copyPending = true; send({type:"copy"}); }, focusList});
  setScreenFocus(id, () => send({type:"focus"}));
  effect(() => { const colors = theme.cssVars[colorScheme() === "light" ? "light" : "dark"]; send({type:"theme",background:colors.background,foreground:colors.foreground}); });
  // The screen follows the tab: it attaches while the pane shows and releases Core's attachment when the pane goes.
  // A pane that only moves between groups is the same mounted view (the layout keeps panes keyed by terminal).
  void showTerminal(id, false);
  effect(() => () => {
    setTerminalScreen(id, null); setScreenFocus(id, null); detachTerminal(id);
    for (const write of writes.values()) { clearTimeout(write.timer); write.reject(new Error("Terminal panel closed.")); } writes.clear();
  });
  const onMessage = (e: {data:string}) => {
    let m: any; try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "ready") { ready = true; for (const q of pending) web?.postMessage(q); pending = []; pendingBytes = 0; send({type:"writable",value:view.controlled()}); }
    if (m.type === "written" && typeof m.id === "number") { const write = writes.get(m.id); if (write) { clearTimeout(write.timer); writes.delete(m.id); write.resolve(); } }
    if (m.type === "focus") {
      setTerminalScreenFocus(id, !!m.focused);
      if (m.focused) { const g = groupIdOfTab("terminal", id); if (g) focusGroup(g); focusTerminal(id); setTerminalScreenFocus(id, true); }
    }
    if (m.type === "focus-list") focusList();
    if (m.type === "toggle-dock") void openTerminalTab();
    if (m.type === "input" && typeof m.data === "string") inputTerminal(m.data, id);
    if (m.type === "resize") resizeTerminal(m.rows, m.cols, id);
    if (m.type === "capture" && evidencePending && m.request === evidencePending.request) {
      const captured = evidencePending; evidencePending = null;
      if (terminalEvidenceContext().generation !== captured.generation || terminalSelected() !== captured.id || draftKey() !== captured.target || typeof m.data !== "string" || !m.data) {
        terminalError.set("Select terminal text first; keep the same terminal and draft open while capturing."); return;
      }
      const payload = terminalEvidence(captured.id, captured.name, m.data, captured.context);
      previewTerminalEvidence(captured.target, payload);
    }
    if (m.type === "selection" && copyPending) { copyPending = false; if (typeof m.data === "string" && m.data) void Clipboard.writeText(m.data); }
  };
  const act = (fn: () => void) => () => { focusTerminal(id); fn(); };
  return <View class="flex-1 min-h-0 bg-background" testID="terminal-panel">
    <View class="z-50 flex-row items-center gap-1 px-3 min-h-9 border-b border-border bg-sidebar">
      <Text style={{numberOfLines:1}} class={`flex-1 min-w-0 ${T.meta}`}>{terminalFocused() && selected() ? "Ctrl+] tabs" : `${item()?.name ?? "Terminal"}${item() ? `  ${item()!.status}` : ""}`}</Text>
      <TerminalButton icon="pencil" label="Rename terminal" onPress={act(() => beginRenameTerminal(id))} testID="terminal-rename" />
      <TerminalButton icon={view.controlled() ? "lock" : "unlock"} label={view.controlled() ? "Release control" : "Take control"} onPress={act(() => void controlTerminal(view.controlled() ? "release" : "acquire", id))} testID="terminal-control" />
      <TerminalButton icon="plus" label="Attach selected output" onPress={attachSelection} disabled={viewed()?.kind === "manager"} testID="terminal-attach" />
      <TerminalButton icon="copy" label="Copy output" onPress={act(() => requestTerminalCopy(id))} testID="terminal-copy" />
      <If when={tabTip()}>{tip => <View style={{width:320}} class="absolute left-3 -top-7 z-50 bg-popover rounded-(--radius) px-2 py-1" testID={`${tip().id}-tooltip`}><Text class="text-[12px] leading-4 font-(family-name:--font-ui) text-popover-foreground" style={{numberOfLines:1}}>{tip().label}</Text></View>}</If>
    </View>
    <View class="flex-1 min-h-0">
      <If when={view.renaming()}><View class="flex-row gap-2 p-2"><Input value={view.name()} onChange={({value}) => { focusTerminal(id); terminalNameSet(id, value); }} onSubmit={() => void renameTerminal(id)} accessibilityLabel="Terminal name" class="flex-1 bg-background text-foreground" /><Btn label="[Rename]" onPress={() => void renameTerminal(id)} /><Btn label="[Cancel]" onPress={() => terminalRenameCancel(id)} /></View></If>
      <If when={!!view.notice()}><Text class="p-2 text-warning" testID="terminal-notice">{view.notice()}</Text></If>
      <If when={!!view.error()}><Text class="p-2 text-error" testID="terminal-error">{view.error()}</Text></If>
      <If when={!view.controlled()}><Text class="p-2 text-warning">Read-only — take control to type.</Text></If>
      <WebView ref={r => { web = r; }} html={terminalPage} onMessage={onMessage} onError={e => terminalError.set(e.message)} class="flex-1 min-h-0" testID="terminal-screen" />
    </View>
  </View>;
}
