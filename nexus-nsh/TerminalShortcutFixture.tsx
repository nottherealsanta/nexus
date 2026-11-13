import { keymap, mount, signal, Text, View } from "nsh";
import { bindKeys, commands } from "./src/commands";
import { currentProject } from "./src/store";
import { terminalAvailable, terminalFocused, terminalItems, terminalOpen, terminalSelected } from "./src/terminal-panel";

// No terminal tab exists, so the shared terminal.open command would create one; the fixture only shows the
// command's wiring: it is bound globally, still bound while a terminal screen has focus, and a plain key is not taken.
terminalItems.set([]);
terminalSelected.set(null);
terminalFocused.set(false);
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
  </View>;
}
mount(Fixture);
