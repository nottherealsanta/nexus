import { Clipboard, PanResponder, Scroll, Text, View, WebView, colorScheme, effect, keymap, signal, type WebViewRef, type ViewRef } from "nsh";
import theme from "../../gen/theme.json";
import { commands, draftKey, focusComposer, term, toKeystroke } from "../commands";
import { PRINTABLE_KEYS } from "../manager";
import { Button, Input } from "nsh/base";
import { terminalPage } from "../gen/terminal-page";
import { terminalEvidenceContext, beginRenameTerminal, closeTerminalPanel, controlTerminal, disconnectTerminal, inputTerminal, newTerminal, renameTerminal, requestTerminalClose, requestTerminalCopy, resizeTerminal, selectTerminal, setTerminalActions, setTerminalCloseFocus, setTerminalSender, terminalControlled, terminalError, terminalFocused, terminalItems, terminalName, terminalNotice, terminalOpen, terminalRename, terminalSelected } from "../terminal-panel";
import { confirmDialog, connection, viewed } from "../store";
import { terminalEvidence, terminalEvidenceAction, type TerminalEvidenceContext } from "../terminal-evidence";
import { previewTerminalEvidence } from "../terminal-evidence-preview";
import { Btn, FOCUS, Icon, T } from "./kit";
import type { IconName } from "../icons";

function TerminalButton(p: {icon: IconName; label: string; onPress: () => void; disabled?: boolean; testID: string}) {
  const hovered = signal(false);
  return <View class="relative" onMouseEnter={() => hovered.set(true)} onMouseLeave={() => hovered.set(false)}><Button onPress={p.onPress} disabled={p.disabled} accessibilityLabel={p.label} testID={p.testID}
    class={`size-8 items-center justify-center rounded-(--radius) hover:bg-accent active:bg-selection disabled:opacity-50 ${FOCUS}`}>
    <View testID={`terminal-icon-${p.icon}`}><Icon name={p.icon} /></View>
  </Button><If when={hovered()}><View style={{width:224}} class="absolute right-0 top-8 z-50 bg-popover rounded-(--radius) px-2 py-1" testID={`${p.testID}-tooltip`}><Text class="text-xs text-popover-foreground">{p.label}</Text></View></If></View>;
}

const dockHeight = signal(320);
export function TerminalPanel() {
  let web: WebViewRef | undefined;
  let list: ViewRef | undefined;
  const cursor = signal(0);
  const tabTip = signal<{id: string; label: string} | null>(null);
  let startHeight = 320;
  const resizeDock = PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onPanResponderGrant: () => { startHeight = dockHeight(); },
    onPanResponderMove: (_event, gesture) => dockHeight.set(Math.max(180, Math.min(700, startHeight - gesture.dy))),
  });
  let copyPending = false;
  let evidenceRequest = 0;
  let evidencePending: {request: number; id: string; name: string; target: string; generation: number; context: TerminalEvidenceContext} | null = null;
  const attachSelection = () => {
    const source = terminalItems().find(t => t.id === terminalSelected());
    if (!source || viewed()?.kind === "manager" || connection().kind !== "live") return;
    focusList();
    const watermark = terminalEvidenceContext();
    evidencePending = {request: ++evidenceRequest, id: source.id, name: source.name, target: draftKey(), generation:watermark.generation, context:{
      project_id:source.project_id, workspace:source.workspace, stream_seq:watermark.stream_seq,
      session_id:source.session_id ?? undefined, turn_id:source.turn_id ?? undefined,
      tool_call_id:source.tool_call_id ?? undefined, background_job_id:source.background_job_id ?? undefined,
    }};
    send({type:"capture", request:evidenceRequest});
  };
  effect(() => {
    if (!terminalFocused()) return;
    const modified = ["ctrl", "alt", "cmd"].flatMap(mod => [..."abcdefghijklmnopqrstuvwxyz0123456789[]"].flatMap(k => [`${mod}+${k}`, `${mod}+shift+${k}`]));
    const keys = new Set([...PRINTABLE_KEYS, ...modified, "escape", "enter", "tab", "space", "up", "down", "left", "right", "pageup", "pagedown", ...commands.flatMap(c => [c.keys?.nsh, c.keys?.tui, ...(c.key_aliases?.nsh ?? []), ...(c.key_aliases?.tui ?? [])].filter((k): k is string => !!k).map(toKeystroke))]);
    const off: (() => void)[] = [];
    for (const key of keys) { try { off.push(keymap.bind(key, () => {}, {when:"always",passThrough:true})); } catch {} }
    return () => off.forEach(f => f());
  });
  terminalEvidenceAction.run = attachSelection;
  effect(() => () => { terminalEvidenceAction.run = null; evidencePending = null; });
  let ready = false;
  let pending: string[] = [];
  let pendingBytes = 0, writeId = 0;
  const writes = new Map<number, {resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout>}>();
  const send = (message: {type: string; [key: string]: unknown}): void | Promise<void> => {
    if (message.type === "reset") evidencePending = null;
    let ack: Promise<void> | undefined;
    if (message.type === "write") {
      const id = ++writeId; message = {...message, id};
      ack = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => { writes.delete(id); reject(new Error("Terminal screen stopped responding.")); }, 15000);
        writes.set(id, {resolve, reject, timer});
      });
    }
    const payload = JSON.stringify(message);
    if (ready && web) web.postMessage(payload);
    else {
      pendingBytes += payload.length;
      if (pendingBytes > 1500000) { pending = []; pendingBytes = 0; disconnectTerminal(); terminalError.set("Terminal screen did not load; select the terminal to resynchronize."); }
      else pending.push(payload);
    }
    return ack;
  };
  setTerminalSender(send);
  effect(() => { const colors = theme.cssVars[colorScheme() === "light" ? "light" : "dark"]; send({type:"theme",background:colors.background,foreground:colors.foreground}); });
  const focusList = () => { terminalFocused.set(false); web?.injectJavaScript("document.activeElement.blur()"); list?.focus(); };
  setTerminalActions(() => { copyPending = true; send({type:"copy"}); }, focusList);
  setTerminalCloseFocus(() => focusComposer.fn?.());
  const create = async () => { await newTerminal(); const index = terminalItems().findIndex(t => t.id === terminalSelected()); if (index >= 0) cursor.set(index + 1); };
  effect(() => () => { setTerminalSender(null); setTerminalActions(null, null); setTerminalCloseFocus(null); for (const write of writes.values()) { clearTimeout(write.timer); write.reject(new Error("Terminal panel closed.")); } writes.clear(); });
  effect(() => {
    if (terminalFocused() || terminalRename() || confirmDialog() || term("overlay.open")) return;
    const bindings: Record<string, () => void> = {
      up: () => cursor.set(Math.max(0, cursor() - 1)),
      down: () => cursor.set(Math.min(terminalItems().length, cursor() + 1)),
      enter: () => {
        if (cursor() === 0) { void create(); return; }
        const item = terminalItems()[cursor() - 1]; if (!item) return;
        if (terminalSelected() === item.id) send({type:"focus"}); else void selectTerminal(item.id);
      },
      c: () => { void controlTerminal("acquire"); },
      r: () => { void controlTerminal("release"); },
    };
    const offs = Object.entries(bindings).map(([key, fn]) => keymap.bind(key, fn, {when:"always"}));
    return () => offs.forEach(off => off());
  });
  const message = (e: {data:string}) => {
    let m: any; try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "ready") { ready = true; for (const p of pending) web?.postMessage(p); pending = []; pendingBytes = 0; send({type:"writable",value:terminalControlled()}); }
    if (m.type === "written" && typeof m.id === "number") { const write = writes.get(m.id); if (write) { clearTimeout(write.timer); writes.delete(m.id); write.resolve(); } }
    if (m.type === "focus") terminalFocused.set(!!m.focused);
    if (m.type === "focus-list") focusList();
    if (m.type === "toggle-dock") closeTerminalPanel();
    if (m.type === "input" && typeof m.data === "string") inputTerminal(m.data);
    if (m.type === "resize") resizeTerminal(m.rows, m.cols);
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
  const close = () => { setTerminalSender(null); closeTerminalPanel(); };
  return <View style={{height:dockHeight()}} class="shrink-0 border-t border-border bg-background" testID="terminal-panel-shell">
    <View {...resizeDock.panHandlers} class="h-2 bg-border" accessibilityLabel="Resize terminal dock" />
    <View class="flex-1 min-h-0" testID="terminal-panel">
      <View class="z-50 flex-row items-center gap-2 px-3 min-h-11 border-b border-border bg-sidebar">
        <Text class={T.title}>{`Terminals (${terminalItems().length})`}</Text>
        <Text class={`flex-1 ${T.meta}`}>{terminalFocused() ? "Ctrl+] tabs" : "Enter open  c take control  r release  Esc close"}</Text>
        <TerminalButton icon="plus" label="New terminal" onPress={() => void create()} testID="terminal-new" />
        <TerminalButton icon="pencil" label="Rename terminal" onPress={beginRenameTerminal} disabled={!terminalSelected()} testID="terminal-rename" />
        <TerminalButton icon="x" label="Close terminal" onPress={() => requestTerminalClose()} disabled={!terminalSelected()} testID="terminal-close" />
        <TerminalButton icon={terminalControlled() ? "lock" : "unlock"} label={terminalControlled() ? "Release control" : "Take control"} onPress={() => void controlTerminal(terminalControlled() ? "release" : "acquire")} disabled={!terminalSelected()} testID="terminal-control" />
        <TerminalButton icon="plus" label="Attach selected output" onPress={attachSelection} disabled={!terminalSelected() || viewed()?.kind === "manager"} testID="terminal-attach" />
        <TerminalButton icon="copy" label="Copy output" onPress={requestTerminalCopy} disabled={!terminalSelected()} testID="terminal-copy" />
        <TerminalButton icon="chevron-down" label="Hide terminal panel" onPress={close} testID="terminal-panel-close" />
        <If when={tabTip()}>{tip => <View style={{width:320}} class="absolute left-3 -top-7 z-50 bg-popover rounded-(--radius) px-2 py-1" testID={`${tip().id}-tooltip`}><Text class="text-xs text-popover-foreground" style={{numberOfLines:1}}>{tip().label}</Text></View>}</If>
      </View>
      <View class="flex-1 min-h-0">
        <Scroll horizontal ref={r => { list = r; }} focusable class="shrink-0 border-b border-border" testID="terminal-list">
          <View class="flex-row items-center">
          <Each of={terminalItems()} key={t => t.id}>{(t, index) => <View class="flex-row items-center">
            <View onMouseEnter={() => tabTip.set({id:`terminal-resource-${t().id}`, label:`${t().name}, ${t().status}`})} onMouseLeave={() => tabTip.set(null)}><Button onPress={() => { cursor.set(index() + 1); void selectTerminal(t().id); }} selected={terminalSelected() === t().id} accessibilityLabel={`${t().name}, ${t().status}`} testID={`terminal-resource-${t().id}`} class={`p-2 ${FOCUS}`}>
              <Text class={T.label}>{`${t().status === "running" ? "●" : "○"} ${t().name}`}</Text>
            </Button></View>
            <View onMouseEnter={() => tabTip.set({id:`terminal-tab-close-${t().id}`, label:`Close ${t().name}`})} onMouseLeave={() => tabTip.set(null)}><Button onPress={() => requestTerminalClose(t().id)} accessibilityLabel={`Close ${t().name}`} testID={`terminal-tab-close-${t().id}`} class={`p-2 ${FOCUS}`}><Text class={T.label}>×</Text></Button></View>
          </View>}</Each>
          </View>
        </Scroll>
        <View class="flex-1 min-h-0">
          <If when={terminalRename()}><View class="flex-row gap-2 p-2"><Input value={terminalName()} onChange={({value}) => terminalName.set(value)} onSubmit={() => void renameTerminal()} accessibilityLabel="Terminal name" class="flex-1 bg-background text-foreground" /><Btn label="[Rename]" onPress={() => void renameTerminal()} /><Btn label="[Cancel]" onPress={() => terminalRename.set(false)} /></View></If>
          <If when={!terminalSelected()}><Text class={`px-3 py-2 ${T.meta}`}>No terminal selected. Use + to create one.</Text></If>
          <If when={!!terminalNotice()}><Text class="p-2 text-warning" testID="terminal-notice">{terminalNotice()}</Text></If>
          <If when={!!terminalError()}><Text class="p-2 text-error" testID="terminal-error">{terminalError()}</Text></If>
          <If when={!!terminalSelected() && !terminalControlled()}><Text class="p-2 text-warning">Read-only — take control to type.</Text></If>
          <WebView ref={r => { web = r; }} html={terminalPage} onMessage={message} onError={e => terminalError.set(e.message)} class="flex-1 min-h-0" testID="terminal-screen" />
        </View>
      </View>
    </View>
  </View>;
}
