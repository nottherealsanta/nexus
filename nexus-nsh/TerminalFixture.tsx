import { keymap, mount, signal, Text, View } from "nsh";
import { TerminalPanel } from "./src/ui/terminal";
import { terminalItems, terminalOpen, terminalSelected } from "./src/terminal-panel";
import { confirmDialog } from "./src/store";
import { Confirm } from "./src/ui/overlays";
import scenario from "../interface/scenarios/terminal-dock.json";
import type { TerminalResource } from "./src/core/terminal";

terminalItems.set(scenario.terminals.map(t => ({...t, status:t.status as TerminalResource["status"], created:1, ended:null, exit_code:null, signal:null})));
terminalOpen.set(true);
terminalSelected.set(scenario.terminals[0].id);
confirmDialog.set(null);
const shortcuts = signal(0);
function Fixture() {
  keymap.bind("ctrl+c", () => shortcuts.update(n => n + 1), {when:"always"});
  keymap.bind("escape", () => shortcuts.update(n => n + 1), {when:"always"});
  return <View class="flex-1"><Text testID="fixture-shortcuts">{String(shortcuts())}</Text><TerminalPanel /><Confirm /></View>;
}
mount(Fixture);
