import { Clipboard, Scroll, Text, View, WebView, effect, keymap, type WebViewRef, type ViewRef } from "nsh";
import { commands, toKeystroke } from "../commands";
import { PRINTABLE_KEYS } from "../manager";
import { Button, Dialog, Input } from "nsh/base";
import { terminalPage } from "../gen/terminal-page";
import { beginRenameTerminal, closeTerminalPanel, controlTerminal, disconnectTerminal, inputTerminal, newTerminal, renameTerminal, requestTerminalClose, requestTerminalCopy, resizeTerminal, selectTerminal, setTerminalActions, setTerminalSender, terminalControlled, terminalError, terminalFocused, terminalItems, terminalName, terminalNotice, terminalOpen, terminalRename, terminalSelected } from "../terminal-panel";
import { isNarrow, width } from "../store";
import { Btn, FOCUS, T } from "./kit";
import { SCRIM } from "./modal";

export function TerminalPanel() {
  let web: WebViewRef | undefined;
  let list: ViewRef | undefined;
  let copyPending = false;
  effect(() => {
    if (!terminalFocused()) return;
    const modified = ["ctrl", "alt", "cmd"].flatMap(mod => [..."abcdefghijklmnopqrstuvwxyz0123456789[]"].flatMap(k => [`${mod}+${k}`, `${mod}+shift+${k}`]));
    const keys = new Set([...PRINTABLE_KEYS, ...modified, "escape", "enter", "tab", "space", "up", "down", "left", "right", "pageup", "pagedown", ...commands.flatMap(c => [c.keys?.nsh, c.keys?.tui].filter((k): k is string => !!k).map(toKeystroke))]);
    const off: (() => void)[] = [];
    for (const key of keys) { try { off.push(keymap.bind(key, () => {}, {when:"always",passThrough:true})); } catch {} }
    return () => off.forEach(f => f());
  });
  let ready = false;
  let pending: object[] = [];
  let pendingBytes = 0;
  const send = (message: object) => {
    if (ready && web) { web.postMessage(JSON.stringify(message)); return; }
    pendingBytes += JSON.stringify(message).length;
    if (pendingBytes > 1500000) { pending = []; pendingBytes = 0; disconnectTerminal(); terminalError.set("Terminal screen did not load; select the terminal to resynchronize."); return; }
    pending.push(message);
  };
  setTerminalSender(send);
  const focusList = () => { terminalFocused.set(false); web?.injectJavaScript("document.activeElement.blur()"); list?.focus(); };
  setTerminalActions(() => { copyPending = true; send({type:"copy"}); }, focusList);
  effect(() => () => { setTerminalSender(null); setTerminalActions(null, null); });
  const message = (e: {data:string}) => {
    let m: any; try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "ready") { ready = true; for (const p of pending) send(p); pending = []; send({type:"writable",value:terminalControlled()}); }
    if (m.type === "focus") terminalFocused.set(!!m.focused);
    if (m.type === "focus-list") focusList();
    if (m.type === "input" && typeof m.data === "string") inputTerminal(m.data);
    if (m.type === "resize") resizeTerminal(m.rows, m.cols);
    if (m.type === "selection" && copyPending) { copyPending = false; if (typeof m.data === "string" && m.data) void Clipboard.writeText(m.data); }
  };
  const close = () => { setTerminalSender(null); closeTerminalPanel(); };
  return <Dialog open={terminalOpen()} closeOnEscape={false} onCancel={() => { if (!terminalFocused()) close(); }} onOpenChange={({open}) => { if (!open) close(); }}
    backdropClass={SCRIM} testID="terminal-panel-shell"
    style={isNarrow() ? {} : {width:Math.min(1100,width()-64)}}
    class={isNarrow() ? "absolute inset-0 bg-background" : "absolute left-0 right-0 top-[5%] bottom-[5%] mx-auto border border-border bg-background overflow-hidden"}>
    <View class="flex-1 min-h-0" testID="terminal-panel">
      <View class="flex-row items-center gap-2 px-3 min-h-11 border-b border-border bg-sidebar">
        <Text class={`flex-1 ${T.title}`}>Terminal</Text>
        <Btn label="New" onPress={() => void newTerminal()} testID="terminal-new" />
        <Btn label="Close panel" onPress={close} testID="terminal-panel-close" />
      </View>
      <View class="flex-1 min-h-0 flex-row">
        <Scroll ref={r => { list = r; }} focusable class="w-64 border-r border-border" testID="terminal-list">
          <Each of={terminalItems()} key={t => t.id}>{t => <Button onPress={() => void selectTerminal(t().id)} selected={terminalSelected() === t().id} accessibilityLabel={t().name} testID={`terminal-resource-${t().id}`} class={`p-2 border-b border-border ${FOCUS}`}>
            <Text class={T.label}>{t().name}</Text><Text class={T.meta}>{t().workspace}</Text><Text class={T.meta}>{`${t().status} · ${t().controller ?? "No controller"}`}</Text>
          </Button>}</Each>
        </Scroll>
        <View class="flex-1 min-h-0">
          <View class="flex-row gap-2 p-2 border-b border-border">
            <Btn label="Rename" onPress={beginRenameTerminal} disabled={!terminalSelected()} />
            <Btn label="Close" onPress={requestTerminalClose} disabled={!terminalSelected()} />
            <Btn label={terminalControlled() ? "Release control" : "Take control"} onPress={() => void controlTerminal(terminalControlled() ? "release" : "acquire")} disabled={!terminalSelected()} />
            <Btn label="Copy" onPress={requestTerminalCopy} disabled={!terminalSelected()} />
          </View>
          <If when={terminalRename()}><View class="flex-row gap-2 p-2"><Input value={terminalName()} onChange={({value}) => terminalName.set(value)} onSubmit={() => void renameTerminal()} accessibilityLabel="Terminal name" class="flex-1 bg-background text-foreground" /><Btn label="Rename" onPress={() => void renameTerminal()} /><Btn label="Cancel" onPress={() => terminalRename.set(false)} /></View></If>
          <If when={!!terminalNotice()}><Text class="p-2 text-warning" testID="terminal-notice">{terminalNotice()}</Text></If>
          <If when={!!terminalError()}><Text class="p-2 text-error" testID="terminal-error">{terminalError()}</Text></If>
          <WebView ref={r => { web = r; }} html={terminalPage} onMessage={message} onError={e => terminalError.set(e.message)} class="flex-1 min-h-0" testID="terminal-screen" />
        </View>
      </View>
    </View>
  </Dialog>;
}
