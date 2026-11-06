import { keymap, mount, signal, Text, View } from "nsh";
import { TerminalPanel } from "./src/ui/terminal";
import { terminalItems, terminalOpen, terminalSelected } from "./src/terminal-panel";
import { confirmDialog } from "./src/store";
import { Confirm } from "./src/ui/overlays";

terminalItems.set([{id:"term_test",name:"Shell",project_id:"p",workspace:"/work/demo",session_id:null,creator:"test",status:"running",created:1,ended:null,rows:24,cols:80,exit_code:null,signal:null,controller:null}]);
terminalOpen.set(true);
terminalSelected.set("term_test");
confirmDialog.set(null);
const shortcuts = signal(0);
function Fixture() {
  keymap.bind("ctrl+c", () => shortcuts.update(n => n + 1), {when:"always"});
  keymap.bind("escape", () => shortcuts.update(n => n + 1), {when:"always"});
  return <View class="flex-1"><Text testID="fixture-shortcuts">{String(shortcuts())}</Text><TerminalPanel /><Confirm /></View>;
}
mount(Fixture);
