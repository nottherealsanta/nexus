import { keymap, mount, signal, Text, View } from "nsh";
import { bindKeys, commands } from "./src/commands";
import { currentProject } from "./src/store";
import { terminalAvailable, terminalFocused, terminalItems, terminalOpen, terminalSelected } from "./src/terminal-panel";
import { TerminalPanel } from "./src/ui/terminal";

// The dock starts open with no resources, so the shared terminal.open command
// closes it without an API call. The global keymap is the one App installs.
terminalItems.set([]);
terminalSelected.set(null);
terminalFocused.set(false);
terminalOpen.set(true);
currentProject.set("p");
const plainHits = signal(0);

function Fixture() {
  bindKeys();
  keymap.bind("ctrl+k", () => plainHits.update(n => n + 1), { when: "always" });
  return <View class="flex-1">
    <Text testID="terminal-open">{String(terminalOpen())}</Text>
    <Text testID="terminal-available">{String(terminalAvailable)}</Text>
    <Text testID="terminal-command">{JSON.stringify(commands.find(c => c.id === "terminal.open"))}</Text>
    <Text testID="plain-hits">{String(plainHits())}</Text>
    <If when={terminalOpen()}><TerminalPanel /></If>
  </View>;
}

mount(Fixture);
