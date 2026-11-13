import { mount, View } from "nsh";
import { TerminalPane } from "./src/ui/terminal";
import { terminalItems, terminalSelected } from "./src/terminal-panel";
import { confirmDialog } from "./src/store";
import { Confirm } from "./src/ui/overlays";
import scenario from "../interface/scenarios/terminal-dock.json";
import type { TerminalResource } from "./src/core/terminal";

// One terminal tab's pane on its own: the toolbar, the read-only banner and the running-close prompt.
terminalItems.set(scenario.terminals.map(t => ({...t, status:t.status as TerminalResource["status"], created:1, ended:null, exit_code:null, signal:null})));
terminalSelected.set(scenario.terminals[0].id);
confirmDialog.set(null);
function Fixture() {
  return <View class="flex-1"><TerminalPane id={scenario.terminals[0].id} /><Confirm /></View>;
}
mount(Fixture);
